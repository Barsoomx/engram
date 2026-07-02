from __future__ import annotations

import json
import unittest
from datetime import datetime

from engram_cli.commands import (
    build_generic_hook_payload,
    build_session_start_hook_payload,
)

CONFIG: dict[str, object] = {"project_id": "", "team_id": "", "agent_version": ""}
REPO = "git@github.com:acme/engram.git"


class HookPayloadTests(unittest.TestCase):
    def test_empty_payload_gets_nonempty_fallback_with_prompt(self) -> None:
        built = build_generic_hook_payload(
            CONFIG,
            "claude_code",
            {"session_id": "s1", "repository_url": REPO, "prompt": "learn the repo"},
            "user_prompt_submit",
        )

        self.assertTrue(built["payload"])
        self.assertEqual("user_prompt_submit", built["payload"]["trigger"])
        self.assertEqual("learn the repo", built["payload"]["prompt"])

    def test_empty_payload_captures_tool_fields(self) -> None:
        built = build_generic_hook_payload(
            CONFIG,
            "claude_code",
            {
                "session_id": "s1",
                "repository_url": REPO,
                "tool_name": "Read",
                "tool_input": {"file_path": "a.py"},
            },
            "post_tool_use",
        )

        self.assertEqual("post_tool_use", built["payload"]["trigger"])
        self.assertEqual("Read", built["payload"]["tool_name"])
        self.assertEqual({"file_path": "a.py"}, built["payload"]["tool_input"])

    def test_explicit_payload_is_preserved(self) -> None:
        built = build_generic_hook_payload(
            CONFIG,
            "claude_code",
            {"session_id": "s1", "repository_url": REPO, "payload": {"custom": 1}},
            "post_tool_use",
        )

        self.assertEqual({"custom": 1}, built["payload"])


class ObservationSynthesisTests(unittest.TestCase):
    def test_user_prompt_submit_body_carries_prompt(self) -> None:
        built = build_generic_hook_payload(
            CONFIG,
            "claude_code",
            {"session_id": "s1", "repository_url": REPO, "prompt": "learn the repo"},
            "user_prompt_submit",
        )

        observation = built["observation"]
        self.assertEqual("user_prompt_submit", observation["type"])
        self.assertIn("learn the repo", observation["body"])

    def test_post_tool_use_read_body_and_files_read(self) -> None:
        built = build_generic_hook_payload(
            CONFIG,
            "claude_code",
            {
                "session_id": "s1",
                "repository_url": REPO,
                "tool_name": "Read",
                "tool_input": {"file_path": "/repo/a.py"},
                "tool_response": {"output": "file contents"},
            },
            "post_tool_use",
        )

        observation = built["observation"]
        self.assertEqual("post_tool_use", observation["type"])
        self.assertIn("Read", observation["body"])
        self.assertIn("/repo/a.py", observation["body"])
        self.assertIn("file contents", observation["body"])
        self.assertEqual(["/repo/a.py"], observation["files_read"])
        self.assertNotIn("files_modified", observation)

    def test_write_tools_report_files_modified(self) -> None:
        for tool_name in ("Edit", "Write", "MultiEdit"):
            built = build_generic_hook_payload(
                CONFIG,
                "claude_code",
                {
                    "session_id": "s1",
                    "repository_url": REPO,
                    "tool_name": tool_name,
                    "tool_input": {"file_path": "/repo/b.py"},
                },
                "post_tool_use",
            )

            observation = built["observation"]
            self.assertEqual(["/repo/b.py"], observation["files_modified"])
            self.assertNotIn("files_read", observation)

    def test_notebook_edit_uses_notebook_path(self) -> None:
        built = build_generic_hook_payload(
            CONFIG,
            "claude_code",
            {
                "session_id": "s1",
                "repository_url": REPO,
                "tool_name": "NotebookEdit",
                "tool_input": {"notebook_path": "/repo/n.ipynb"},
            },
            "post_tool_use",
        )

        self.assertEqual(["/repo/n.ipynb"], built["observation"]["files_modified"])

    def test_body_truncated_to_server_limit(self) -> None:
        built = build_generic_hook_payload(
            CONFIG,
            "claude_code",
            {
                "session_id": "s1",
                "repository_url": REPO,
                "tool_name": "Bash",
                "tool_response": {"output": "x" * 50000},
            },
            "post_tool_use",
        )

        self.assertLessEqual(len(built["observation"]["body"]), 16000)

    def test_no_observation_when_nothing_to_describe(self) -> None:
        built = build_generic_hook_payload(
            CONFIG,
            "claude_code",
            {"session_id": "s1", "repository_url": REPO},
            "session_end",
        )

        self.assertNotIn("observation", built)

    def test_explicit_observation_wins_over_synthesis(self) -> None:
        built = build_generic_hook_payload(
            CONFIG,
            "claude_code",
            {
                "session_id": "s1",
                "repository_url": REPO,
                "prompt": "ignored",
                "observation": {"type": "custom", "body": "explicit"},
            },
            "user_prompt_submit",
        )

        self.assertEqual({"type": "custom", "body": "explicit"}, built["observation"])

    def test_session_start_source_lands_in_body(self) -> None:
        built = build_session_start_hook_payload(
            CONFIG,
            "claude_code",
            {"session_id": "s1", "repository_url": REPO, "source": "startup"},
        )

        self.assertIn("startup", built["observation"]["body"])


