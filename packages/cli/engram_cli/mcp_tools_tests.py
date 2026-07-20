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


class RouteStubTransport:
    def __init__(
        self,
        routes: dict[str, tuple[int, dict]],
        default: tuple[int, dict] = (404, {}),
    ) -> None:
        self.routes = routes
        self.default = default
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
        for substring, response in self.routes.items():
            if substring in url:
                return response

        return self.default


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
        with mock.patch.object(
            mcp_tools, "workspace_repository_url", return_value=""
        ):
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
            mcp_tools, "workspace_repository_url", return_value="https://github.com/a/b"
        ):
            runtime = mcp_tools.resolve_runtime(self.config_dir)

        self.assertEqual("", runtime.project_id)
        self.assertEqual("https://github.com/a/b", runtime.repository_url)

    def test_codex_per_call_repository_wins_over_plugin_process_cwd(self) -> None:
        self.write_local_config(project_id="")
        transport = StubTransport(body={"items": []})

        with mock.patch.object(
            mcp_tools,
            "workspace_repository_url",
            return_value="https://github.com/engram/plugin-cache",
        ):
            mcp_tools.search_memory(
                {
                    "query": "auth",
                    "__engram_repository_url": "https://github.com/acme/project",
                },
                self.config_dir,
                transport,
            )

        payload = transport.calls[0][3]
        self.assertEqual("https://github.com/acme/project", payload["repository_url"])

    def test_empty_codex_per_call_repository_does_not_fallback_to_process_cwd(
        self,
    ) -> None:
        self.write_local_config(project_id="")
        transport = StubTransport(body={"items": []})

        with mock.patch.object(
            mcp_tools,
            "workspace_repository_url",
            return_value="https://github.com/engram/plugin-cache",
        ):
            result = mcp_tools.search_memory(
                {
                    "query": "auth",
                    "__engram_repository_url": "",
                },
                self.config_dir,
                transport,
            )

        self.assertEqual(mcp_tools.NOT_CONFIGURED_MESSAGE, result)
        self.assertEqual([], transport.calls)

    def test_configured_project_wins_over_codex_per_call_repository(self) -> None:
        self.write_local_config(project_id="configured-project")
        transport = StubTransport(body={"items": []})

        mcp_tools.search_memory(
            {
                "query": "auth",
                "__engram_repository_url": "https://github.com/acme/project",
            },
            self.config_dir,
            transport,
        )

        payload = transport.calls[0][3]
        self.assertEqual("configured-project", payload["project_id"])
        self.assertNotIn("repository_url", payload)

    def test_project_id_argument_wins_over_env_config_and_repo(self) -> None:
        self.write_local_config(project_id="")
        os.environ["ENGRAM_PROJECT_ID"] = "env-project"
        with mock.patch.object(
            mcp_tools, "workspace_repository_url", return_value="https://github.com/a/b"
        ):
            runtime = mcp_tools.resolve_runtime(
                self.config_dir, project_override="arg-project"
            )

        self.assertEqual("arg-project", runtime.project_id)
        self.assertEqual("", runtime.repository_url)

    def test_env_project_id_wins_over_config_and_repo(self) -> None:
        self.write_local_config(project_id="")
        os.environ["ENGRAM_PROJECT_ID"] = "env-project"
        with mock.patch.object(
            mcp_tools, "workspace_repository_url", return_value="https://github.com/a/b"
        ):
            runtime = mcp_tools.resolve_runtime(self.config_dir)

        self.assertEqual("env-project", runtime.project_id)
        self.assertEqual("", runtime.repository_url)

    def test_search_posts_scope_and_renders_items(self) -> None:
        self.write_local_config()
        transport = StubTransport(
            body={
                "items": [
                    {"citation": "c-1", "title": "T", "body": "B", "memory_id": "m-1"}
                ]
            }
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
        self.assertIn("[c-1] T (memory_id=m-1)", text)

    def test_search_renders_kind_and_confidence_suffix(self) -> None:
        self.write_local_config()
        transport = StubTransport(
            body={
                "items": [
                    {
                        "citation": "c-1",
                        "title": "T",
                        "body": "B",
                        "memory_id": "m-1",
                        "kind": "gotcha",
                        "confidence": "0.950",
                    }
                ]
            }
        )
        text = mcp_tools.search_memory(
            {"query": "auth"}, self.config_dir, transport
        )

        self.assertIn(
            "[c-1] T (memory_id=m-1) [gotcha, conf 0.950]", text
        )

    def test_search_renders_kind_only_suffix(self) -> None:
        self.write_local_config()
        transport = StubTransport(
            body={
                "items": [
                    {
                        "citation": "c-1",
                        "title": "T",
                        "body": "B",
                        "memory_id": "m-1",
                        "kind": "gotcha",
                    }
                ]
            }
        )
        text = mcp_tools.search_memory(
            {"query": "auth"}, self.config_dir, transport
        )

        self.assertIn("[c-1] T (memory_id=m-1) [gotcha]", text)

    def test_search_renders_confidence_only_suffix(self) -> None:
        self.write_local_config()
        transport = StubTransport(
            body={
                "items": [
                    {
                        "citation": "c-1",
                        "title": "T",
                        "body": "B",
                        "memory_id": "m-1",
                        "confidence": "0.950",
                    }
                ]
            }
        )
        text = mcp_tools.search_memory(
            {"query": "auth"}, self.config_dir, transport
        )

        self.assertIn("[c-1] T (memory_id=m-1) [conf 0.950]", text)

    def test_search_renders_valid_entries_and_skips_garbage(self) -> None:
        self.write_local_config()
        transport = StubTransport(
            body={
                "items": [
                    {"citation": "c-1", "title": "T", "body": "B", "memory_id": "m-1"},
                    "garbage",
                    None,
                ]
            }
        )
        text = mcp_tools.search_memory(
            {"query": "auth"}, self.config_dir, transport
        )

        self.assertIn("[c-1] T (memory_id=m-1)", text)
        self.assertNotIn("garbage", text)

    def test_search_uses_repository_url_when_no_project(self) -> None:
        self.write_local_config(project_id="")
        transport = StubTransport(body={"items": []})
        with mock.patch.object(
            mcp_tools, "workspace_repository_url", return_value="https://github.com/a/b"
        ):
            mcp_tools.search_memory({"query": "x"}, self.config_dir, transport)

        payload = transport.calls[0][3]
        self.assertNotIn("project_id", payload)
        self.assertEqual("https://github.com/a/b", payload["repository_url"])

    def test_search_tool_argument_project_id_overrides_config(self) -> None:
        self.write_local_config()
        transport = StubTransport(body={"items": []})
        mcp_tools.search_memory(
            {"query": "x", "project_id": "arg-project"}, self.config_dir, transport
        )

        payload = transport.calls[0][3]
        self.assertEqual("arg-project", payload["project_id"])

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

    def test_four_handlers_send_repository_url_without_project_gate(self) -> None:
        self.write_local_config(project_id="")
        with mock.patch.object(
            mcp_tools, "workspace_repository_url", return_value="https://github.com/a/b"
        ):
            version_transport = StubTransport(
                body={"memory_id": "m-1", "current_version": 2}
            )
            version_text = mcp_tools.update_memory_version(
                {"memory_id": "m-1", "body": "x"}, self.config_dir, version_transport
            )
            observations_transport = StubTransport(body={"items": []})
            observations_text = mcp_tools.list_observations(
                {}, self.config_dir, observations_transport
            )
            link_transport = StubTransport(
                status=201, body={"link_id": "l-1", "created": True}
            )
            link_text = mcp_tools.create_memory_link(
                {"memory_id": "m-1", "link_type": "file", "target": "a.py"},
                self.config_dir,
                link_transport,
            )
            feedback_transport = StubTransport(
                body={"memory_id": "m-1", "action": "stale", "stale": True}
            )
            feedback_text = mcp_tools.submit_memory_feedback(
                {"memory_id": "m-1", "action": "stale", "reason": "outdated"},
                self.config_dir,
                feedback_transport,
            )

        self.assertNotIn("requires a connected project", version_text)
        self.assertNotIn("requires a connected project", observations_text)
        self.assertNotIn("requires a connected project", link_text)
        self.assertNotIn("requires a connected project", feedback_text)
        self.assertEqual(
            "https://github.com/a/b", version_transport.calls[0][3]["repository_url"]
        )
        self.assertIn(
            "repository_url=", observations_transport.calls[0][1]
        )
        self.assertEqual(
            "https://github.com/a/b", link_transport.calls[0][3]["repository_url"]
        )
        self.assertEqual(
            "https://github.com/a/b", feedback_transport.calls[0][3]["repository_url"]
        )

    def test_project_not_found_renders_guidance_text(self) -> None:
        self.write_local_config(project_id="")
        with mock.patch.object(
            mcp_tools, "workspace_repository_url", return_value="https://github.com/a/b"
        ):
            transport = StubTransport(
                status=404, body={"code": "project_not_found", "detail": "no project"}
            )
            text = mcp_tools.list_observations({}, self.config_dir, transport)

        self.assertEqual(mcp_tools.PROJECT_NOT_FOUND_MESSAGE, text)

    def test_observations_sends_project_id_argument_and_query_param(self) -> None:
        self.write_local_config(project_id="")
        with mock.patch.object(
            mcp_tools, "workspace_repository_url", return_value="https://github.com/a/b"
        ):
            transport = StubTransport(body={"items": []})
            mcp_tools.list_observations(
                {"project_id": "arg-project"}, self.config_dir, transport
            )

        self.assertIn("project_id=arg-project", transport.calls[0][1])
        self.assertNotIn("repository_url=", transport.calls[0][1])

    def test_build_tools_exposes_eight_tools(self) -> None:
        tools = mcp_tools.build_tools(self.config_dir, StubTransport())

        self.assertEqual(8, len(tools))
        self.assertIn("engram_memory_get", tools)
        self.assertIn("engram_audit", tools)

    def test_build_tools_forwards_config_dir_and_transport(self) -> None:
        self.write_local_config()
        transport = StubTransport(body={"items": []})
        tools = mcp_tools.build_tools(self.config_dir, transport)
        tools["engram_search"]({"query": "x"})

        self.assertEqual(1, len(transport.calls))
        self.assertTrue(transport.calls[0][1].endswith("/v1/search/"))


class MemoryGetToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env = {key: os.environ.pop(key, None) for key in ENV_KEYS}
        self._tmp = tempfile.TemporaryDirectory(prefix="engram-mcp-get-tests-")
        self.config_dir = self._tmp.name

    def tearDown(self) -> None:
        self._tmp.cleanup()
        for key, value in self._env.items():
            if value is not None:
                os.environ[key] = value
            else:
                os.environ.pop(key, None)

    def write_local_config(self, *, project_id: str = "11111111-1111-1111-1111-111111111111") -> None:
        root = Path(self.config_dir)
        config: dict[str, object] = {"server_url": "http://server.local"}
        if project_id:
            config["project_id"] = project_id
        root.joinpath("config.json").write_text(json.dumps(config), encoding="utf-8")
        root.joinpath("credentials.json").write_text(
            json.dumps({"api_key": "egk_file_key"}), encoding="utf-8"
        )

    def _version_body(self) -> dict:
        return {
            "count": 3,
            "items": [
                {"version": 3, "body": "C" * 500, "created_at": "2026-07-03T00:00:00Z"},
                {"version": 2, "body": "older v2", "created_at": "2026-07-02T00:00:00Z"},
                {"version": 1, "body": "older v1", "created_at": "2026-07-01T00:00:00Z"},
            ],
        }

    def _links_body(self) -> dict:
        return {
            "count": 2,
            "items": [
                {
                    "link_id": "l-1",
                    "link_type": "narrowed_by",
                    "target": "memory-target-1",
                    "label": "narrowing label",
                    "created_at": "2026-07-03T00:00:00Z",
                },
                {
                    "link_id": "l-2",
                    "link_type": "file",
                    "target": "apps/backend/x.py",
                    "label": "",
                    "created_at": "2026-07-03T00:00:00Z",
                },
            ],
        }

    def test_memory_get_renders_body_versions_and_links(self) -> None:
        self.write_local_config()
        transport = RouteStubTransport(
            {
                "/version": (200, self._version_body()),
                "/links": (200, self._links_body()),
            }
        )

        text = mcp_tools.memory_get(
            {"memory_id": "m-1"}, self.config_dir, transport
        )

        self.assertIn("C" * 500, text)
        self.assertIn("versions: v3 (2026-07-03T00:00:00Z), v2 (2026-07-02T00:00:00Z), v1 (2026-07-01T00:00:00Z)", text)
        self.assertIn("narrowed_by: memory-target-1 (narrowing label)", text)
        self.assertIn("file: apps/backend/x.py", text)
        self.assertNotIn("file: apps/backend/x.py ()", text)
        self.assertIn("engram_search", text)
        self.assertFalse(any("/v1/inspection/memories/" in call[1] for call in transport.calls))

    def test_memory_get_repo_only_routing(self) -> None:
        self.write_local_config(project_id="")
        transport = RouteStubTransport(
            {
                "/version": (200, self._version_body()),
                "/links": (200, self._links_body()),
            }
        )
        with mock.patch.object(
            mcp_tools, "workspace_repository_url", return_value="https://github.com/a/b"
        ):
            text = mcp_tools.memory_get({"memory_id": "m-1"}, self.config_dir, transport)

        self.assertIn("C" * 500, text)
        self.assertIn("versions: v3", text)
        self.assertIn("links:", text)
        version_calls = [c for c in transport.calls if "/version" in c[1]]
        links_calls = [c for c in transport.calls if "/links" in c[1]]
        self.assertTrue(version_calls and "repository_url=https" in version_calls[0][1])
        self.assertTrue(links_calls and "repository_url=https" in links_calls[0][1])
        self.assertFalse(any("/v1/inspection/memories/" in call[1] for call in transport.calls))

    def test_memory_get_diff_addendum(self) -> None:
        self.write_local_config()
        transport = RouteStubTransport(
            {
                "/version": (200, self._version_body()),
                "/links": (200, self._links_body()),
                "/diff": (
                    200,
                    {
                        "from": {"version": 1, "body": "from body", "created_at": "2026-07-01T00:00:00Z"},
                        "to": {"version": 2, "body": "to body", "created_at": "2026-07-02T00:00:00Z"},
                    },
                ),
            }
        )

        text = mcp_tools.memory_get(
            {"memory_id": "m-1", "from_version": 1, "to_version": 2}, self.config_dir, transport
        )

        self.assertIn("from body", text)
        self.assertIn("to body", text)
        diff_calls = [c for c in transport.calls if "/diff" in c[1]]
        self.assertEqual(1, len(diff_calls))
        self.assertIn("from_version=1", diff_calls[0][1])
        self.assertIn("to_version=2", diff_calls[0][1])

    def test_memory_get_no_diff_for_one_sided_or_nonpositive(self) -> None:
        for from_version, to_version in ((3, 0), (0, 3), (0, 0), (-1, 2)):
            self.write_local_config()
            transport = RouteStubTransport(
                {
                    "/version": (200, self._version_body()),
                    "/links": (200, self._links_body()),
                }
            )

            text = mcp_tools.memory_get(
                {"memory_id": "m-1", "from_version": from_version, "to_version": to_version},
                self.config_dir,
                transport,
            )

            self.assertFalse(any("/diff" in c[1] for c in transport.calls), (from_version, to_version))
            self.assertIn("C" * 500, text)

    def test_memory_get_diff_404_tolerated(self) -> None:
        self.write_local_config()
        transport = RouteStubTransport(
            {
                "/version": (200, self._version_body()),
                "/links": (200, self._links_body()),
                "/diff": (404, {"code": "version_not_found"}),
            }
        )

        text = mcp_tools.memory_get(
            {"memory_id": "m-1", "from_version": 1, "to_version": 2}, self.config_dir, transport
        )

        self.assertIn("C" * 500, text)
        self.assertIn("diff unavailable", text)
        self.assertNotIn("Engram call failed", text)

    def test_memory_get_diff_non_404_surfaces_error_not_success(self) -> None:
        for diff_status in (403, 500, 503):
            self.write_local_config()
            transport = RouteStubTransport(
                {
                    "/version": (200, self._version_body()),
                    "/links": (200, self._links_body()),
                    "/diff": (diff_status, {"code": "boom", "detail": "kaput"}),
                }
            )

            text = mcp_tools.memory_get(
                {"memory_id": "m-1", "from_version": 1, "to_version": 2}, self.config_dir, transport
            )

            self.assertIn(f"HTTP {diff_status}", text, diff_status)
            self.assertNotIn("diff unavailable", text)
            self.assertNotIn("C" * 500, text)

    def test_memory_get_empty_version_returns_not_found_and_skips_links(self) -> None:
        self.write_local_config()
        transport = RouteStubTransport({"/version": (200, {"count": 0, "items": []})})

        text = mcp_tools.memory_get({"memory_id": "m-1"}, self.config_dir, transport)

        self.assertIn("not found", text.lower())
        self.assertFalse(any("/links" in c[1] for c in transport.calls))

    def test_memory_get_links_failure_surfaced(self) -> None:
        self.write_local_config()
        transport = RouteStubTransport(
            {
                "/version": (200, self._version_body()),
                "/links": (503, {}),
            }
        )

        text = mcp_tools.memory_get({"memory_id": "m-1"}, self.config_dir, transport)

        self.assertIn("C" * 500, text)
        self.assertIn("links: unavailable (HTTP 503", text)

    def test_memory_get_links_empty_omits_line(self) -> None:
        self.write_local_config()
        transport = RouteStubTransport(
            {
                "/version": (200, self._version_body()),
                "/links": (200, {"count": 0, "items": []}),
            }
        )

        text = mcp_tools.memory_get({"memory_id": "m-1"}, self.config_dir, transport)

        self.assertNotIn("links:", text)
        self.assertNotIn("unavailable", text)


class AuditToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env = {key: os.environ.pop(key, None) for key in ENV_KEYS}
        self._tmp = tempfile.TemporaryDirectory(prefix="engram-mcp-audit-tests-")
        self.config_dir = self._tmp.name

    def tearDown(self) -> None:
        self._tmp.cleanup()
        for key, value in self._env.items():
            if value is not None:
                os.environ[key] = value
            else:
                os.environ.pop(key, None)

    def write_local_config(
        self, *, project_id: str = "11111111-1111-1111-1111-111111111111", team_id: str = ""
    ) -> None:
        root = Path(self.config_dir)
        config: dict[str, object] = {"server_url": "http://server.local"}
        if project_id:
            config["project_id"] = project_id
        if team_id:
            config["team_id"] = team_id
        root.joinpath("config.json").write_text(json.dumps(config), encoding="utf-8")
        root.joinpath("credentials.json").write_text(
            json.dumps({"api_key": "egk_file_key"}), encoding="utf-8"
        )

    def _event(self, **overrides: object) -> dict:
        event = {
            "event_type": "MemoryTransitionCommitted",
            "metadata": {"transition_type": "refute", "reason": "contradicted"},
            "actor_id": "actor-uuid",
            "actor_display": "Alice",
            "result": "recorded",
            "target_id": "m-1",
            "target_display": "Some Title",
            "target_type": "memory",
            "capability": "memories:write",
            "created_at": "2026-07-03T00:00:00Z",
        }
        event.update(overrides)

        return event

    def test_audit_happy_path_with_metadata(self) -> None:
        self.write_local_config()
        transport = StubTransport(body={"count": 1, "items": [self._event()]})

        text = mcp_tools.audit({"memory_id": "m-1"}, self.config_dir, transport)

        self.assertIn("audit trace for memory m-1", text)
        self.assertIn("MemoryTransitionCommitted", text)
        self.assertIn("(refute)", text)
        self.assertIn("actor=actor-uuid (Alice)", text)
        self.assertIn("result=recorded", text)
        self.assertIn("target=m-1 (Some Title)", text)
        self.assertIn("target_type=memory", text)
        self.assertIn("capability=memories:write", text)
        self.assertIn("reason=contradicted", text)
        self.assertNotIn("showing most recent", text)
        url = transport.calls[0][1]
        self.assertIn("target_id=m-1", url)
        self.assertIn("target_type=memory", url)
        self.assertIn("ordering=-created_at", url)

    def test_audit_null_displays_have_no_empty_parens(self) -> None:
        self.write_local_config()
        event = self._event(actor_display=None, target_display=None)
        transport = StubTransport(body={"count": 1, "items": [event]})

        text = mcp_tools.audit({"memory_id": "m-1"}, self.config_dir, transport)

        self.assertIn("actor=actor-uuid result=", text)
        self.assertIn("target=m-1 target_type=memory", text)

    def test_audit_conditional_headers(self) -> None:
        self.write_local_config()
        project_wide = StubTransport(body={"count": 0, "items": []})
        text = mcp_tools.audit({}, self.config_dir, project_wide)
        self.assertIn("project-wide audit events", text)
        self.assertNotIn("audit trace for memory", text)

        link_trace = StubTransport(body={"count": 0, "items": []})
        text = mcp_tools.audit(
            {"target_id": "X", "target_type": "memory_link"}, self.config_dir, link_trace
        )
        self.assertIn("audit trace for memory_link X", text)

    def test_audit_event_without_metadata_is_clean(self) -> None:
        self.write_local_config()
        event = self._event(metadata={})
        transport = StubTransport(body={"count": 1, "items": [event]})

        text = mcp_tools.audit({"memory_id": "m-1"}, self.config_dir, transport)

        self.assertNotIn("(refute)", text)
        self.assertNotIn("reason=", text)
        self.assertNotIn("None", text)

    def test_audit_multiline_injection_guard(self) -> None:
        self.write_local_config()
        injected = "\n2099-01-01 EvilEvent fake record"
        event = self._event(
            metadata={"transition_type": "refute", "reason": "line1" + injected},
            actor_id="actor" + injected,
            target_id="m-1" + injected,
            target_type="memory" + injected,
            actor_display="Alice" + injected,
            target_display="Title" + injected,
        )
        transport = StubTransport(body={"count": 1, "items": [event]})

        text = mcp_tools.audit({"memory_id": "m-1"}, self.config_dir, transport)

        event_lines = [line for line in text.splitlines() if line.startswith("2026-07-03")]
        self.assertEqual(1, len(event_lines))
        self.assertNotIn("EvilEvent", "".join(l for l in text.splitlines() if "2099" in l and l != event_lines[0]) or "")

        header_stub = StubTransport(body={"count": 0, "items": []})
        header_text = mcp_tools.audit(
            {"target_id": "X" + injected, "target_type": "memory_link" + injected},
            self.config_dir,
            header_stub,
        )
        header_lines = header_text.splitlines()
        self.assertEqual(2, len(header_lines))
        self.assertTrue(header_lines[0].startswith("audit trace"))
        self.assertEqual(1, len([line for line in header_lines if "EvilEvent" in line]))

    def test_audit_memory_get_links_injection_guard(self) -> None:
        self.write_local_config()
        injected = "\nfake links line"
        transport = RouteStubTransport(
            {
                "/version": (
                    200,
                    {"count": 1, "items": [{"version": 1, "body": "body", "created_at": "2026-07-01T00:00:00Z"}]},
                ),
                "/links": (
                    200,
                    {
                        "count": 1,
                        "items": [
                            {
                                "link_id": "l-1",
                                "link_type": "file",
                                "target": "path" + injected,
                                "label": "label" + injected,
                                "created_at": "2026-07-01T00:00:00Z",
                            }
                        ],
                    },
                ),
            }
        )

        text = mcp_tools.memory_get({"memory_id": "m-1"}, self.config_dir, transport)

        lines = text.splitlines()
        links_lines = [line for line in lines if line.startswith("links:")]
        self.assertEqual(1, len(links_lines))
        self.assertEqual(1, len([line for line in lines if "fake links line" in line]))

    def test_audit_single_request_and_truncation_note(self) -> None:
        self.write_local_config()
        items = [self._event(created_at=f"2026-07-{index:02d}T00:00:00Z", actor_id=f"a-{index}") for index in range(20)]
        transport = StubTransport(body={"count": 25, "items": items})

        text = mcp_tools.audit({"memory_id": "m-1", "limit": 20}, self.config_dir, transport)

        self.assertEqual(1, len(transport.calls))
        url = transport.calls[0][1]
        self.assertNotIn("offset", url)
        self.assertIn("ordering=-created_at", url)
        self.assertIn("limit=20", url)
        self.assertIn(
            "(showing most recent 20 of 25 events; 5 older omitted — narrow with since/until/event_type)",
            text,
        )
        event_lines = [line for line in text.splitlines() if line.startswith("2026-07-")]
        self.assertEqual([f"a-{index}" for index in range(20)], [line.split("actor=")[1].split(" ")[0] for line in event_lines])

    def test_audit_no_note_when_count_le_limit(self) -> None:
        self.write_local_config()
        for count in (12, 20):
            items = [self._event(actor_id=f"a-{index}") for index in range(count)]
            transport = StubTransport(body={"count": count, "items": items})

            text = mcp_tools.audit({"memory_id": "m-1", "limit": 20}, self.config_dir, transport)

            self.assertEqual(1, len(transport.calls))
            self.assertNotIn("showing most recent", text)

    def test_audit_missing_capability(self) -> None:
        self.write_local_config()
        transport = StubTransport(
            status=403,
            body={"code": "missing_capability", "error_code": "missing_capability", "detail": "no"},
        )

        text = mcp_tools.audit({"memory_id": "m-1"}, self.config_dir, transport)

        self.assertIn("audit:read", text)

    def test_audit_and_memory_get_project_scope_denied(self) -> None:
        self.write_local_config()
        body = {"code": "project_scope_denied", "error_code": "project_scope_denied", "detail": "no"}

        audit_text = mcp_tools.audit(
            {"memory_id": "m-1"}, self.config_dir, StubTransport(status=403, body=body)
        )
        get_text = mcp_tools.memory_get(
            {"memory_id": "m-1"}, self.config_dir, StubTransport(status=403, body=body)
        )

        for text in (audit_text, get_text):
            self.assertIn("cannot resolve project", text)
            self.assertNotIn("audit:read", text)
            self.assertNotIn("memories:read", text)

    def test_team_scope_denied_names_forwarded_team(self) -> None:
        self.write_local_config(team_id="team-42")
        body = {"code": "team_scope_denied", "error_code": "team_scope_denied", "detail": "no"}

        audit_text = mcp_tools.audit(
            {"memory_id": "m-1"}, self.config_dir, StubTransport(status=403, body=body)
        )
        get_text = mcp_tools.memory_get(
            {"memory_id": "m-1"}, self.config_dir, StubTransport(status=403, body=body)
        )

        for text in (audit_text, get_text):
            self.assertIn("cannot access team team-42 for memory m-1", text)
            self.assertNotIn("audit:read", text)
            self.assertNotIn("cannot resolve project", text)

    def test_team_scope_denied_without_forwarded_team_is_truthful(self) -> None:
        self.write_local_config(team_id="")
        body = {"code": "team_scope_denied", "error_code": "team_scope_denied", "detail": "no"}

        get_text = mcp_tools.memory_get(
            {"memory_id": "m-1"}, self.config_dir, StubTransport(status=403, body=body)
        )

        self.assertIn("team scope of memory m-1", get_text)
        self.assertNotIn("access team  ", get_text)
        self.assertNotIn("team  for memory", get_text)
        self.assertNotIn("cannot resolve project", get_text)

    def test_memory_get_project_scope_denied_repo_routed_names_repository(self) -> None:
        self.write_local_config(project_id="")
        body = {"code": "project_scope_denied", "error_code": "project_scope_denied", "detail": "no"}
        with mock.patch.object(
            mcp_tools, "workspace_repository_url", return_value="https://github.com/a/b"
        ):
            text = mcp_tools.memory_get(
                {"memory_id": "m-1"}, self.config_dir, StubTransport(status=403, body=body)
            )

        self.assertIn("repository https://github.com/a/b", text)
        self.assertNotIn("resolve project .", text)
        self.assertNotIn("resolve project.", text)

    def test_audit_missing_project_id_makes_no_call(self) -> None:
        self.write_local_config(project_id="")
        transport = StubTransport(body={"count": 0, "items": []})
        with mock.patch.object(
            mcp_tools, "workspace_repository_url", return_value="https://github.com/a/b"
        ):
            text = mcp_tools.audit({"memory_id": "m-1"}, self.config_dir, transport)

        self.assertIn("needs a project_id", text)
        self.assertEqual([], transport.calls)

    def test_audit_empty_result(self) -> None:
        self.write_local_config()
        transport = StubTransport(body={"count": 0, "items": []})

        text = mcp_tools.audit({"memory_id": "m-1"}, self.config_dir, transport)

        self.assertIn("audit trace for memory m-1", text)
        self.assertIn("No audit events found.", text)


if __name__ == "__main__":
    unittest.main()
