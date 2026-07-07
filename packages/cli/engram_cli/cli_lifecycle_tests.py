from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from engram_cli.config import credential_fingerprint
from engram_cli import main
from engram_cli.commands import (
    build_engram_mcp_entry,
    git_remote_url,
    run_connect,
    workspace_repository_url,
)


RAW_KEY = "egk_test_cli_0123456789abcdefghijklmnopqrstuvwxyz"
PROJECT_ID = "11111111-1111-1111-1111-111111111111"
TEAM_ID = "22222222-2222-2222-2222-222222222222"
ORG_ID = "33333333-3333-3333-3333-333333333333"
DRF_TOKEN = "drf-token-abc123"


class FakeTransport:
    def __init__(self, responses: list[tuple[int, dict[str, object]]]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def __call__(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        payload: dict[str, object] | None,
        timeout: float,
    ) -> tuple[int, dict[str, object]]:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "payload": payload,
                "timeout": timeout,
            },
        )
        if not self.responses:
            raise AssertionError("unexpected transport call")

        return self.responses.pop(0)


def dry_run_ok(project_id: str = PROJECT_ID) -> dict[str, object]:
    return {
        "status": "ok",
        "request_id": "request-1",
        "resolved_actor": {"type": "api_key", "id": "api-key-1"},
        "scope": {
            "organization_id": "org-1",
            "project_ids": [project_id],
            "team_ids": [TEAM_ID],
            "capabilities": ["observations:write", "memories:read"],
        },
        "server": {"health": "ok"},
    }


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