class OccurredAtTests(unittest.TestCase):
    def test_occurred_at_is_iso_datetime(self) -> None:
        built = build_generic_hook_payload(
            CONFIG,
            "claude_code",
            {"session_id": "s1", "repository_url": REPO, "prompt": "hi"},
            "user_prompt_submit",
        )

        datetime.fromisoformat(str(built["occurred_at"]))

    def test_occurred_at_does_not_break_idempotency(self) -> None:
        hook_input: dict[str, object] = {
            "session_id": "s1",
            "repository_url": REPO,
            "prompt": "hi",
        }
        first = build_generic_hook_payload(
            CONFIG, "claude_code", hook_input, "user_prompt_submit"
        )
        second = build_generic_hook_payload(
            CONFIG, "claude_code", hook_input, "user_prompt_submit"
        )

        self.assertEqual(first["content_hash"], second["content_hash"])
        self.assertEqual(first["event_id"], second["event_id"])

    def test_explicit_occurred_at_preserved(self) -> None:
        built = build_generic_hook_payload(
            CONFIG,
            "claude_code",
            {
                "session_id": "s1",
                "repository_url": REPO,
                "prompt": "hi",
                "occurred_at": "2026-07-02T10:00:00+00:00",
            },
            "user_prompt_submit",
        )

        self.assertEqual("2026-07-02T10:00:00+00:00", built["occurred_at"])


class PayloadSizeBoundTests(unittest.TestCase):
    def test_oversized_tool_input_replaced_with_preview(self) -> None:
        built = build_generic_hook_payload(
            CONFIG,
            "claude_code",
            {
                "session_id": "s1",
                "repository_url": REPO,
                "tool_name": "Write",
                "tool_input": {"file_path": "/repo/big.py", "content": "y" * 100000},
            },
            "post_tool_use",
        )

        payload = built["payload"]
        self.assertNotIn("tool_input", payload)
        self.assertIn("tool_input_preview", payload)
        self.assertLessEqual(len(json.dumps(payload)), 65536)

    def test_small_tool_input_kept_verbatim(self) -> None:
        built = build_generic_hook_payload(
            CONFIG,
            "claude_code",
            {
                "session_id": "s1",
                "repository_url": REPO,
                "tool_name": "Read",
                "tool_input": {"file_path": "/repo/a.py"},
            },
            "post_tool_use",
        )

        self.assertEqual({"file_path": "/repo/a.py"}, built["payload"]["tool_input"])


if __name__ == "__main__":
    unittest.main()
