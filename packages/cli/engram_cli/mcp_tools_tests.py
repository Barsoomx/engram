from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engram_cli import mcp_tools


class StubTransport:
    def __init__(self, status: int = 200, body: dict | None = None) -> None:
        self.status = status
        self.body = body if body is not None else {}
        self.calls: list[tuple[str, str, dict, dict | None, float]] = []

    def __call__(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        payload: dict[str, object] | None,
        timeout: float,
    ) -> tuple[int, dict[str, object]]:
        self.calls.append((method, url, headers, payload, timeout))

        return self.status, self.body


ENV_KEYS = (
    "ENGRAM_SERVER_URL",
    "ENGRAM_API_KEY",
    "ENGRAM_PROJECT_ID",
    "ENGRAM_TEAM_ID",
    "ENGRAM_AGENT_RUNTIME",
    "ENGRAM_HOME",
)


class McpToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env = {key: os.environ.pop(key, None) for key in ENV_KEYS}
        self._tmp = tempfile.TemporaryDirectory(prefix="engram-mcp-tests-")
        self.config_dir = self._tmp.name

    def tearDown(self) -> None:
        self._tmp.cleanup()
        for key, value in self._env.items():
            if value is not None:
                os.environ[key] = value
            else:
                os.environ.pop(key, None)

    def write_local_config(
        self,
        *,
        server_url: str = "http://server.local",
        api_key: str = "egk_file_key",
        project_id: str = "11111111-1111-1111-1111-111111111111",
        team_id: str = "",
    ) -> None:
        root = Path(self.config_dir)
        config: dict[str, object] = {"server_url": server_url, "project_id": project_id}
        if team_id:
            config["team_id"] = team_id
        root.joinpath("config.json").write_text(json.dumps(config), encoding="utf-8")
        root.joinpath("credentials.json").write_text(
            json.dumps({"api_key": api_key}), encoding="utf-8"
        )

    def test_resolve_runtime_returns_none_without_any_config(self) -> None:
        with mock.patch.object(mcp_tools, "_git_remote_url", return_value=""):
            runtime = mcp_tools.resolve_runtime(self.config_dir)

        self.assertIsNone(runtime)

    def test_resolve_runtime_reads_local_files(self) -> None:
        self.write_local_config()
        runtime = mcp_tools.resolve_runtime(self.config_dir)

        self.assertEqual("http://server.local", runtime.server_url)
        self.assertEqual("egk_file_key", runtime.api_key)
        self.assertEqual("11111111-1111-1111-1111-111111111111", runtime.project_id)

    def test_env_overrides_local_files(self) -> None:
        self.write_local_config()
        os.environ["ENGRAM_SERVER_URL"] = "http://env.local/"
        os.environ["ENGRAM_API_KEY"] = "egk_env_key"
        runtime = mcp_tools.resolve_runtime(self.config_dir)

        self.assertEqual("http://env.local", runtime.server_url)
        self.assertEqual("egk_env_key", runtime.api_key)

    def test_repository_url_fallback_without_project_id(self) -> None:
        self.write_local_config(project_id="")
        with mock.patch.object(
            mcp_tools, "_git_remote_url", return_value="https://github.com/a/b"
        ):
            runtime = mcp_tools.resolve_runtime(self.config_dir)

        self.assertEqual("", runtime.project_id)
        self.assertEqual("https://github.com/a/b", runtime.repository_url)

    def test_search_posts_scope_and_renders_items(self) -> None:
        self.write_local_config()
        transport = StubTransport(
            body={"items": [{"citation": "c-1", "title": "T", "body": "B"}]}
        )
        text = mcp_tools.search_memory(
            {"query": "auth"}, self.config_dir, transport
        )

        method, url, headers, payload, _timeout = transport.calls[0]
        self.assertEqual("POST", method)
        self.assertTrue(url.endswith("/v1/search/"))
        self.assertEqual(
            "11111111-1111-1111-1111-111111111111", payload["project_id"]
        )
        self.assertEqual("Bearer egk_file_key", headers["Authorization"])
        self.assertIn("[c-1] T", text)

    def test_search_uses_repository_url_when_no_project(self) -> None:
        self.write_local_config(project_id="")
        transport = StubTransport(body={"items": []})
        with mock.patch.object(
            mcp_tools, "_git_remote_url", return_value="https://github.com/a/b"
        ):
            mcp_tools.search_memory({"query": "x"}, self.config_dir, transport)

        payload = transport.calls[0][3]
        self.assertNotIn("project_id", payload)
        self.assertEqual("https://github.com/a/b", payload["repository_url"])

    def test_search_renders_error_without_secret(self) -> None:
        self.write_local_config()
        transport = StubTransport(
            status=403, body={"code": "forbidden", "detail": "denied"}
        )
        text = mcp_tools.search_memory({"query": "x"}, self.config_dir, transport)

        self.assertEqual("Engram call failed: HTTP 403 forbidden: denied", text)
        self.assertNotIn("egk_file_key", text)

    def test_context_requires_session_id(self) -> None:
        text = mcp_tools.fetch_context({}, self.config_dir, StubTransport())

        self.assertIn("session_id", text)

    def test_context_renders_rendered_context(self) -> None:
        self.write_local_config()
        transport = StubTransport(body={"rendered_context": "bundle text"})
        text = mcp_tools.fetch_context(
            {"session_id": "sess-1"}, self.config_dir, transport
        )

        self.assertEqual("bundle text", text)
        payload = transport.calls[0][3]
        self.assertEqual("sess-1", payload["session_id"])
        self.assertTrue(payload["request_id"].startswith("mcp-"))

    def test_memory_version_renders_current_version_fields(self) -> None:
        self.write_local_config()
        transport = StubTransport(
            body={
                "memory_id": "m-1",
                "current_version": 3,
                "memory_version_id": "mv-9",
            }
        )
        text = mcp_tools.update_memory_version(
            {"memory_id": "m-1", "body": "new"}, self.config_dir, transport
        )

        self.assertIn("current_version=3", text)
        self.assertIn("memory_version_id=mv-9", text)
        self.assertNotIn("version=None", text)

    def test_write_request_ids_are_unique_per_call(self) -> None:
        self.write_local_config()
        transport = StubTransport(body={})
        mcp_tools.update_memory_version(
            {"memory_id": "m-1", "body": "one"}, self.config_dir, transport
        )
        mcp_tools.update_memory_version(
            {"memory_id": "m-1", "body": "two"}, self.config_dir, transport
        )
        first = transport.calls[0][3]["request_id"]
        second = transport.calls[1][3]["request_id"]

        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("mcp-"))

    def test_explicit_request_id_wins(self) -> None:
        self.write_local_config()
        transport = StubTransport(body={})
        mcp_tools.update_memory_version(
            {"memory_id": "m-1", "body": "one", "request_id": "fixed-1"},
            self.config_dir,
            transport,
        )

        self.assertEqual("fixed-1", transport.calls[0][3]["request_id"])

    def test_memory_link_posts_payload_and_renders(self) -> None:
        self.write_local_config()
        transport = StubTransport(
            status=201,
            body={
                "link_id": "l-1",
                "link_type": "file",
                "target": "a.py",
                "created": True,
            },
        )
        text = mcp_tools.create_memory_link(
            {"memory_id": "m-1", "link_type": "file", "target": "a.py"},
            self.config_dir,
            transport,
        )

        self.assertIn("link_id=l-1", text)
        self.assertIn("created=True", text)
        self.assertTrue(transport.calls[0][1].endswith("/v1/memories/m-1/links"))

    def test_observations_lists_items(self) -> None:
        self.write_local_config()
        transport = StubTransport(
            body={"items": [{"observation_type": "note", "title": "T", "body": "B"}]}
        )
        text = mcp_tools.list_observations({"limit": 3}, self.config_dir, transport)

        self.assertIn("[note] T", text)
        method, url, _headers, payload, _timeout = transport.calls[0]
        self.assertEqual("GET", method)
        self.assertIn("/v1/observations/", url)
        self.assertIn("limit=3", url)
        self.assertIsNone(payload)

    def test_feedback_validates_action(self) -> None:
        self.write_local_config()
        text = mcp_tools.submit_memory_feedback(
            {"memory_id": "m-1", "action": "wrong", "reason": "r"},
            self.config_dir,
            StubTransport(),
        )

        self.assertIn("stale or refuted", text)

    def test_feedback_posts_and_renders(self) -> None:
        self.write_local_config()
        transport = StubTransport(
            body={
                "memory_id": "m-1",
                "action": "stale",
                "stale": True,
                "refuted": False,
                "already_applied": False,
            }
        )
        text = mcp_tools.submit_memory_feedback(
            {"memory_id": "m-1", "action": "stale", "reason": "outdated"},
            self.config_dir,
            transport,
        )

        self.assertIn("action=stale", text)
        self.assertIn("stale=True", text)
        self.assertTrue(transport.calls[0][1].endswith("/v1/memories/m-1/feedback"))

    def test_project_required_for_writes_in_repo_mode(self) -> None:
        self.write_local_config(project_id="")
        with mock.patch.object(
            mcp_tools, "_git_remote_url", return_value="https://github.com/a/b"
        ):
            version_text = mcp_tools.update_memory_version(
                {"memory_id": "m-1", "body": "x"}, self.config_dir, StubTransport()
            )
            observations_text = mcp_tools.list_observations(
                {}, self.config_dir, StubTransport()
            )

        self.assertIn("requires a connected project", version_text)
        self.assertIn("requires a connected project", observations_text)

    def test_build_tools_exposes_six_tools(self) -> None:
        tools = mcp_tools.build_tools(self.config_dir, StubTransport())

        self.assertEqual(
            [
                "engram_search",
                "engram_context",
                "engram_memory_link",
                "engram_observations",
                "engram_memory_version",
                "engram_memory_feedback",
            ],
            list(tools.keys()),
        )


if __name__ == "__main__":
    unittest.main()