class CliLifecycleTests(unittest.TestCase):
    def run_cli(
        self,
        argv: list[str],
        transport: FakeTransport,
        stdin: io.StringIO | None = None,
    ) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()

        exit_code = main.main(
            argv, stdin=stdin, stdout=stdout, stderr=stderr, transport=transport
        )

        return exit_code, stdout.getvalue(), stderr.getvalue()

    def connect(
        self,
        config_dir: Path,
        responses: list[tuple[int, dict[str, object]]] | None = None,
    ) -> FakeTransport:
        transport = FakeTransport(
            responses or [(200, dry_run_ok()), (200, dry_run_ok())]
        )
        exit_code, _stdout, stderr = self.run_cli(
            [
                "connect",
                "--server",
                "https://engram.example/",
                "--api-key",
                RAW_KEY,
                "--project",
                PROJECT_ID,
                "--team",
                TEAM_ID,
                "--config-dir",
                str(config_dir),
            ],
            transport,
        )
        self.assertEqual(0, exit_code, stderr)

        return transport

    def snapshot_files(self, root: Path) -> dict[str, bytes]:
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    def test_connect_verifies_dry_run_then_writes_redacted_local_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            transport = FakeTransport([(200, dry_run_ok()), (200, dry_run_ok())])

            exit_code, stdout, stderr = self.run_cli(
                [
                    "connect",
                    "--server",
                    "https://engram.example/",
                    "--api-key",
                    RAW_KEY,
                    "--project",
                    PROJECT_ID,
                    "--team",
                    TEAM_ID,
                    "--config-dir",
                    str(config_dir),
                ],
                transport,
            )

            self.assertEqual(0, exit_code, stderr)
            self.assertEqual("", stderr)
            self.assertEqual(2, len(transport.calls))
            self.assertEqual(
                ["codex", "claude_code"],
                [call["payload"]["agent_runtime"] for call in transport.calls],
            )
            self.assertEqual(
                [
                    "https://engram.example/v1/hooks/dry-run",
                    "https://engram.example/v1/hooks/dry-run",
                ],
                [call["url"] for call in transport.calls],
            )
            self.assertTrue(
                all(
                    call["headers"]["Authorization"] == f"Bearer {RAW_KEY}"
                    for call in transport.calls
                )
            )

            config_path = config_dir / "config.json"
            credentials_path = config_dir / "credentials.json"
            codex_hook_path = config_dir / "hooks" / "codex.json"
            claude_hook_path = config_dir / "hooks" / "claude_code.json"
            for path in (
                config_path,
                credentials_path,
                codex_hook_path,
                claude_hook_path,
            ):
                self.assertTrue(path.exists(), path)

            config = read_json(config_path)
            credentials = read_json(credentials_path)
            codex_hook = read_json(codex_hook_path)
            claude_hook = read_json(claude_hook_path)
            public_state = f"{config} {codex_hook} {claude_hook}"

            self.assertEqual("https://engram.example", config["server_url"])
            self.assertEqual(PROJECT_ID, config["project_id"])
            self.assertEqual(TEAM_ID, config["team_id"])
            self.assertEqual(["codex", "claude_code"], config["agent_runtimes"])
            self.assertEqual(RAW_KEY, credentials["api_key"])
            self.assertNotIn(RAW_KEY, public_state)
            self.assertEqual(0o600, stat.S_IMODE(credentials_path.stat().st_mode))
            self.assertIn("connected", stdout)
            self.assertIn(PROJECT_ID, stdout)
            self.assertIn("codex", stdout)
            self.assertIn("claude_code", stdout)
            self.assertIn("sha256:", stdout)
            self.assertNotIn(RAW_KEY, stdout)
            self.assertNotIn(RAW_KEY, stderr)

    def test_connect_writes_event_specific_hook_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)

            codex_hook = read_json(config_dir / "hooks" / "codex.json")
            claude_hook = read_json(config_dir / "hooks" / "claude_code.json")

            self.assertEqual(
                "engram hook session-start --agent codex --response-format codex",
                codex_hook["commands"]["SessionStart"],
            )
            self.assertEqual(
                "engram hook post-tool-use --agent codex --response-format codex",
                codex_hook["commands"]["PostToolUse"],
            )
            self.assertEqual(
                "engram hook error --agent codex --response-format codex",
                codex_hook["commands"]["Error"],
            )
            self.assertEqual(
                "engram hook decision --agent codex --response-format codex",
                codex_hook["commands"]["Decision"],
            )
            self.assertEqual(
                "engram hook session-end --agent codex --response-format codex",
                codex_hook["commands"]["SessionEnd"],
            )
            self.assertEqual(
                "engram hook user-prompt-submit --agent codex --response-format codex",
                codex_hook["commands"]["UserPromptSubmit"],
            )
            self.assertEqual(
                "engram hook session-start --agent claude_code --response-format claude-code",
                claude_hook["commands"]["SessionStart"],
            )
            self.assertEqual(
                "engram hook post-tool-use --agent claude_code --response-format claude-code",
                claude_hook["commands"]["PostToolUse"],
            )
            self.assertEqual(
                "engram hook error --agent claude_code --response-format claude-code",
                claude_hook["commands"]["Error"],
            )
            self.assertEqual(
                "engram hook decision --agent claude_code --response-format claude-code",
                claude_hook["commands"]["Decision"],
            )
            self.assertEqual(
                "engram hook session-end --agent claude_code --response-format claude-code",
                claude_hook["commands"]["SessionEnd"],
            )
            self.assertEqual(
                "engram hook user-prompt-submit --agent claude_code --response-format claude-code",
                claude_hook["commands"]["UserPromptSubmit"],
            )

    def test_connect_fingerprint_uses_only_derived_material_for_short_keys(
        self,
    ) -> None:
        short_key = "short"
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            transport = FakeTransport([(200, dry_run_ok()), (200, dry_run_ok())])

            exit_code, stdout, stderr = self.run_cli(
                [
                    "connect",
                    "--server",
                    "https://engram.example",
                    "--api-key",
                    short_key,
                    "--project",
                    PROJECT_ID,
                    "--config-dir",
                    str(config_dir),
                ],
                transport,
            )

            self.assertEqual(0, exit_code, stderr)
            self.assertEqual("", stderr)
            public_state = " ".join(
                [
                    stdout,
                    (config_dir / "config.json").read_text(encoding="utf-8"),
                    (config_dir / "hooks" / "codex.json").read_text(encoding="utf-8"),
                    (config_dir / "hooks" / "claude_code.json").read_text(
                        encoding="utf-8"
                    ),
                ],
            )

            self.assertIn("sha256:", public_state)
            self.assertNotIn(short_key, public_state)
            self.assertNotIn(short_key, credential_fingerprint(short_key))

    def test_connect_writes_nothing_when_dry_run_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            transport = FakeTransport(
                [
                    (
                        403,
                        {
                            "code": "project_scope_denied",
                            "detail": "API key cannot access requested project",
                        },
                    ),
                ],
            )

            exit_code, stdout, stderr = self.run_cli(
                [
                    "connect",
                    "--server",
                    "https://engram.example",
                    "--api-key",
                    RAW_KEY,
                    "--project",
                    PROJECT_ID,
                    "--config-dir",
                    str(config_dir),
                ],
                transport,
            )

            self.assertEqual(1, exit_code)
            self.assertEqual("", stdout)
            self.assertIn("project_scope_denied", stderr)
            self.assertNotIn(RAW_KEY, stderr)
            self.assertEqual([], list(config_dir.rglob("*")))

    def test_connect_redacts_raw_key_from_server_error_detail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            transport = FakeTransport(
                [
                    (
                        401,
                        {
                            "code": "invalid_key",
                            "detail": f"API key {RAW_KEY} is invalid",
                        },
                    ),
                ],
            )

            exit_code, _stdout, stderr = self.run_cli(
                [
                    "connect",
                    "--server",
                    "https://engram.example",
                    "--api-key",
                    RAW_KEY,
                    "--project",
                    PROJECT_ID,
                    "--config-dir",
                    tmp,
                ],
                transport,
            )

            self.assertEqual(1, exit_code)
            self.assertIn("invalid_key", stderr)
            self.assertIn("[REDACTED]", stderr)
            self.assertNotIn(RAW_KEY, stderr)

    def test_connect_rejects_malformed_server_url_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            exit_code, stdout, stderr = self.run_cli(
                [
                    "connect",
                    "--server",
                    "not-a-url",
                    "--api-key",
                    RAW_KEY,
                    "--project",
                    PROJECT_ID,
                    "--config-dir",
                    tmp,
                ],
                FakeTransport([]),
            )

            self.assertEqual(1, exit_code)
            self.assertEqual("", stdout)
            self.assertIn("server_unavailable", stderr)
            self.assertIn("http:// or https://", stderr)
            self.assertNotIn("Traceback", stderr)

    def test_doctor_passes_when_config_health_hooks_and_dry_run_are_valid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            before = self.snapshot_files(config_dir)
            transport = FakeTransport(
                [
                    (200, {"status": "ok", "checks": {"process": "ok"}}),
                    (200, dry_run_ok()),
                    (200, dry_run_ok()),
                ],
            )

            exit_code, stdout, stderr = self.run_cli(
                ["doctor", "--config-dir", str(config_dir)],
                transport,
            )

            self.assertEqual(0, exit_code, stderr)
            self.assertIn("All required checks passed", stdout)
            self.assertEqual(before, self.snapshot_files(config_dir))
            self.assertEqual(
                ["GET", "POST", "POST"], [call["method"] for call in transport.calls]
            )
            self.assertEqual(
                "https://engram.example/-/healthz/", transport.calls[0]["url"]
            )

    def test_doctor_reports_missing_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            exit_code, stdout, stderr = self.run_cli(
                ["doctor", "--config-dir", tmp],
                FakeTransport([]),
            )

            self.assertEqual(1, exit_code)
            self.assertIn("missing_config", stderr)
            self.assertNotIn("All required checks passed", stdout)

    def test_doctor_reports_missing_credential(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            (config_dir / "credentials.json").unlink()

            exit_code, _stdout, stderr = self.run_cli(
                ["doctor", "--config-dir", str(config_dir)],
                FakeTransport([]),
            )

            self.assertEqual(1, exit_code)
            self.assertIn("missing_credential", stderr)

    def test_doctor_reports_missing_hook_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            (config_dir / "hooks" / "codex.json").unlink()

            exit_code, _stdout, stderr = self.run_cli(
                ["doctor", "--config-dir", str(config_dir)],
                FakeTransport([]),
            )

            self.assertEqual(1, exit_code)
            self.assertIn("missing_hook_config", stderr)

    def test_doctor_reports_server_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            transport = FakeTransport(
                [
                    (
                        503,
                        {"status": "unavailable", "checks": {"process": "unavailable"}},
                    ),
                ],
            )

            exit_code, _stdout, stderr = self.run_cli(
                ["doctor", "--config-dir", str(config_dir)],
                transport,
            )

            self.assertEqual(1, exit_code)
            self.assertIn("server_unavailable", stderr)

    def test_doctor_rejects_malformed_stored_server_url_without_transport(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            config_path = config_dir / "config.json"
            config = read_json(config_path)
            config["server_url"] = "not-a-url"
            config_path.write_text(json.dumps(config), encoding="utf-8")

            exit_code, _stdout, stderr = self.run_cli(
                ["doctor", "--config-dir", str(config_dir)],
                FakeTransport([]),
            )

            self.assertEqual(1, exit_code)
            self.assertIn("server_unavailable", stderr)
            self.assertIn("http:// or https://", stderr)

    def test_doctor_reports_invalid_key_from_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            transport = FakeTransport(
                [
                    (200, {"status": "ok", "checks": {"process": "ok"}}),
                    (401, {"code": "invalid_key", "detail": "API key is invalid"}),
                ],
            )

            exit_code, _stdout, stderr = self.run_cli(
                ["doctor", "--config-dir", str(config_dir)],
                transport,
            )

            self.assertEqual(1, exit_code)
            self.assertIn("invalid_key", stderr)
            self.assertNotIn(RAW_KEY, stderr)

    def test_hook_post_tool_use_posts_connected_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            transport = FakeTransport(
                [
                    (
                        202,
                        {
                            "status": "accepted",
                            "duplicate": False,
                            "request_id": "hook-request-1",
                        },
                    ),
                ],
            )
            stdin = io.StringIO(
                json.dumps(
                    {
                        "session_id": "session-1",
                        "event_id": "event-1",
                        "request_id": "hook-request-1",
                        "payload": {
                            "tool_name": "bash",
                            "tool_input": {"command": "pytest"},
                            "tool_response": {"exit_code": 0},
                        },
                        "observation": {
                            "type": "tool_use",
                            "title": "pytest passed",
                            "body": "hook ingest tests passed",
                            "files_read": ["apps/backend/engram/hooks/services.py"],
                            "files_modified": [],
                        },
                    },
                ),
            )

            exit_code, stdout, stderr = self.run_cli(
                ["hook", "post-tool-use", "--config-dir", str(config_dir)],
                transport,
                stdin=stdin,
            )

            self.assertEqual(0, exit_code, stderr)
            self.assertEqual("", stderr)
            self.assertEqual(1, len(transport.calls))
            call = transport.calls[0]
            payload = call["payload"]
            self.assertEqual("POST", call["method"])
            self.assertEqual(
                "https://engram.example/v1/hooks/post-tool-use", call["url"]
            )
            self.assertEqual(f"Bearer {RAW_KEY}", call["headers"]["Authorization"])
            self.assertEqual(PROJECT_ID, payload["project_id"])
            self.assertEqual(TEAM_ID, payload["team_id"])
            self.assertEqual("codex", payload["agent_runtime"])
            self.assertEqual("post_tool_use", payload["event_type"])
            self.assertEqual("v1", payload["payload_schema_version"])
            self.assertEqual("event-1", payload["idempotency_key"])
            self.assertTrue(payload["content_hash"])
            body = json.loads(stdout)
            self.assertEqual("accepted", body["status"])
            self.assertNotIn(RAW_KEY, stdout)
            self.assertNotIn(RAW_KEY, stderr)

    def test_hook_error_posts_connected_event_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            transport = FakeTransport(
                [
                    (
                        202,
                        {
                            "status": "accepted",
                            "duplicate": False,
                            "request_id": "error-request-1",
                        },
                    ),
                ],
            )
            stdin = io.StringIO(
                json.dumps(
                    {
                        "session_id": "session-1",
                        "event_id": "error-event-1",
                        "request_id": "error-request-1",
                        "payload": {"message": "tool failed"},
                        "observation": {"type": "error", "title": "tool failed"},
                    },
                ),
            )

            exit_code, stdout, stderr = self.run_cli(
                ["hook", "error", "--agent", "codex", "--config-dir", str(config_dir)],
                transport,
                stdin=stdin,
            )

            self.assertEqual(0, exit_code, stderr)
            self.assertEqual("", stderr)
            self.assertEqual(1, len(transport.calls))
            call = transport.calls[0]
            payload = call["payload"]
            self.assertEqual("POST", call["method"])
            self.assertEqual("https://engram.example/v1/hooks/error", call["url"])
            self.assertEqual("error", payload["event_type"])
            self.assertEqual("error-event-1", payload["idempotency_key"])
            self.assertEqual({"message": "tool failed"}, payload["payload"])
            self.assertEqual(
                {"type": "error", "title": "tool failed"}, payload["observation"]
            )
            body = json.loads(stdout)
            self.assertEqual("accepted", body["status"])
            self.assertNotIn(RAW_KEY, stdout)
            self.assertNotIn(RAW_KEY, stderr)

    def test_hook_error_derives_stable_fallback_idempotency_for_identical_input(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            transport = FakeTransport(
                [
                    (
                        202,
                        {
                            "status": "accepted",
                            "duplicate": False,
                            "request_id": "error-request-1",
                        },
                    ),
                    (
                        202,
                        {
                            "status": "accepted",
                            "duplicate": True,
                            "request_id": "error-request-2",
                        },
                    ),
                    (
                        202,
                        {
                            "status": "accepted",
                            "duplicate": False,
                            "request_id": "error-request-3",
                        },
                    ),
                ],
            )
            base_input = {
                "session_id": "session-1",
                "request_id": "request-1",
                "payload_schema_version": "v2",
                "sequence_number": 7,
                "payload": {"message": "tool failed"},
                "observation": {"type": "error", "title": "tool failed"},
                "agent_external_id": "codex-agent-1",
                "repository_url": "https://github.com/Barsoomx/engram",
                "repository_root": "/workspace/engram",
                "branch": "feat/parity-14-hook-event-coverage",
                "cwd": "/workspace/engram/packages/cli",
            }

            for hook_input in (
                base_input,
                dict(base_input),
                {**base_input, "payload": {"message": "tool failed again"}},
            ):
                exit_code, stdout, stderr = self.run_cli(
                    [
                        "hook",
                        "error",
                        "--agent",
                        "codex",
                        "--config-dir",
                        str(config_dir),
                    ],
                    transport,
                    stdin=io.StringIO(json.dumps(hook_input)),
                )
                self.assertEqual(0, exit_code, stderr)
                self.assertEqual("", stderr)
                self.assertEqual("accepted", json.loads(stdout)["status"])

            first_payload = transport.calls[0]["payload"]
            second_payload = transport.calls[1]["payload"]
            changed_payload = transport.calls[2]["payload"]
            for payload in (first_payload, second_payload, changed_payload):
                self.assertTrue(str(payload["event_id"]).startswith("engram-cli-"))
                self.assertEqual(payload["event_id"], payload["idempotency_key"])
                self.assertTrue(payload["content_hash"])

            self.assertEqual(first_payload["event_id"], second_payload["event_id"])
            self.assertEqual(
                first_payload["idempotency_key"], second_payload["idempotency_key"]
            )
            self.assertEqual(
                first_payload["content_hash"], second_payload["content_hash"]
            )
            self.assertNotEqual(first_payload["event_id"], changed_payload["event_id"])
            self.assertNotEqual(
                first_payload["idempotency_key"], changed_payload["idempotency_key"]
            )
            self.assertNotEqual(
                first_payload["content_hash"], changed_payload["content_hash"]
            )

    def test_hook_error_preserves_explicit_idempotency_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            transport = FakeTransport(
                [
                    (
                        202,
                        {
                            "status": "accepted",
                            "duplicate": False,
                            "request_id": "error-request-1",
                        },
                    ),
                ],
            )
            stdin = io.StringIO(
                json.dumps(
                    {
                        "session_id": "session-1",
                        "event_id": "explicit-event-1",
                        "idempotency_key": "explicit-key-1",
                        "content_hash": "explicit-content-hash-1",
                        "payload": {"message": "tool failed"},
                    },
                ),
            )

            exit_code, _stdout, stderr = self.run_cli(
                ["hook", "error", "--agent", "codex", "--config-dir", str(config_dir)],
                transport,
                stdin=stdin,
            )

            self.assertEqual(0, exit_code, stderr)
            self.assertEqual("", stderr)
            payload = transport.calls[0]["payload"]
            self.assertEqual("explicit-event-1", payload["event_id"])
            self.assertEqual("explicit-key-1", payload["idempotency_key"])
            self.assertEqual("explicit-content-hash-1", payload["content_hash"])

    def test_hook_decision_posts_connected_event_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            transport = FakeTransport(
                [
                    (
                        202,
                        {
                            "status": "accepted",
                            "duplicate": False,
                            "request_id": "decision-request-1",
                        },
                    ),
                ],
            )
            stdin = io.StringIO(
                json.dumps(
                    {
                        "session_id": "session-1",
                        "event_id": "decision-event-1",
                        "request_id": "decision-request-1",
                        "payload": {"choice": "keep thin cli"},
                        "correlation_id": "corr-1",
                    },
                ),
            )

            exit_code, stdout, stderr = self.run_cli(
                [
                    "hook",
                    "decision",
                    "--agent",
                    "codex",
                    "--config-dir",
                    str(config_dir),
                ],
                transport,
                stdin=stdin,
            )

            self.assertEqual(0, exit_code, stderr)
            self.assertEqual("", stderr)
            self.assertEqual(1, len(transport.calls))
            call = transport.calls[0]
            payload = call["payload"]
            self.assertEqual("POST", call["method"])
            self.assertEqual("https://engram.example/v1/hooks/decision", call["url"])
            self.assertEqual("decision", payload["event_type"])
            self.assertEqual("decision-event-1", payload["idempotency_key"])
            self.assertEqual({"choice": "keep thin cli"}, payload["payload"])
            self.assertEqual("corr-1", payload["correlation_id"])
            body = json.loads(stdout)
            self.assertEqual("accepted", body["status"])
            self.assertNotIn(RAW_KEY, stdout)
            self.assertNotIn(RAW_KEY, stderr)

    def test_hook_session_end_posts_connected_event_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            transport = FakeTransport(
                [
                    (
                        202,
                        {
                            "status": "accepted",
                            "duplicate": False,
                            "request_id": "session-end-request-1",
                        },
                    ),
                ],
            )
            stdin = io.StringIO(
                json.dumps(
                    {
                        "session_id": "session-1",
                        "event_id": "session-end-event-1",
                        "request_id": "session-end-request-1",
                        "payload": {"reason": "agent stopped"},
                        "observation": {"type": "session_end", "title": "session ended"},
                    },
                ),
            )

            exit_code, stdout, stderr = self.run_cli(
                [
                    "hook",
                    "session-end",
                    "--agent",
                    "claude_code",
                    "--config-dir",
                    str(config_dir),
                ],
                transport,
                stdin=stdin,
            )

            self.assertEqual(0, exit_code, stderr)
            self.assertEqual("", stderr)
            self.assertEqual(1, len(transport.calls))
            call = transport.calls[0]
            payload = call["payload"]
            self.assertEqual("POST", call["method"])
            self.assertEqual(
                "https://engram.example/v1/hooks/session-end", call["url"]
            )
            self.assertEqual("session_end", payload["event_type"])
            self.assertEqual("session-end-event-1", payload["idempotency_key"])
            self.assertEqual({"reason": "agent stopped"}, payload["payload"])
            self.assertEqual(
                {"type": "session_end", "title": "session ended"},
                payload["observation"],
            )
            body = json.loads(stdout)
            self.assertEqual("accepted", body["status"])
            self.assertNotIn(RAW_KEY, stdout)
            self.assertNotIn(RAW_KEY, stderr)

    def test_hook_session_end_non_2xx_response_exits_with_1(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            transport = FakeTransport(
                [
                    (
                        500,
                        {"code": "server_error", "detail": "Internal Server Error"},
                    ),
                ],
            )
            stdin = io.StringIO(
                json.dumps(
                    {
                        "session_id": "session-1",
                        "event_id": "session-end-event-2",
                    },
                ),
            )

            exit_code, _stdout, stderr = self.run_cli(
                [
                    "hook",
                    "session-end",
                    "--agent",
                    "claude_code",
                    "--config-dir",
                    str(config_dir),
                ],
                transport,
                stdin=stdin,
            )

            self.assertEqual(1, exit_code)
            self.assertIn("server_error", stderr)

    def test_hook_session_start_posts_event_then_requests_context_with_connected_scope(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            transport = FakeTransport(
                [
                    (
                        202,
                        {
                            "status": "accepted",
                            "duplicate": False,
                            "request_id": "session-event-request-1",
                        },
                    ),
                    (
                        200,
                        {
                            "status": "created",
                            "purpose": "session_start",
                            "items": [{"citation": "M1"}],
                        },
                    ),
                ],
            )
            stdin = io.StringIO(
                json.dumps(
                    {
                        "session_id": "future-session",
                        "event_id": "session-event-1",
                        "request_id": "context-request-1",
                        "query": "hook ingest replay handling",
                        "file_paths": ["apps/backend/engram/hooks/services.py"],
                        "symbols": ["IngestHookEvent"],
                        "payload": {"source": "codex"},
                    },
                ),
            )

            exit_code, stdout, stderr = self.run_cli(
                ["hook", "session-start", "--config-dir", str(config_dir)],
                transport,
                stdin=stdin,
            )

            self.assertEqual(0, exit_code, stderr)
            self.assertEqual("", stderr)
            self.assertEqual(2, len(transport.calls))
            hook_call = transport.calls[0]
            hook_payload = hook_call["payload"]
            context_call = transport.calls[1]
            context_payload = context_call["payload"]
            self.assertEqual("POST", hook_call["method"])
            self.assertEqual(
                "https://engram.example/v1/hooks/session-start", hook_call["url"]
            )
            self.assertEqual("session_start", hook_payload["event_type"])
            self.assertEqual("session-event-1", hook_payload["idempotency_key"])
            self.assertEqual({"source": "codex"}, hook_payload["payload"])
            self.assertEqual("POST", context_call["method"])
            self.assertEqual(
                "https://engram.example/v1/context/session-start", context_call["url"]
            )
            self.assertEqual(PROJECT_ID, context_payload["project_id"])
            self.assertEqual(TEAM_ID, context_payload["team_id"])
            self.assertEqual("codex", context_payload["agent_runtime"])
            self.assertEqual("context-request-1", context_payload["request_id"])
            self.assertEqual(
                ["apps/backend/engram/hooks/services.py"], context_payload["file_paths"]
            )
            body = json.loads(stdout)
            self.assertEqual("created", body["status"])
            self.assertNotIn(RAW_KEY, stdout)
            self.assertNotIn(RAW_KEY, stderr)

    def test_hook_session_start_posts_non_empty_lifecycle_payload_without_input_payload(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            transport = FakeTransport(
                [
                    (
                        202,
                        {
                            "status": "accepted",
                            "duplicate": False,
                            "request_id": "session-event-request-1",
                        },
                    ),
                    (
                        200,
                        {
                            "status": "created",
                            "purpose": "session_start",
                            "items": [{"citation": "M1"}],
                        },
                    ),
                ],
            )
            stdin_payload = {
                "session_id": "future-session",
                "event_id": "session-event-1",
                "request_id": "context-request-1",
                "query": "hook ingest replay handling",
                "file_paths": ["apps/backend/engram/hooks/services.py"],
                "symbols": ["IngestHookEvent"],
                "repository_root": "/workspace/engram",
                "branch": "feat/parity-14-hook-event-coverage",
                "cwd": "/workspace/engram/packages/cli",
            }

            exit_code, stdout, stderr = self.run_cli(
                ["hook", "session-start", "--config-dir", str(config_dir)],
                transport,
                stdin=io.StringIO(json.dumps(stdin_payload)),
            )

            self.assertEqual(0, exit_code, stderr)
            self.assertEqual("", stderr)
            self.assertEqual(2, len(transport.calls))
            hook_call = transport.calls[0]
            hook_payload = hook_call["payload"]
            context_call = transport.calls[1]
            context_payload = context_call["payload"]
            self.assertEqual("POST", hook_call["method"])
            self.assertEqual(
                "https://engram.example/v1/hooks/session-start", hook_call["url"]
            )
            self.assertEqual("session_start", hook_payload["event_type"])
            self.assertEqual(
                {
                    "trigger": "session_start",
                    "repository_root": "/workspace/engram",
                    "branch": "feat/parity-14-hook-event-coverage",
                    "cwd": "/workspace/engram/packages/cli",
                },
                hook_payload["payload"],
            )
            self.assertEqual("POST", context_call["method"])
            self.assertEqual(
                "https://engram.example/v1/context/session-start", context_call["url"]
            )
            self.assertEqual("context-request-1", context_payload["request_id"])
            self.assertEqual("hook ingest replay handling", context_payload["query"])
            self.assertEqual(
                ["apps/backend/engram/hooks/services.py"], context_payload["file_paths"]
            )
            self.assertEqual(["IngestHookEvent"], context_payload["symbols"])
            self.assertNotIn("payload", context_payload)
            body = json.loads(stdout)
            self.assertEqual("created", body["status"])
            self.assertNotIn(RAW_KEY, stdout)
            self.assertNotIn(RAW_KEY, stderr)

    def test_hook_session_start_codex_response_format_emits_hook_specific_output(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            transport = FakeTransport(
                [
                    (
                        202,
                        {
                            "status": "accepted",
                            "duplicate": False,
                            "request_id": "session-event-request-1",
                        },
                    ),
                    (
                        200,
                        {
                            "status": "created",
                            "purpose": "session_start",
                            "rendered_context": "Relevant Engram context",
                            "items": [{"citation": "M1"}],
                        },
                    ),
                ],
            )
            stdin = io.StringIO(
                json.dumps(
                    {
                        "session_id": "future-session",
                        "event_id": "session-event-1",
                        "request_id": "context-request-1",
                    },
                ),
            )

            exit_code, stdout, stderr = self.run_cli(
                [
                    "hook",
                    "session-start",
                    "--response-format",
                    "codex",
                    "--config-dir",
                    str(config_dir),
                ],
                transport,
                stdin=stdin,
            )

            self.assertEqual(0, exit_code, stderr)
            self.assertEqual("", stderr)
            body = json.loads(stdout)
            self.assertEqual(True, body["continue"])
            self.assertEqual("Relevant Engram context", body["systemMessage"])
            self.assertEqual(
                {
                    "hookEventName": "SessionStart",
                    "additionalContext": "Relevant Engram context",
                },
                body["hookSpecificOutput"],
            )
            self.assertNotIn(RAW_KEY, stdout)
            self.assertNotIn(RAW_KEY, stderr)

    def test_hook_session_start_claude_code_response_format_emits_claude_output_only(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            transport = FakeTransport(
                [
                    (
                        202,
                        {
                            "status": "accepted",
                            "duplicate": False,
                            "request_id": "session-event-request-1",
                        },
                    ),
                    (
                        200,
                        {
                            "status": "created",
                            "purpose": "session_start",
                            "rendered_context": "Relevant Engram context",
                            "items": [{"citation": "M1"}],
                        },
                    ),
                ],
            )
            stdin = io.StringIO(
                json.dumps(
                    {
                        "session_id": "future-session",
                        "event_id": "session-event-1",
                        "request_id": "context-request-1",
                    },
                ),
            )

            exit_code, stdout, stderr = self.run_cli(
                [
                    "hook",
                    "session-start",
                    "--response-format",
                    "claude-code",
                    "--config-dir",
                    str(config_dir),
                ],
                transport,
                stdin=stdin,
            )

            self.assertEqual(0, exit_code, stderr)
            self.assertEqual("", stderr)
            body = json.loads(stdout)
            self.assertNotIn("continue", body)
            self.assertEqual("Relevant Engram context", body["systemMessage"])
            self.assertEqual(
                {
                    "hookEventName": "SessionStart",
                    "additionalContext": "Relevant Engram context",
                },
                body["hookSpecificOutput"],
            )
            self.assertNotIn(RAW_KEY, stdout)
            self.assertNotIn(RAW_KEY, stderr)

    def test_hook_non_session_claude_code_response_format_emits_empty_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            transport = FakeTransport(
                [
                    (
                        202,
                        {
                            "status": "accepted",
                            "duplicate": False,
                            "request_id": "tool-event-request-1",
                        },
                    ),
                ],
            )

            exit_code, stdout, stderr = self.run_cli(
                [
                    "hook",
                    "post-tool-use",
                    "--response-format",
                    "claude-code",
                    "--config-dir",
                    str(config_dir),
                ],
                transport,
                stdin=io.StringIO(
                    json.dumps(
                        {
                            "session_id": "future-session",
                            "event_id": "tool-event-1",
                            "tool_name": "Read",
                        },
                    ),
                ),
            )

            self.assertEqual(0, exit_code, stderr)
            self.assertEqual("", stderr)
            self.assertEqual({}, json.loads(stdout))
            self.assertNotIn(RAW_KEY, stdout)
            self.assertNotIn(RAW_KEY, stderr)

    def test_hook_rejects_invalid_response_format_through_argparse(self) -> None:
        exit_code, stdout, stderr = self.run_cli(
            ["hook", "session-start", "--response-format", "xml"],
            FakeTransport([]),
            stdin=io.StringIO("{}"),
        )

        self.assertEqual(2, exit_code)
        self.assertEqual("", stdout)
        self.assertNotIn(RAW_KEY, stderr)

    def test_hook_rejects_invalid_json_without_transport_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            transport = FakeTransport([])

            exit_code, stdout, stderr = self.run_cli(
                ["hook", "post-tool-use", "--config-dir", str(config_dir)],
                transport,
                stdin=io.StringIO("{not-json"),
            )

            self.assertEqual(1, exit_code)
            self.assertEqual("", stdout)
            self.assertIn("invalid_response", stderr)
            self.assertEqual([], transport.calls)

    def test_hook_reports_missing_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            exit_code, stdout, stderr = self.run_cli(
                ["hook", "session-start", "--config-dir", tmp],
                FakeTransport([]),
                stdin=io.StringIO("{}"),
            )

            self.assertEqual(1, exit_code)
            self.assertEqual("", stdout)
            self.assertIn("missing_config", stderr)

    def test_hook_user_prompt_submit_posts_event_then_requests_context(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            transport = FakeTransport(
                [
                    (
                        202,
                        {
                            "status": "accepted",
                            "duplicate": False,
                            "request_id": "ups-event-request-1",
                        },
                    ),
                    (
                        200,
                        {
                            "status": "created",
                            "purpose": "user_prompt_submit",
                            "rendered_context": "Relevant Engram context",
                            "hook_specific_output": {
                                "hookEventName": "UserPromptSubmit",
                                "additionalContext": "Relevant Engram context",
                            },
                            "items": [{"citation": "M1"}],
                        },
                    ),
                ],
            )
            stdin = io.StringIO(
                json.dumps(
                    {
                        "session_id": "session-ups-1",
                        "event_id": "ups-event-1",
                        "request_id": "ups-context-request-1",
                        "prompt": "how does authorization work?",
                        "query": "how does authorization work?",
                        "file_paths": ["apps/backend/engram/context/services.py"],
                        "symbols": ["BuildContextBundle"],
                        "payload": {"prompt": "how does authorization work?"},
                    },
                ),
            )

            exit_code, stdout, stderr = self.run_cli(
                ["hook", "user-prompt-submit", "--config-dir", str(config_dir)],
                transport,
                stdin=stdin,
            )

            self.assertEqual(0, exit_code, stderr)
            self.assertEqual("", stderr)
            self.assertEqual(2, len(transport.calls))
            hook_call = transport.calls[0]
            hook_payload = hook_call["payload"]
            context_call = transport.calls[1]
            context_payload = context_call["payload"]
            self.assertEqual("POST", hook_call["method"])
            self.assertEqual(
                "https://engram.example/v1/hooks/user-prompt-submit", hook_call["url"]
            )
            self.assertEqual("user_prompt_submit", hook_payload["event_type"])
            self.assertEqual("ups-event-1", hook_payload["idempotency_key"])
            self.assertEqual(
                {"prompt": "how does authorization work?"}, hook_payload["payload"]
            )
            self.assertEqual("POST", context_call["method"])
            self.assertEqual(
                "https://engram.example/v1/context/user-prompt-submit",
                context_call["url"],
            )
            self.assertEqual(PROJECT_ID, context_payload["project_id"])
            self.assertEqual(TEAM_ID, context_payload["team_id"])
            self.assertEqual("codex", context_payload["agent_runtime"])
            self.assertEqual("ups-context-request-1", context_payload["request_id"])
            self.assertEqual(
                ["apps/backend/engram/context/services.py"],
                context_payload["file_paths"],
            )
            body = json.loads(stdout)
            self.assertEqual("created", body["status"])
            self.assertNotIn(RAW_KEY, stdout)
            self.assertNotIn(RAW_KEY, stderr)

    def test_hook_user_prompt_submit_codex_response_format_emits_hook_specific_output(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            transport = FakeTransport(
                [
                    (
                        202,
                        {
                            "status": "accepted",
                            "duplicate": False,
                            "request_id": "ups-event-request-1",
                        },
                    ),
                    (
                        200,
                        {
                            "status": "created",
                            "purpose": "user_prompt_submit",
                            "rendered_context": "Relevant Engram context",
                            "items": [{"citation": "M1"}],
                        },
                    ),
                ],
            )
            stdin = io.StringIO(
                json.dumps(
                    {
                        "session_id": "session-ups-1",
                        "event_id": "ups-event-1",
                        "request_id": "ups-context-request-1",
                        "prompt": "how does authorization work in this service?",
                    },
                ),
            )

            exit_code, stdout, stderr = self.run_cli(
                [
                    "hook",
                    "user-prompt-submit",
                    "--response-format",
                    "codex",
                    "--config-dir",
                    str(config_dir),
                ],
                transport,
                stdin=stdin,
            )

            self.assertEqual(0, exit_code, stderr)
            self.assertEqual("", stderr)
            body = json.loads(stdout)
            self.assertEqual(True, body["continue"])
            self.assertEqual("Relevant Engram context", body["systemMessage"])
            self.assertEqual(
                {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": "Relevant Engram context",
                },
                body["hookSpecificOutput"],
            )
            self.assertNotIn(RAW_KEY, stdout)
            self.assertNotIn(RAW_KEY, stderr)

    def test_hook_user_prompt_submit_claude_code_response_format_emits_claude_output_only(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            transport = FakeTransport(
                [
                    (
                        202,
                        {
                            "status": "accepted",
                            "duplicate": False,
                            "request_id": "ups-event-request-1",
                        },
                    ),
                    (
                        200,
                        {
                            "status": "created",
                            "purpose": "user_prompt_submit",
                            "rendered_context": "Relevant Engram context",
                            "items": [{"citation": "M1"}],
                        },
                    ),
                ],
            )
            stdin = io.StringIO(
                json.dumps(
                    {
                        "session_id": "session-ups-1",
                        "event_id": "ups-event-1",
                        "request_id": "ups-context-request-1",
                        "prompt": "how does authorization work in this service?",
                    },
                ),
            )

            exit_code, stdout, stderr = self.run_cli(
                [
                    "hook",
                    "user-prompt-submit",
                    "--response-format",
                    "claude-code",
                    "--config-dir",
                    str(config_dir),
                ],
                transport,
                stdin=stdin,
            )

            self.assertEqual(0, exit_code, stderr)
            self.assertEqual("", stderr)
            body = json.loads(stdout)
            self.assertNotIn("continue", body)
            self.assertEqual("Relevant Engram context", body["systemMessage"])
            self.assertEqual(
                {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": "Relevant Engram context",
                },
                body["hookSpecificOutput"],
            )
            self.assertNotIn(RAW_KEY, stdout)
            self.assertNotIn(RAW_KEY, stderr)

    def test_hook_user_prompt_submit_non_2xx_hook_response_exits_with_1(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            transport = FakeTransport(
                [
                    (
                        500,
                        {"code": "server_error", "detail": "Internal Server Error"},
                    ),
                ],
            )
            stdin = io.StringIO(
                json.dumps(
                    {
                        "session_id": "session-ups-1",
                        "event_id": "ups-error-event-1",
                    },
                ),
            )

            exit_code, _stdout, stderr = self.run_cli(
                [
                    "hook",
                    "user-prompt-submit",
                    "--agent",
                    "claude_code",
                    "--config-dir",
                    str(config_dir),
                ],
                transport,
                stdin=stdin,
            )

            self.assertEqual(1, exit_code)
            self.assertIn("server_error", stderr)

    def test_hook_user_prompt_submit_skips_context_query_for_short_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            transport = FakeTransport(
                [
                    (
                        202,
                        {
                            "status": "accepted",
                            "duplicate": False,
                            "request_id": "ups-event-request-1",
                        },
                    ),
                ],
            )
            stdin = io.StringIO(
                json.dumps(
                    {
                        "session_id": "session-ups-1",
                        "event_id": "ups-short-event-1",
                        "prompt": "hi there",
                    },
                ),
            )

            exit_code, stdout, stderr = self.run_cli(
                ["hook", "user-prompt-submit", "--config-dir", str(config_dir)],
                transport,
                stdin=stdin,
            )

            self.assertEqual(0, exit_code, stderr)
            self.assertEqual("", stderr)
            self.assertEqual(1, len(transport.calls))
            self.assertEqual(
                "https://engram.example/v1/hooks/user-prompt-submit",
                transport.calls[0]["url"],
            )
            self.assertEqual({}, json.loads(stdout))

    def test_hook_user_prompt_submit_skips_context_query_for_slash_command(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            transport = FakeTransport(
                [
                    (
                        202,
                        {
                            "status": "accepted",
                            "duplicate": False,
                            "request_id": "ups-event-request-1",
                        },
                    ),
                ],
            )
            stdin = io.StringIO(
                json.dumps(
                    {
                        "session_id": "session-ups-1",
                        "event_id": "ups-slash-event-1",
                        "prompt": "/mem-search please look up something specific",
                    },
                ),
            )

            exit_code, stdout, stderr = self.run_cli(
                ["hook", "user-prompt-submit", "--config-dir", str(config_dir)],
                transport,
                stdin=stdin,
            )

            self.assertEqual(0, exit_code, stderr)
            self.assertEqual(1, len(transport.calls))
            self.assertEqual({}, json.loads(stdout))

    def test_hook_user_prompt_submit_skips_context_query_for_command_tag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            transport = FakeTransport(
                [
                    (
                        202,
                        {
                            "status": "accepted",
                            "duplicate": False,
                            "request_id": "ups-event-request-1",
                        },
                    ),
                ],
            )
            stdin = io.StringIO(
                json.dumps(
                    {
                        "session_id": "session-ups-1",
                        "event_id": "ups-command-tag-event-1",
                        "prompt": "<command-name>investigate this issue</command-name>",
                    },
                ),
            )

            exit_code, stdout, stderr = self.run_cli(
                ["hook", "user-prompt-submit", "--config-dir", str(config_dir)],
                transport,
                stdin=stdin,
            )

            self.assertEqual(0, exit_code, stderr)
            self.assertEqual(1, len(transport.calls))
            self.assertEqual({}, json.loads(stdout))

    def test_hook_user_prompt_submit_queries_context_when_prompt_meets_threshold(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            transport = FakeTransport(
                [
                    (
                        202,
                        {
                            "status": "accepted",
                            "duplicate": False,
                            "request_id": "ups-event-request-1",
                        },
                    ),
                    (
                        200,
                        {
                            "status": "created",
                            "purpose": "user_prompt_submit",
                            "rendered_context": "Relevant Engram context",
                            "items": [{"citation": "M1"}],
                        },
                    ),
                ],
            )
            stdin = io.StringIO(
                json.dumps(
                    {
                        "session_id": "session-ups-1",
                        "event_id": "ups-threshold-event-1",
                        "prompt": "12345678901234567890",
                    },
                ),
            )

            exit_code, stdout, stderr = self.run_cli(
                ["hook", "user-prompt-submit", "--config-dir", str(config_dir)],
                transport,
                stdin=stdin,
            )

            self.assertEqual(0, exit_code, stderr)
            self.assertEqual(2, len(transport.calls))
            self.assertEqual(
                "https://engram.example/v1/context/user-prompt-submit",
                transport.calls[1]["url"],
            )
            body = json.loads(stdout)
            self.assertEqual("created", body["status"])

    def test_disconnect_removes_only_engram_owned_state_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            keep_path = config_dir / "keep.txt"
            keep_path.write_text("user data", encoding="utf-8")

            first_exit, first_stdout, first_stderr = self.run_cli(
                ["disconnect", "--config-dir", str(config_dir)],
                FakeTransport([]),
            )
            second_exit, second_stdout, second_stderr = self.run_cli(
                ["disconnect", "--config-dir", str(config_dir)],
                FakeTransport([]),
            )

            self.assertEqual(0, first_exit, first_stderr)
            self.assertEqual(0, second_exit, second_stderr)
            self.assertIn("disconnected", first_stdout)
            self.assertIn("nothing connected", second_stdout)
            self.assertTrue(keep_path.exists())
            self.assertFalse((config_dir / "config.json").exists())
            self.assertFalse((config_dir / "credentials.json").exists())
            self.assertFalse((config_dir / "hooks" / "codex.json").exists())
            self.assertFalse((config_dir / "hooks" / "claude_code.json").exists())

    def test_connect_requires_server_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            exit_code, _stdout, stderr = self.run_cli(
                [
                    "connect",
                    "--api-key",
                    RAW_KEY,
                    "--project",
                    PROJECT_ID,
                    "--config-dir",
                    tmp,
                ],
                FakeTransport([]),
            )

            self.assertEqual(1, exit_code)
            self.assertIn("missing_server_url", stderr)

    def test_connect_requires_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            exit_code, _stdout, stderr = self.run_cli(
                [
                    "connect",
                    "--server",
                    "https://engram.example",
                    "--project",
                    PROJECT_ID,
                    "--config-dir",
                    tmp,
                ],
                FakeTransport([]),
            )

            self.assertEqual(1, exit_code)
            self.assertIn("missing_api_key", stderr)

    def test_connect_without_project_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            exit_code, _stdout, stderr = self.run_cli(
                [
                    "connect",
                    "--server",
                    "https://engram.example",
                    "--api-key",
                    RAW_KEY,
                    "--config-dir",
                    tmp,
                ],
                FakeTransport([(200, dry_run_ok()), (200, dry_run_ok())]),
            )

            self.assertEqual(0, exit_code, stderr)
            self.assertTrue((Path(tmp) / "config.json").exists())

    def test_search_posts_query_and_prints_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            search_response = {
                "request_id": "request-search-1",
                "items": [
                    {
                        "citation": "M1",
                        "memory_id": "mem-1",
                        "memory_version_id": "ver-1",
                        "retrieval_document_id": "doc-1",
                        "title": "Authorisation handling",
                        "body": "Authorisation runs before ranking.",
                        "inclusion_reason": "exact match: auth",
                        "scope_evidence": {"visibility_scope": "project"},
                        "matched_terms": ["auth"],
                    },
                ],
                "warnings": [],
            }
            transport = FakeTransport([(200, search_response)])
            exit_code, stdout, stderr = self.run_cli(
                [
                    "search",
                    "--query",
                    "auth ranking",
                    "--limit",
                    "3",
                    "--config-dir",
                    str(config_dir),
                ],
                transport,
            )

            self.assertEqual(0, exit_code, stderr)
            self.assertEqual(1, len(transport.calls))
            call = transport.calls[0]
            self.assertEqual("https://engram.example/v1/search/", call["url"])
            self.assertEqual("POST", call["method"])
            self.assertEqual("auth ranking", call["payload"]["query"])
            self.assertEqual(3, call["payload"]["limit"])
            self.assertEqual(PROJECT_ID, call["payload"]["project_id"])
            self.assertEqual(TEAM_ID, call["payload"]["team_id"])
            self.assertEqual(f"Bearer {RAW_KEY}", call["headers"]["Authorization"])
            self.assertIn("Authorisation handling", stdout)
            self.assertIn("M1", stdout)
            self.assertNotIn(RAW_KEY, stdout)
            self.assertNotIn(RAW_KEY, stderr)

    def test_search_prints_kind_and_confidence_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            search_response = {
                "request_id": "request-search-2",
                "items": [
                    {
                        "citation": "M1",
                        "memory_id": "mem-1",
                        "title": "Authorisation handling",
                        "body": "Authorisation runs before ranking.",
                        "kind": "gotcha",
                        "confidence": "0.950",
                    },
                ],
                "warnings": [],
            }
            transport = FakeTransport([(200, search_response)])
            exit_code, stdout, stderr = self.run_cli(
                [
                    "search",
                    "--query",
                    "auth ranking",
                    "--config-dir",
                    str(config_dir),
                ],
                transport,
            )

            self.assertEqual(0, exit_code, stderr)
            self.assertIn(
                "M1: Authorisation handling [gotcha, conf 0.950]", stdout
            )

    def test_search_as_json_omits_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            search_response = {
                "request_id": "request-search-3",
                "items": [
                    {
                        "citation": "M1",
                        "memory_id": "mem-1",
                        "title": "Authorisation handling",
                        "body": "Authorisation runs before ranking.",
                        "kind": "gotcha",
                        "confidence": "0.950",
                    },
                ],
                "warnings": [],
            }
            transport = FakeTransport([(200, search_response)])
            exit_code, stdout, stderr = self.run_cli(
                [
                    "search",
                    "--query",
                    "auth ranking",
                    "--json",
                    "--config-dir",
                    str(config_dir),
                ],
                transport,
            )

            self.assertEqual(0, exit_code, stderr)
            body = json.loads(stdout)
            self.assertEqual("gotcha", body["items"][0]["kind"])
            self.assertNotIn("conf 0.950", stdout)

    def test_search_project_flag_wins_over_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            transport = FakeTransport(
                [(200, {"request_id": "r", "items": [], "warnings": []})]
            )
            exit_code, _stdout, stderr = self.run_cli(
                [
                    "search",
                    "--query",
                    "auth",
                    "--project",
                    "flag-project",
                    "--config-dir",
                    str(config_dir),
                ],
                transport,
            )

            self.assertEqual(0, exit_code, stderr)
            call = transport.calls[0]
            self.assertEqual("flag-project", call["payload"]["project_id"])

    def test_search_env_project_id_wins_over_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            transport = FakeTransport(
                [(200, {"request_id": "r", "items": [], "warnings": []})]
            )
            with mock.patch.dict(os.environ, {"ENGRAM_PROJECT_ID": "env-project"}):
                exit_code, _stdout, stderr = self.run_cli(
                    ["search", "--query", "auth", "--config-dir", str(config_dir)],
                    transport,
                )

            self.assertEqual(0, exit_code, stderr)
            call = transport.calls[0]
            self.assertEqual("env-project", call["payload"]["project_id"])

    def test_search_reports_missing_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            transport = FakeTransport([])
            exit_code, _stdout, stderr = self.run_cli(
                ["search", "--query", "auth", "--config-dir", str(config_dir)],
                transport,
            )

            self.assertEqual(1, exit_code)
            self.assertEqual(0, len(transport.calls))
            self.assertIn("missing_config", stderr)

    def test_search_prints_empty_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            transport = FakeTransport(
                [(200, {"request_id": "r", "items": [], "warnings": []})]
            )
            exit_code, stdout, stderr = self.run_cli(
                ["search", "--query", "nothing", "--config-dir", str(config_dir)],
                transport,
            )

            self.assertEqual(0, exit_code, stderr)
            self.assertIn("No memory matched", stdout)

    def test_memory_version_posts_body_and_prints_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            response = {
                "memory_id": "mem-1",
                "project_id": PROJECT_ID,
                "team_id": TEAM_ID,
                "current_version": 2,
                "memory_version_id": "ver-2",
                "retrieval_document_id": "doc-2",
            }
            transport = FakeTransport([(200, response)])
            exit_code, stdout, stderr = self.run_cli(
                [
                    "memory",
                    "version",
                    "mem-1",
                    "--body",
                    "Updated body",
                    "--config-dir",
                    str(config_dir),
                ],
                transport,
            )

            self.assertEqual(0, exit_code, stderr)
            self.assertEqual(1, len(transport.calls))
            call = transport.calls[0]
            self.assertEqual(
                "https://engram.example/v1/memories/mem-1/version", call["url"]
            )
            self.assertEqual("Updated body", call["payload"]["body"])
            self.assertEqual(PROJECT_ID, call["payload"]["project_id"])
            self.assertIn("current_version=2", stdout)
            self.assertNotIn(RAW_KEY, stdout)

    def test_memory_version_project_flag_wins_over_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            response = {
                "memory_id": "mem-1",
                "current_version": 2,
                "memory_version_id": "ver-2",
            }
            transport = FakeTransport([(200, response)])
            exit_code, _stdout, stderr = self.run_cli(
                [
                    "memory",
                    "version",
                    "mem-1",
                    "--body",
                    "Updated body",
                    "--project",
                    "flag-project",
                    "--config-dir",
                    str(config_dir),
                ],
                transport,
            )

            self.assertEqual(0, exit_code, stderr)
            call = transport.calls[0]
            self.assertEqual("flag-project", call["payload"]["project_id"])

    def test_memory_version_falls_back_to_repository_url_without_project(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            connect_transport = FakeTransport(
                [(200, dry_run_ok()), (200, dry_run_ok())]
            )
            exit_code, _stdout, stderr = self.run_cli(
                [
                    "connect",
                    "--server",
                    "https://engram.example",
                    "--api-key",
                    RAW_KEY,
                    "--config-dir",
                    str(config_dir),
                ],
                connect_transport,
            )
            self.assertEqual(0, exit_code, stderr)

            response = {
                "memory_id": "mem-1",
                "current_version": 2,
                "memory_version_id": "ver-2",
            }
            transport = FakeTransport([(200, response)])
            with mock.patch(
                "engram_cli.commands.git_remote_url",
                return_value="git@github.com:acme/x.git",
            ):
                exit_code, _stdout, stderr = self.run_cli(
                    [
                        "memory",
                        "version",
                        "mem-1",
                        "--body",
                        "Updated body",
                        "--config-dir",
                        str(config_dir),
                    ],
                    transport,
                )

            self.assertEqual(0, exit_code, stderr)
            call = transport.calls[0]
            self.assertNotIn("project_id", call["payload"])
            self.assertEqual(
                "git@github.com:acme/x.git", call["payload"]["repository_url"]
            )

    def test_memory_version_reports_missing_project_when_nothing_resolves(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            connect_transport = FakeTransport(
                [(200, dry_run_ok()), (200, dry_run_ok())]
            )
            exit_code, _stdout, stderr = self.run_cli(
                [
                    "connect",
                    "--server",
                    "https://engram.example",
                    "--api-key",
                    RAW_KEY,
                    "--config-dir",
                    str(config_dir),
                ],
                connect_transport,
            )
            self.assertEqual(0, exit_code, stderr)

            transport = FakeTransport([])
            with mock.patch("engram_cli.commands.git_remote_url", return_value=""):
                exit_code, _stdout, stderr = self.run_cli(
                    [
                        "memory",
                        "version",
                        "mem-1",
                        "--body",
                        "Updated body",
                        "--config-dir",
                        str(config_dir),
                    ],
                    transport,
                )

            self.assertEqual(1, exit_code)
            self.assertEqual(0, len(transport.calls))
            self.assertIn("missing_project", stderr)

    def test_memory_link_posts_link_and_prints_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            response = {
                "memory_id": "mem-1",
                "link_id": "link-1",
                "link_type": "file",
                "target": "apps/backend/engram/memory/services.py",
                "label": "service",
                "created": True,
            }
            transport = FakeTransport([(200, response)])
            exit_code, stdout, stderr = self.run_cli(
                [
                    "memory",
                    "link",
                    "mem-1",
                    "--link-type",
                    "file",
                    "--target",
                    "apps/backend/engram/memory/services.py",
                    "--label",
                    "service",
                    "--config-dir",
                    str(config_dir),
                ],
                transport,
            )

            self.assertEqual(0, exit_code, stderr)
            call = transport.calls[0]
            self.assertEqual(
                "https://engram.example/v1/memories/mem-1/links", call["url"]
            )
            self.assertEqual("file", call["payload"]["link_type"])
            self.assertIn("link_id=link-1", stdout)
            self.assertIn("apps/backend/engram/memory/services.py", stdout)

    def test_memory_link_project_flag_wins_over_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            response = {
                "memory_id": "mem-1",
                "link_id": "link-1",
                "link_type": "file",
                "target": "a.py",
                "created": True,
            }
            transport = FakeTransport([(200, response)])
            exit_code, _stdout, stderr = self.run_cli(
                [
                    "memory",
                    "link",
                    "mem-1",
                    "--link-type",
                    "file",
                    "--target",
                    "a.py",
                    "--project",
                    "flag-project",
                    "--config-dir",
                    str(config_dir),
                ],
                transport,
            )

            self.assertEqual(0, exit_code, stderr)
            call = transport.calls[0]
            self.assertEqual("flag-project", call["payload"]["project_id"])

    def test_memory_link_falls_back_to_repository_url_without_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            connect_transport = FakeTransport(
                [(200, dry_run_ok()), (200, dry_run_ok())]
            )
            exit_code, _stdout, stderr = self.run_cli(
                [
                    "connect",
                    "--server",
                    "https://engram.example",
                    "--api-key",
                    RAW_KEY,
                    "--config-dir",
                    str(config_dir),
                ],
                connect_transport,
            )
            self.assertEqual(0, exit_code, stderr)

            response = {
                "memory_id": "mem-1",
                "link_id": "link-1",
                "link_type": "file",
                "target": "a.py",
                "created": True,
            }
            transport = FakeTransport([(200, response)])
            with mock.patch(
                "engram_cli.commands.git_remote_url",
                return_value="git@github.com:acme/x.git",
            ):
                exit_code, _stdout, stderr = self.run_cli(
                    [
                        "memory",
                        "link",
                        "mem-1",
                        "--link-type",
                        "file",
                        "--target",
                        "a.py",
                        "--config-dir",
                        str(config_dir),
                    ],
                    transport,
                )

            self.assertEqual(0, exit_code, stderr)
            call = transport.calls[0]
            self.assertNotIn("project_id", call["payload"])
            self.assertEqual(
                "git@github.com:acme/x.git", call["payload"]["repository_url"]
            )

    def test_memory_link_reports_missing_project_when_nothing_resolves(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            connect_transport = FakeTransport(
                [(200, dry_run_ok()), (200, dry_run_ok())]
            )
            exit_code, _stdout, stderr = self.run_cli(
                [
                    "connect",
                    "--server",
                    "https://engram.example",
                    "--api-key",
                    RAW_KEY,
                    "--config-dir",
                    str(config_dir),
                ],
                connect_transport,
            )
            self.assertEqual(0, exit_code, stderr)

            transport = FakeTransport([])
            with mock.patch("engram_cli.commands.git_remote_url", return_value=""):
                exit_code, _stdout, stderr = self.run_cli(
                    [
                        "memory",
                        "link",
                        "mem-1",
                        "--link-type",
                        "file",
                        "--target",
                        "a.py",
                        "--config-dir",
                        str(config_dir),
                    ],
                    transport,
                )

            self.assertEqual(1, exit_code)
            self.assertEqual(0, len(transport.calls))
            self.assertIn("missing_project", stderr)

    def test_memory_links_lists_recorded_links(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            response = {
                "items": [
                    {
                        "link_id": "link-1",
                        "link_type": "file",
                        "target": "apps/backend/engram/memory/services.py",
                        "label": "",
                        "created_at": "2026-06-26T00:00:00Z",
                    },
                ],
            }
            transport = FakeTransport([(200, response)])
            exit_code, stdout, stderr = self.run_cli(
                ["memory", "links", "mem-1", "--config-dir", str(config_dir)],
                transport,
            )

            self.assertEqual(0, exit_code, stderr)
            call = transport.calls[0]
            self.assertEqual("GET", call["method"])
            self.assertIn("/v1/memories/mem-1/links", call["url"])
            self.assertIn("project_id=", call["url"])
            self.assertIn("file: apps/backend/engram/memory/services.py", stdout)

    def test_memory_links_project_flag_wins_over_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            transport = FakeTransport([(200, {"items": []})])
            exit_code, _stdout, stderr = self.run_cli(
                [
                    "memory",
                    "links",
                    "mem-1",
                    "--project",
                    "flag-project",
                    "--config-dir",
                    str(config_dir),
                ],
                transport,
            )

            self.assertEqual(0, exit_code, stderr)
            call = transport.calls[0]
            self.assertIn("project_id=flag-project", call["url"])

    def test_memory_links_falls_back_to_repository_url_without_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            connect_transport = FakeTransport(
                [(200, dry_run_ok()), (200, dry_run_ok())]
            )
            exit_code, _stdout, stderr = self.run_cli(
                [
                    "connect",
                    "--server",
                    "https://engram.example",
                    "--api-key",
                    RAW_KEY,
                    "--config-dir",
                    str(config_dir),
                ],
                connect_transport,
            )
            self.assertEqual(0, exit_code, stderr)

            transport = FakeTransport([(200, {"items": []})])
            with mock.patch(
                "engram_cli.commands.git_remote_url",
                return_value="git@github.com:acme/x.git",
            ):
                exit_code, _stdout, stderr = self.run_cli(
                    ["memory", "links", "mem-1", "--config-dir", str(config_dir)],
                    transport,
                )

            self.assertEqual(0, exit_code, stderr)
            call = transport.calls[0]
            self.assertIn("repository_url=", call["url"])
            self.assertNotIn("project_id=", call["url"])

    def test_memory_links_reports_missing_project_when_nothing_resolves(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            connect_transport = FakeTransport(
                [(200, dry_run_ok()), (200, dry_run_ok())]
            )
            exit_code, _stdout, stderr = self.run_cli(
                [
                    "connect",
                    "--server",
                    "https://engram.example",
                    "--api-key",
                    RAW_KEY,
                    "--config-dir",
                    str(config_dir),
                ],
                connect_transport,
            )
            self.assertEqual(0, exit_code, stderr)

            transport = FakeTransport([])
            with mock.patch("engram_cli.commands.git_remote_url", return_value=""):
                exit_code, _stdout, stderr = self.run_cli(
                    ["memory", "links", "mem-1", "--config-dir", str(config_dir)],
                    transport,
                )

            self.assertEqual(1, exit_code)
            self.assertEqual(0, len(transport.calls))
            self.assertIn("missing_project", stderr)

    def test_observations_project_flag_wins_over_env_and_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            transport = FakeTransport([(200, {"items": []})])
            with mock.patch.dict(os.environ, {"ENGRAM_PROJECT_ID": "env-project"}):
                exit_code, _stdout, stderr = self.run_cli(
                    [
                        "observations",
                        "--project",
                        "flag-project",
                        "--config-dir",
                        str(config_dir),
                    ],
                    transport,
                )

            self.assertEqual(0, exit_code, stderr)
            call = transport.calls[0]
            self.assertIn("project_id=flag-project", call["url"])
            self.assertNotIn("env-project", call["url"])
            self.assertNotIn(PROJECT_ID, call["url"])

    def test_observations_lists_recorded_observations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            response = {
                "request_id": "observations-1",
                "items": [
                    {
                        "observation_type": "tool_use",
                        "title": "Obs one",
                        "body": "Obs body one.",
                    },
                ],
                "warnings": [],
            }
            transport = FakeTransport([(200, response)])
            exit_code, stdout, stderr = self.run_cli(
                ["observations", "--config-dir", str(config_dir)],
                transport,
            )

            self.assertEqual(0, exit_code, stderr)
            call = transport.calls[0]
            self.assertEqual("GET", call["method"])
            self.assertIn("/v1/observations/", call["url"])
            self.assertIn("project_id=", call["url"])
            self.assertIn("Obs one", stdout)

    def test_observations_project_flag_wins_over_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            transport = FakeTransport([(200, {"items": []})])
            exit_code, _stdout, stderr = self.run_cli(
                [
                    "observations",
                    "--project",
                    "flag-project",
                    "--config-dir",
                    str(config_dir),
                ],
                transport,
            )

            self.assertEqual(0, exit_code, stderr)
            call = transport.calls[0]
            self.assertIn("project_id=flag-project", call["url"])

    def test_observations_env_project_id_wins_over_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            transport = FakeTransport([(200, {"items": []})])
            with mock.patch.dict(os.environ, {"ENGRAM_PROJECT_ID": "env-project"}):
                exit_code, _stdout, stderr = self.run_cli(
                    ["observations", "--config-dir", str(config_dir)],
                    transport,
                )

            self.assertEqual(0, exit_code, stderr)
            call = transport.calls[0]
            self.assertIn("project_id=env-project", call["url"])

    def test_observations_falls_back_to_repository_url_without_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            connect_transport = FakeTransport(
                [(200, dry_run_ok()), (200, dry_run_ok())]
            )
            exit_code, _stdout, stderr = self.run_cli(
                [
                    "connect",
                    "--server",
                    "https://engram.example",
                    "--api-key",
                    RAW_KEY,
                    "--config-dir",
                    str(config_dir),
                ],
                connect_transport,
            )
            self.assertEqual(0, exit_code, stderr)

            transport = FakeTransport([(200, {"items": []})])
            with mock.patch(
                "engram_cli.commands.git_remote_url",
                return_value="git@github.com:acme/x.git",
            ):
                exit_code, _stdout, stderr = self.run_cli(
                    ["observations", "--config-dir", str(config_dir)],
                    transport,
                )

            self.assertEqual(0, exit_code, stderr)
            call = transport.calls[0]
            self.assertIn("repository_url=", call["url"])
            self.assertNotIn("project_id=", call["url"])

    def test_observations_reports_missing_project_when_nothing_resolves(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            connect_transport = FakeTransport(
                [(200, dry_run_ok()), (200, dry_run_ok())]
            )
            exit_code, _stdout, stderr = self.run_cli(
                [
                    "connect",
                    "--server",
                    "https://engram.example",
                    "--api-key",
                    RAW_KEY,
                    "--config-dir",
                    str(config_dir),
                ],
                connect_transport,
            )
            self.assertEqual(0, exit_code, stderr)

            transport = FakeTransport([])
            with mock.patch("engram_cli.commands.git_remote_url", return_value=""):
                exit_code, _stdout, stderr = self.run_cli(
                    ["observations", "--config-dir", str(config_dir)],
                    transport,
                )

            self.assertEqual(1, exit_code)
            self.assertEqual(0, len(transport.calls))
            self.assertIn("missing_project", stderr)


class WorkspaceRepositoryUrlTests(unittest.TestCase):
    def setUp(self) -> None:
        self._claude_project_dir = os.environ.pop("CLAUDE_PROJECT_DIR", None)
        self._cwd = os.getcwd()

    def tearDown(self) -> None:
        os.chdir(self._cwd)
        if self._claude_project_dir is not None:
            os.environ["CLAUDE_PROJECT_DIR"] = self._claude_project_dir
        else:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)

    def init_repo(self, path: Path, remote_url: str) -> None:
        subprocess.run(["git", "init", "-q", str(path)], check=True)
        subprocess.run(
            ["git", "-C", str(path), "remote", "add", "origin", remote_url],
            check=True,
        )

    def test_claude_project_dir_beats_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as cwd_repo, tempfile.TemporaryDirectory() as claude_repo:
            self.init_repo(Path(cwd_repo), "git@github.com:acme/cwd-repo.git")
            self.init_repo(Path(claude_repo), "git@github.com:acme/claude-repo.git")
            os.environ["CLAUDE_PROJECT_DIR"] = claude_repo
            os.chdir(cwd_repo)

            url = workspace_repository_url()

        self.assertEqual("git@github.com:acme/claude-repo.git", url)

    def test_cwd_fallback_when_claude_project_dir_unset(self) -> None:
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        with tempfile.TemporaryDirectory() as cwd_repo:
            self.init_repo(Path(cwd_repo), "git@github.com:acme/cwd-repo.git")
            os.chdir(cwd_repo)

            url = workspace_repository_url()

        self.assertEqual("git@github.com:acme/cwd-repo.git", url)

    def test_git_remote_url_strips_https_userinfo(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            self.init_repo(Path(repo), "https://user:token@github.com/acme/x.git")
            url = git_remote_url(repo)

        self.assertEqual("https://github.com/acme/x.git", url)

    def test_git_remote_url_leaves_scp_form_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            self.init_repo(Path(repo), "git@github.com:acme/x.git")
            url = git_remote_url(repo)

        self.assertEqual("git@github.com:acme/x.git", url)

    def test_git_remote_url_strips_password_only_userinfo(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            self.init_repo(Path(repo), "https://:TOKEN@github.com/acme/x.git")
            url = git_remote_url(repo)

        self.assertEqual("https://github.com/acme/x.git", url)
        self.assertNotIn("TOKEN", url)


class StubPrompt:
    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []

    def __call__(self, message: str) -> str:
        self.prompts.append(message)
        if not self.answers:
            raise AssertionError(f"unexpected prompt: {message}")

        return self.answers.pop(0)


def login_ok() -> dict[str, object]:
    return {
        "token": DRF_TOKEN,
        "user_id": 1,
        "username": "alice",
        "identity_id": "ident-1",
        "organization_id": ORG_ID,
        "capabilities": ["memories:read", "observations:write", "search:query"],
    }


def org_list() -> dict[str, object]:
    return {
        "count": 1,
        "results": [
            {"id": ORG_ID, "name": "Acme", "slug": "acme"},
        ],
    }


def project_list() -> dict[str, object]:
    return {
        "count": 1,
        "results": [
            {
                "id": PROJECT_ID,
                "name": "Demo",
                "slug": "demo",
                "repository_url": "",
                "default_branch": "main",
            },
        ],
    }


def api_key_issued() -> dict[str, object]:
    return {
        "id": "api-key-1",
        "name": "cli-key",
        "key_prefix": "egk_live_abcde",
        "key_fingerprint": "sha256:abc",
        "plaintext": RAW_KEY,
        "capabilities": ["memories:read", "observations:write", "search:query"],
        "created_at": "2026-06-27T00:00:00Z",
    }


def health_ok() -> dict[str, object]:
    return {"status": "ok", "checks": {"process": "ok"}}


class WizardTests(unittest.TestCase):
    def run_connect_wizard(
        self,
        config_dir: Path,
        responses: list[tuple[int, dict[str, object]]],
        answers: list[str],
    ) -> tuple[int, str, str, FakeTransport]:
        transport = FakeTransport(responses)
        stdout = io.StringIO()
        stderr = io.StringIO()
        args = main.build_parser().parse_args(
            ["connect", "--config-dir", str(config_dir)]
        )
        exit_code = run_connect(
            args,
            stdout,
            stderr,
            transport,
            prompt=StubPrompt(answers),
            interactive=True,
        )

        return exit_code, stdout.getvalue(), stderr.getvalue(), transport

    def test_wizard_full_flow_probes_logins_lists_and_writes_local_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            exit_code, stdout, stderr, transport = self.run_connect_wizard(
                config_dir,
                [
                    (200, health_ok()),
                    (200, login_ok()),
                    (200, org_list()),
                    (200, project_list()),
                    (201, api_key_issued()),
                ],
                ["", "alice", "secret", "1", "1", "cli-key"],
            )

            self.assertEqual(0, exit_code, stderr)
            methods = [call["method"] for call in transport.calls]
            self.assertEqual(
                ["GET", "POST", "GET", "GET", "POST"], methods
            )
            self.assertEqual(
                "http://localhost:8000/-/healthz/", transport.calls[0]["url"]
            )
            self.assertEqual(
                "http://localhost:8000/v1/auth/login", transport.calls[1]["url"]
            )
            self.assertEqual(
                {"username": "alice", "password": "secret"},
                transport.calls[1]["payload"],
            )
            self.assertEqual(
                "http://localhost:8000/v1/admin/organizations/",
                transport.calls[2]["url"],
            )
            self.assertEqual(
                f"Token {DRF_TOKEN}",
                transport.calls[2]["headers"]["Authorization"],
            )
            self.assertEqual(
                "http://localhost:8000/v1/admin/projects/",
                transport.calls[3]["url"],
            )
            self.assertEqual(
                ORG_ID,
                transport.calls[3]["headers"]["X-Engram-Organization"],
            )
            self.assertEqual(
                f"Token {DRF_TOKEN}",
                transport.calls[3]["headers"]["Authorization"],
            )
            issue_call = transport.calls[4]
            self.assertEqual(
                "http://localhost:8000/v1/admin/api-keys/", issue_call["url"]
            )
            self.assertEqual(
                ORG_ID,
                issue_call["headers"]["X-Engram-Organization"],
            )
            self.assertEqual(
                f"Token {DRF_TOKEN}", issue_call["headers"]["Authorization"]
            )
            self.assertEqual("cli-key", issue_call["payload"]["name"])
            self.assertEqual(
                [
                    "memories:read",
                    "observations:write",
                    "search:query",
                ],
                issue_call["payload"]["capabilities"],
            )

            config = read_json(config_dir / "config.json")
            credentials = read_json(config_dir / "credentials.json")
            self.assertEqual(
                "http://localhost:8000", config["server_url"]
            )
            self.assertEqual(ORG_ID, config["organization_id"])
            self.assertEqual(PROJECT_ID, config["project_id"])
            self.assertEqual(RAW_KEY, credentials["api_key"])
            self.assertEqual(
                0o600,
                stat.S_IMODE((config_dir / "credentials.json").stat().st_mode),
            )
            self.assertTrue(
                (config_dir / "hooks" / "codex.json").exists()
            )
            self.assertTrue(
                (config_dir / "hooks" / "claude_code.json").exists()
            )
            self.assertIn("connected", stdout)
            self.assertIn(PROJECT_ID, stdout)
            self.assertNotIn("secret", stdout)
            self.assertNotIn(DRF_TOKEN, stdout)
            self.assertNotIn(RAW_KEY, stdout)
            self.assertNotIn("Traceback", stderr)

    def test_wizard_reprompts_server_when_health_check_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            exit_code, stdout, stderr, transport = self.run_connect_wizard(
                config_dir,
                [
                    (503, {"status": "unavailable"}),
                    (200, health_ok()),
                    (200, login_ok()),
                    (200, org_list()),
                    (200, project_list()),
                    (201, api_key_issued()),
                ],
                [
                    "http://broken.example",
                    "http://localhost:8000",
                    "alice",
                    "secret",
                    "1",
                    "1",
                    "cli-key",
                ],
            )

            self.assertEqual(0, exit_code, stderr)
            self.assertEqual(6, len(transport.calls))
            self.assertEqual(
                "http://broken.example/-/healthz/", transport.calls[0]["url"]
            )
            self.assertEqual(
                "http://localhost:8000/-/healthz/", transport.calls[1]["url"]
            )
            self.assertIn("Could not reach", stderr)

    def test_wizard_surfaces_login_failure_and_retries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            exit_code, stdout, stderr, transport = self.run_connect_wizard(
                config_dir,
                [
                    (200, health_ok()),
                    (401, {"code": "invalid_credentials", "detail": "Bad creds"}),
                    (200, login_ok()),
                    (200, org_list()),
                    (200, project_list()),
                    (201, api_key_issued()),
                ],
                [
                    "",
                    "alice",
                    "wrong",
                    "alice",
                    "secret",
                    "1",
                    "1",
                    "cli-key",
                ],
            )

            self.assertEqual(0, exit_code, stderr)
            login_calls = [
                call
                for call in transport.calls
                if call["url"].endswith("/v1/auth/login")
            ]
            self.assertEqual(2, len(login_calls))
            self.assertEqual("wrong", login_calls[0]["payload"]["password"])
            self.assertEqual("secret", login_calls[1]["payload"]["password"])
            self.assertIn("Login failed", stderr)

    def test_wizard_login_failure_without_retry_surfaces_error_and_writes_nothing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            exit_code, stdout, stderr, transport = self.run_connect_wizard(
                config_dir,
                [
                    (200, health_ok()),
                    (
                        401,
                        {"code": "invalid_credentials", "detail": "Bad creds"},
                    ),
                    (
                        401,
                        {"code": "invalid_credentials", "detail": "Bad creds"},
                    ),
                    (
                        401,
                        {"code": "invalid_credentials", "detail": "Bad creds"},
                    ),
                ],
                [
                    "",
                    "alice",
                    "wrong1",
                    "alice",
                    "wrong2",
                    "alice",
                    "wrong3",
                ],
            )

            self.assertEqual(1, exit_code)
            self.assertIn("invalid_credentials", stderr)
            self.assertEqual([], list(config_dir.rglob("*")))

    def test_wizard_preserves_non_interactive_flag_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            transport = FakeTransport([(200, dry_run_ok()), (200, dry_run_ok())])
            stdout = io.StringIO()
            stderr = io.StringIO()
            args = main.build_parser().parse_args(
                [
                    "connect",
                    "--server",
                    "https://engram.example",
                    "--api-key",
                    RAW_KEY,
                    "--project",
                    PROJECT_ID,
                    "--config-dir",
                    str(config_dir),
                ]
            )
            exit_code = run_connect(
                args,
                stdout,
                stderr,
                transport,
                prompt=StubPrompt(["should-not-be-called"]),
                interactive=False,
            )

            self.assertEqual(0, exit_code, stderr)
            self.assertEqual(2, len(transport.calls))
            self.assertEqual(
                "https://engram.example/v1/hooks/dry-run",
                transport.calls[0]["url"],
            )

    def test_wizard_non_interactive_missing_flags_errors_without_prompt(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            transport = FakeTransport([])
            stdout = io.StringIO()
            stderr = io.StringIO()
            args = main.build_parser().parse_args(
                ["connect", "--config-dir", tmp]
            )
            exit_code = run_connect(
                args,
                stdout,
                stderr,
                transport,
                prompt=StubPrompt(["should-not-be-called"]),
                interactive=False,
            )

            self.assertEqual(1, exit_code)
            self.assertIn("missing_server_url", stderr.getvalue())
            self.assertEqual([], transport.calls)


class McpInstallTests(unittest.TestCase):
    def run_cli(
        self,
        argv: list[str],
        transport: FakeTransport,
    ) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()

        exit_code = main.main(
            argv, stdin=None, stdout=stdout, stderr=stderr, transport=transport
        )

        return exit_code, stdout.getvalue(), stderr.getvalue()

    def connect(self, config_dir: Path) -> None:
        transport = FakeTransport(
            [(200, dry_run_ok()), (200, dry_run_ok())]
        )
        exit_code, _stdout, stderr = self.run_cli(
            [
                "connect",
                "--server",
                "https://engram.example/",
                "--api-key",
                RAW_KEY,
                "--project",
                PROJECT_ID,
                "--team",
                TEAM_ID,
                "--config-dir",
                str(config_dir),
            ],
            transport,
        )
        self.assertEqual(0, exit_code, stderr)

    def test_mcp_install_default_writes_engram_entry_to_claude_code_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            claude_code_config = config_dir / "claude.json"

            with mock.patch(
                "engram_cli.commands.shutil.which", return_value=None
            ):
                exit_code, stdout, stderr = self.run_cli(
                    [
                        "mcp-install",
                        "--config-dir",
                        str(config_dir),
                        "--claude-code-config",
                        str(claude_code_config),
                    ],
                    FakeTransport([]),
                )

            self.assertEqual(0, exit_code, stderr)
            self.assertTrue(claude_code_config.exists())
            data = read_json(claude_code_config)
            servers = data["mcpServers"]
            self.assertIn("engram", servers)
            entry = servers["engram"]
            self.assertEqual(sys.executable, entry["command"])
            self.assertEqual(
                ["-m", "engram_cli", "mcp", "serve", "--config-dir", str(config_dir)],
                entry["args"],
            )
            self.assertNotIn("env", entry)
            self.assertNotIn(RAW_KEY, json.dumps(data))
            self.assertIn("engram", stdout)
            self.assertNotIn(RAW_KEY, stdout)
            self.assertNotIn(RAW_KEY, stderr)

    def test_mcp_install_creates_file_with_mcp_servers_when_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            claude_code_config = config_dir / "claude.json"

            with mock.patch(
                "engram_cli.commands.shutil.which", return_value=None
            ):
                exit_code, _stdout, stderr = self.run_cli(
                    [
                        "mcp-install",
                        "--agent",
                        "claude_code",
                        "--config-dir",
                        str(config_dir),
                        "--claude-code-config",
                        str(claude_code_config),
                    ],
                    FakeTransport([]),
                )

            self.assertEqual(0, exit_code, stderr)
            data = read_json(claude_code_config)
            self.assertIn("mcpServers", data)
            self.assertIn("engram", data["mcpServers"])

    def test_mcp_install_merges_engram_entry_without_duplicating_or_dropping_existing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            claude_code_config = config_dir / "claude.json"
            claude_code_config.parent.mkdir(parents=True, exist_ok=True)
            existing = {
                "mcpServers": {
                    "other": {
                        "command": "node",
                        "args": ["other.js"],
                    },
                    "engram": {
                        "command": "stale",
                        "args": ["old"],
                        "env": {"ENGRAM_API_KEY": "old-key"},
                    },
                },
                "theme": "dark",
            }
            claude_code_config.write_text(
                json.dumps(existing), encoding="utf-8"
            )

            with mock.patch(
                "engram_cli.commands.shutil.which", return_value=None
            ):
                exit_code, _stdout, stderr = self.run_cli(
                    [
                        "mcp-install",
                        "--agent",
                        "claude_code",
                        "--config-dir",
                        str(config_dir),
                        "--claude-code-config",
                        str(claude_code_config),
                    ],
                    FakeTransport([]),
                )

            self.assertEqual(0, exit_code, stderr)
            data = read_json(claude_code_config)
            self.assertEqual("dark", data["theme"])
            servers = data["mcpServers"]
            self.assertIn("other", servers)
            self.assertEqual("node", servers["other"]["command"])
            self.assertIn("engram", servers)
            entry = servers["engram"]
            self.assertEqual(sys.executable, entry["command"])
            self.assertEqual(
                ["-m", "engram_cli", "mcp", "serve", "--config-dir", str(config_dir)],
                entry["args"],
            )
            self.assertNotIn("env", entry)
            self.assertNotIn(RAW_KEY, json.dumps(data))
            engram_entries = [
                name for name in servers if name == "engram"
            ]
            self.assertEqual(1, len(engram_entries))

    def test_mcp_install_is_idempotent_on_repeat_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            claude_code_config = config_dir / "claude.json"

            with mock.patch(
                "engram_cli.commands.shutil.which", return_value=None
            ):
                for _ in range(2):
                    exit_code, _stdout, stderr = self.run_cli(
                        [
                            "mcp-install",
                            "--agent",
                            "claude_code",
                            "--config-dir",
                            str(config_dir),
                            "--claude-code-config",
                            str(claude_code_config),
                        ],
                        FakeTransport([]),
                    )
                    self.assertEqual(0, exit_code, stderr)

            data = read_json(claude_code_config)
            servers = data["mcpServers"]
            self.assertEqual(
                ["engram"], [name for name in servers if name == "engram"]
            )

    def test_mcp_install_agent_flag_claude_desktop_writes_desktop_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            desktop_dir = config_dir / "Claude"
            desktop_config = desktop_dir / "claude_desktop_config.json"

            with mock.patch(
                "engram_cli.commands.shutil.which", return_value=None
            ):
                exit_code, _stdout, stderr = self.run_cli(
                    [
                        "mcp-install",
                        "--agent",
                        "claude_desktop",
                        "--config-dir",
                        str(config_dir),
                        "--claude-desktop-config",
                        str(desktop_config),
                    ],
                    FakeTransport([]),
                )

            self.assertEqual(0, exit_code, stderr)
            self.assertTrue(desktop_config.exists())
            data = read_json(desktop_config)
            entry = data["mcpServers"]["engram"]
            self.assertEqual(sys.executable, entry["command"])
            self.assertEqual(
                ["-m", "engram_cli", "mcp", "serve", "--config-dir", str(config_dir)],
                entry["args"],
            )
            self.assertNotIn("env", entry)
            self.assertNotIn(RAW_KEY, json.dumps(data))

    def test_mcp_install_agent_flag_both_writes_both_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            claude_code_config = config_dir / "claude.json"
            desktop_config = config_dir / "claude_desktop_config.json"

            with mock.patch(
                "engram_cli.commands.shutil.which", return_value=None
            ):
                exit_code, _stdout, stderr = self.run_cli(
                    [
                        "mcp-install",
                        "--agent",
                        "both",
                        "--config-dir",
                        str(config_dir),
                        "--claude-code-config",
                        str(claude_code_config),
                        "--claude-desktop-config",
                        str(desktop_config),
                    ],
                    FakeTransport([]),
                )

            self.assertEqual(0, exit_code, stderr)
            self.assertTrue(claude_code_config.exists())
            self.assertTrue(desktop_config.exists())
            self.assertEqual(
                sys.executable,
                read_json(claude_code_config)["mcpServers"]["engram"]["command"],
            )
            self.assertEqual(
                sys.executable,
                read_json(desktop_config)["mcpServers"]["engram"]["command"],
            )

    def test_mcp_install_entry_uses_engram_binary_when_on_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            claude_code_config = config_dir / "claude.json"

            with mock.patch(
                "engram_cli.commands.shutil.which",
                return_value="/usr/local/bin/engram",
            ):
                exit_code, _stdout, stderr = self.run_cli(
                    [
                        "mcp",
                        "install",
                        "--agent",
                        "claude_code",
                        "--config-dir",
                        str(config_dir),
                        "--claude-code-config",
                        str(claude_code_config),
                    ],
                    FakeTransport([]),
                )

            self.assertEqual(0, exit_code, stderr)
            entry = read_json(claude_code_config)["mcpServers"]["engram"]
            self.assertEqual("/usr/local/bin/engram", entry["command"])
            self.assertEqual(
                ["mcp", "serve", "--config-dir", str(config_dir)], entry["args"]
            )
            self.assertNotIn("env", entry)

    def test_mcp_install_entry_falls_back_to_python_module(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            claude_code_config = config_dir / "claude.json"

            with mock.patch(
                "engram_cli.commands.shutil.which", return_value=None
            ):
                exit_code, _stdout, stderr = self.run_cli(
                    [
                        "mcp",
                        "install",
                        "--agent",
                        "claude_code",
                        "--config-dir",
                        str(config_dir),
                        "--claude-code-config",
                        str(claude_code_config),
                    ],
                    FakeTransport([]),
                )

            self.assertEqual(0, exit_code, stderr)
            entry = read_json(claude_code_config)["mcpServers"]["engram"]
            self.assertEqual(sys.executable, entry["command"])
            self.assertEqual(
                ["-m", "engram_cli", "mcp", "serve", "--config-dir", str(config_dir)],
                entry["args"],
            )
            self.assertNotIn("env", entry)

    def test_mcp_install_hyphen_alias_still_works(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            claude_code_config = config_dir / "claude.json"

            with mock.patch(
                "engram_cli.commands.shutil.which", return_value=None
            ):
                exit_code, _stdout, stderr = self.run_cli(
                    [
                        "mcp-install",
                        "--agent",
                        "claude_code",
                        "--config-dir",
                        str(config_dir),
                        "--claude-code-config",
                        str(claude_code_config),
                    ],
                    FakeTransport([]),
                )

            self.assertEqual(0, exit_code, stderr)
            self.assertTrue(claude_code_config.exists())
            self.assertIn("engram", read_json(claude_code_config)["mcpServers"])

    def test_mcp_install_omits_config_dir_flag_when_not_passed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            claude_code_config = config_dir / "claude.json"

            with mock.patch(
                "engram_cli.commands.shutil.which", return_value=None
            ), mock.patch.dict(os.environ, {"ENGRAM_HOME": str(config_dir)}):
                exit_code, _stdout, stderr = self.run_cli(
                    [
                        "mcp-install",
                        "--agent",
                        "claude_code",
                        "--claude-code-config",
                        str(claude_code_config),
                    ],
                    FakeTransport([]),
                )

            self.assertEqual(0, exit_code, stderr)
            entry = read_json(claude_code_config)["mcpServers"]["engram"]
            self.assertEqual(["-m", "engram_cli", "mcp", "serve"], entry["args"])
            self.assertNotIn("--config-dir", entry["args"])

    def test_mcp_serve_round_trips_initialize_and_tools_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            stdin = io.StringIO(
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
                + "\n"
                + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
                + "\n",
            )
            stdout = io.StringIO()
            stderr = io.StringIO()

            exit_code = main.main(
                ["mcp", "serve", "--config-dir", str(config_dir)],
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
            )
            lines = [json.loads(line) for line in stdout.getvalue().splitlines()]

            self.assertEqual(0, exit_code, stderr.getvalue())
            self.assertEqual(2, len(lines))
            self.assertEqual(6, len(lines[1]["result"]["tools"]))

    def test_build_engram_mcp_entry_omits_config_dir_flag_when_absent(self) -> None:
        with mock.patch(
            "engram_cli.commands.shutil.which", return_value=None
        ):
            entry = build_engram_mcp_entry()

        self.assertEqual(sys.executable, entry["command"])
        self.assertEqual(["-m", "engram_cli", "mcp", "serve"], entry["args"])
        self.assertNotIn("env", entry)

    def test_build_engram_mcp_entry_appends_config_dir_flag_when_present(self) -> None:
        with mock.patch(
            "engram_cli.commands.shutil.which", return_value=None
        ):
            entry = build_engram_mcp_entry(config_dir="/tmp/engram-config")

        self.assertEqual(
            ["-m", "engram_cli", "mcp", "serve", "--config-dir", "/tmp/engram-config"],
            entry["args"],
        )

    def test_mcp_install_reports_error_when_not_connected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            claude_code_config = config_dir / "claude.json"

            exit_code, _stdout, stderr = self.run_cli(
                [
                    "mcp-install",
                    "--config-dir",
                    str(config_dir),
                    "--claude-code-config",
                    str(claude_code_config),
                ],
                FakeTransport([]),
            )

            self.assertEqual(1, exit_code)
            self.assertFalse(claude_code_config.exists())
            self.assertIn("missing_config", stderr)
            self.assertIn("engram connect", stderr)

    def test_mcp_install_reports_error_when_credentials_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            (config_dir / "credentials.json").unlink()
            claude_code_config = config_dir / "claude.json"

            exit_code, _stdout, stderr = self.run_cli(
                [
                    "mcp-install",
                    "--config-dir",
                    str(config_dir),
                    "--claude-code-config",
                    str(claude_code_config),
                ],
                FakeTransport([]),
            )

            self.assertEqual(1, exit_code)
            self.assertIn("missing_credential", stderr)


if __name__ == "__main__":
    unittest.main()
