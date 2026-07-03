# MCP Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Engram MCP bridge deliverable, tested end-to-end, and documented: merge it into the `engram-connect` dist, auto-register it via the Claude plugin, fix known contract bugs, and cover it with unit + e2e tests.

**Architecture:** The stdio JSON-RPC server moves from the orphaned `packages/mcp` into `packages/cli/engram_cli` as two flat modules (`mcp_tools.py` handlers on top of existing `http.py`/`config.py`, `mcp_server.py` protocol loop). The CLI grows an `engram mcp install|serve` group; the Claude plugin ships `.mcp.json` + a `hooks/mcp.py` shim so `engram install` delivers MCP with zero extra steps. Both e2e pipelines drive the server over real stdio.

**Tech Stack:** Python 3.12 stdlib only (no new deps), unittest (CI runs `unittest discover` for packages/cli — do NOT write pytest-style tests here), Docker Compose e2e, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-07-03-mcp-delivery-design.md`. One recorded deviation: the spec says "port tests to pytest", but CI runs `python3 -m unittest discover -s packages/cli`, which does not collect pytest-style function tests — package tests stay unittest-style to match the harness and surrounding code.

**User decisions (already made):** User was AFK during design; defaults chosen per spec: approach A (merge into CLI), tool set = existing 5 + `engram_memory_feedback`, curator tools deferred, Codex MCP registration deferred (docs snippet only).

**Branch:** `feat/mcp-delivery` (already created; spec committed as 38b0f154).

**Style notes for all tasks:** `packages/cli` uses double quotes and blank line after `return`/`raise` — match it. No comments/docstrings unless non-obvious. Absolute imports. Do not run `pip install`; everything is stdlib. Run package tests on the host: they need no Docker.

---

### Task 1: MCP tool handlers on the CLI runtime (`mcp_tools.py`)

**Goal:** Implement all six MCP tool handlers as thin wrappers over `engram_cli.http`/`engram_cli.config` with env→file config resolution, repository-url fallback, unique request ids, and correct response fields.

**Files:**
- Create: `packages/cli/engram_cli/mcp_tools.py`
- Create: `packages/cli/engram_cli/mcp_tools_tests.py`

**Acceptance Criteria:**
- [ ] `resolve_runtime` prefers env vars, falls back to `~/.engram/config.json` + `credentials.json` (via `local_paths(config_dir)`), and falls back to git `repository_url` only when `project_id` is absent
- [ ] All six handlers (`search`, `context`, `link`, `observations`, `version`, `feedback`) build payloads matching the CLI's endpoints: `/v1/search/`, `/v1/context/session-start`, `/v1/memories/{id}/links`, `/v1/observations/`, `/v1/memories/{id}/version`, `/v1/memories/{id}/feedback`
- [ ] `update_memory_version` renders `current_version` and `memory_version_id` (not `version`/`reason`)
- [ ] Two consecutive calls to the same write handler produce different `request_id` values (`mcp-<uuid4>`), unless the caller passes `request_id` explicitly
- [ ] Write handlers (`link`, `version`, `feedback`) and `observations` return a "requires a connected project" message in repository-url mode
- [ ] Non-2xx responses render `Engram call failed: HTTP <status> <code>: <detail>` without the API key
- [ ] Tests use a stub transport object (no network), typed fixtures, unittest style

**Verify:** `PYTHONPATH=packages/cli python3 -m unittest engram_cli.mcp_tools_tests -v` → all tests pass

**Steps:**

- [ ] **Step 1: Write failing tests** — `packages/cli/engram_cli/mcp_tools_tests.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=packages/cli python3 -m unittest engram_cli.mcp_tools_tests -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'engram_cli.mcp_tools'`

- [ ] **Step 3: Implement** `packages/cli/engram_cli/mcp_tools.py`:

```python
from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from engram_cli.commands import _git_remote_url
from engram_cli.config import as_string, local_paths, read_json
from engram_cli.http import Transport, get_json, post_json, urllib_transport

ToolFn = Callable[[dict[str, Any]], str]
HandlerFn = Callable[[dict[str, Any], "str | None", Transport], str]

NOT_CONFIGURED_MESSAGE = (
    "Engram MCP bridge is not configured. Run `engram connect` first, or set "
    "ENGRAM_SERVER_URL and ENGRAM_API_KEY."
)
PROJECT_REQUIRED_MESSAGE = (
    "This tool requires a connected project. Run `engram connect --project ...` "
    "or set ENGRAM_PROJECT_ID."
)


@dataclass(frozen=True)
class McpRuntime:
    server_url: str
    api_key: str
    project_id: str
    team_id: str
    repository_url: str
    agent_runtime: str


def resolve_runtime(config_dir: str | None = None) -> McpRuntime | None:
    paths = local_paths(config_dir)
    config = _read_optional_json(paths.config)
    credentials = _read_optional_json(paths.credentials)
    server_url = (
        os.environ.get("ENGRAM_SERVER_URL") or as_string(config.get("server_url"))
    ).rstrip("/")
    api_key = os.environ.get("ENGRAM_API_KEY") or as_string(credentials.get("api_key"))
    project_id = os.environ.get("ENGRAM_PROJECT_ID") or as_string(
        config.get("project_id")
    )
    team_id = os.environ.get("ENGRAM_TEAM_ID") or as_string(config.get("team_id"))
    agent_runtime = os.environ.get("ENGRAM_AGENT_RUNTIME") or "codex"
    repository_url = "" if project_id else _git_remote_url(os.getcwd())
    if not server_url or not api_key:
        return None

    if not project_id and not repository_url:
        return None

    return McpRuntime(
        server_url=server_url,
        api_key=api_key,
        project_id=project_id,
        team_id=team_id,
        repository_url=repository_url,
        agent_runtime=agent_runtime,
    )


def build_tools(
    config_dir: str | None = None, transport: Transport | None = None
) -> dict[str, ToolFn]:
    active = transport or urllib_transport

    def bind(handler: HandlerFn) -> ToolFn:
        def tool(arguments: dict[str, Any]) -> str:
            return handler(arguments, config_dir, active)

        return tool

    return {
        "engram_search": bind(search_memory),
        "engram_context": bind(fetch_context),
        "engram_memory_link": bind(create_memory_link),
        "engram_observations": bind(list_observations),
        "engram_memory_version": bind(update_memory_version),
        "engram_memory_feedback": bind(submit_memory_feedback),
    }


def search_memory(
    arguments: dict[str, Any], config_dir: str | None, transport: Transport
) -> str:
    runtime = resolve_runtime(config_dir)
    if runtime is None:
        return NOT_CONFIGURED_MESSAGE

    payload = _scope_payload(runtime)
    payload.update(
        {
            "query": as_string(arguments.get("query")),
            "file_paths": arguments.get("file_paths") or [],
            "symbols": arguments.get("symbols") or [],
            "limit": arguments.get("limit") or 5,
        },
    )
    status, body = post_json(
        transport=transport,
        server_url=runtime.server_url,
        path="/v1/search/",
        api_key=runtime.api_key,
        payload=payload,
    )
    if status != 200:
        return _error_text(status, body)

    items = body.get("items")
    if not isinstance(items, list) or not items:
        return "No memory matched the search."

    lines: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        lines.append(f"[{item.get('citation')}] {item.get('title')}")
        lines.append(f"  {item.get('body')}")

    return "\n".join(lines)


def fetch_context(
    arguments: dict[str, Any], config_dir: str | None, transport: Transport
) -> str:
    session_id = as_string(arguments.get("session_id"))
    if not session_id:
        return "engram_context requires session_id."

    runtime = resolve_runtime(config_dir)
    if runtime is None:
        return NOT_CONFIGURED_MESSAGE

    payload = _scope_payload(runtime)
    payload.update(
        {
            "agent_runtime": runtime.agent_runtime,
            "session_id": session_id,
            "request_id": _new_request_id(arguments),
            "query": as_string(arguments.get("query")),
            "file_paths": arguments.get("file_paths") or [],
            "symbols": arguments.get("symbols") or [],
            "limit": arguments.get("limit") or 5,
        },
    )
    status, body = post_json(
        transport=transport,
        server_url=runtime.server_url,
        path="/v1/context/session-start",
        api_key=runtime.api_key,
        payload=payload,
    )
    if status != 200:
        return _error_text(status, body)

    rendered = as_string(body.get("rendered_context"))
    if not rendered:
        return "Engram returned no context for this session."

    return rendered


def create_memory_link(
    arguments: dict[str, Any], config_dir: str | None, transport: Transport
) -> str:
    memory_id = as_string(arguments.get("memory_id"))
    link_type = as_string(arguments.get("link_type"))
    target = as_string(arguments.get("target"))
    if not memory_id or not link_type or not target:
        return "engram_memory_link requires memory_id, link_type, and target."

    runtime = resolve_runtime(config_dir)
    if runtime is None:
        return NOT_CONFIGURED_MESSAGE

    if not runtime.project_id:
        return PROJECT_REQUIRED_MESSAGE

    payload = _project_payload(runtime)
    payload.update(
        {
            "link_type": link_type,
            "target": target,
            "label": as_string(arguments.get("label")),
            "request_id": _new_request_id(arguments),
        },
    )
    status, body = post_json(
        transport=transport,
        server_url=runtime.server_url,
        path=f"/v1/memories/{memory_id}/links",
        api_key=runtime.api_key,
        payload=payload,
    )
    if status not in (200, 201):
        return _error_text(status, body)

    return (
        f"link_id={body.get('link_id')} link_type={body.get('link_type')} "
        f"target={body.get('target')} created={body.get('created')}"
    )


def list_observations(
    arguments: dict[str, Any], config_dir: str | None, transport: Transport
) -> str:
    runtime = resolve_runtime(config_dir)
    if runtime is None:
        return NOT_CONFIGURED_MESSAGE

    if not runtime.project_id:
        return PROJECT_REQUIRED_MESSAGE

    params: dict[str, str] = {
        "project_id": runtime.project_id,
        "limit": str(arguments.get("limit") or 10),
    }
    if runtime.team_id:
        params["team_id"] = runtime.team_id
    status, body = get_json(
        transport=transport,
        server_url=runtime.server_url,
        path="/v1/observations/",
        api_key=runtime.api_key,
        params=params,
    )
    if status != 200:
        return _error_text(status, body)

    items = body.get("items")
    if not isinstance(items, list) or not items:
        return "No observations found."

    lines: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        lines.append(f"[{item.get('observation_type')}] {item.get('title')}")
        body_text = as_string(item.get("body"))
        if body_text:
            lines.append(f"  {body_text}")

    return "\n".join(lines)


def update_memory_version(
    arguments: dict[str, Any], config_dir: str | None, transport: Transport
) -> str:
    memory_id = as_string(arguments.get("memory_id"))
    body_text = as_string(arguments.get("body"))
    if not memory_id or not body_text:
        return "engram_memory_version requires memory_id and body."

    runtime = resolve_runtime(config_dir)
    if runtime is None:
        return NOT_CONFIGURED_MESSAGE

    if not runtime.project_id:
        return PROJECT_REQUIRED_MESSAGE

    payload = _project_payload(runtime)
    payload.update(
        {
            "body": body_text,
            "reason": as_string(arguments.get("reason")),
            "request_id": _new_request_id(arguments),
        },
    )
    status, body = post_json(
        transport=transport,
        server_url=runtime.server_url,
        path=f"/v1/memories/{memory_id}/version",
        api_key=runtime.api_key,
        payload=payload,
    )
    if status != 200:
        return _error_text(status, body)

    return (
        f"memory_id={body.get('memory_id') or memory_id} "
        f"current_version={body.get('current_version')} "
        f"memory_version_id={body.get('memory_version_id')}"
    )


def submit_memory_feedback(
    arguments: dict[str, Any], config_dir: str | None, transport: Transport
) -> str:
    memory_id = as_string(arguments.get("memory_id"))
    action = as_string(arguments.get("action"))
    reason = as_string(arguments.get("reason"))
    if not memory_id or action not in ("stale", "refuted") or not reason:
        return (
            "engram_memory_feedback requires memory_id, action "
            "(stale or refuted), and reason."
        )

    runtime = resolve_runtime(config_dir)
    if runtime is None:
        return NOT_CONFIGURED_MESSAGE

    if not runtime.project_id:
        return PROJECT_REQUIRED_MESSAGE

    payload = _project_payload(runtime)
    payload.update(
        {
            "action": action,
            "reason": reason,
            "request_id": _new_request_id(arguments),
        },
    )
    status, body = post_json(
        transport=transport,
        server_url=runtime.server_url,
        path=f"/v1/memories/{memory_id}/feedback",
        api_key=runtime.api_key,
        payload=payload,
    )
    if status != 200:
        return _error_text(status, body)

    return (
        f"memory_id={body.get('memory_id') or memory_id} "
        f"action={body.get('action')} stale={body.get('stale')} "
        f"refuted={body.get('refuted')} "
        f"already_applied={body.get('already_applied')}"
    )


def _read_optional_json(path: Path) -> dict[str, object]:
    try:
        return read_json(path)
    except (OSError, ValueError):
        return {}


def _scope_payload(runtime: McpRuntime) -> dict[str, object]:
    payload: dict[str, object] = {}
    if runtime.project_id:
        payload["project_id"] = runtime.project_id
    elif runtime.repository_url:
        payload["repository_url"] = runtime.repository_url
    if runtime.team_id:
        payload["team_id"] = runtime.team_id

    return payload


def _project_payload(runtime: McpRuntime) -> dict[str, object]:
    payload: dict[str, object] = {"project_id": runtime.project_id}
    if runtime.team_id:
        payload["team_id"] = runtime.team_id

    return payload


def _new_request_id(arguments: dict[str, Any]) -> str:
    provided = as_string(arguments.get("request_id"))

    return provided or f"mcp-{uuid.uuid4()}"


def _error_text(status: int, body: dict[str, object]) -> str:
    code = as_string(body.get("code")) or "error"
    detail = as_string(body.get("detail")) or "request failed"

    return f"Engram call failed: HTTP {status} {code}: {detail}"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=packages/cli python3 -m unittest engram_cli.mcp_tools_tests -v`
Expected: PASS (all tests)

Also run the full package suite to catch regressions:
`PYTHONPATH=packages/cli python3 -m unittest discover -s packages/cli -p '*_tests.py' -v` → PASS

- [ ] **Step 5: Commit**

```bash
git add packages/cli/engram_cli/mcp_tools.py packages/cli/engram_cli/mcp_tools_tests.py
git commit -m 'feat: add mcp tool handlers on cli runtime'
```

```json:metadata
{"files": ["packages/cli/engram_cli/mcp_tools.py", "packages/cli/engram_cli/mcp_tools_tests.py"], "verifyCommand": "PYTHONPATH=packages/cli python3 -m unittest discover -s packages/cli -p '*_tests.py' -v", "acceptanceCriteria": ["six handlers with correct endpoints and payloads", "current_version/memory_version_id rendered", "unique mcp-<uuid4> request ids per call", "env-over-file config resolution with repo-url fallback", "project-required message for writes in repo mode"], "modelTier": "mechanical"}
```

---

### Task 2: MCP protocol server module (`mcp_server.py`)

**Goal:** Move the JSON-RPC stdio server into `engram_cli`, add the `engram_memory_feedback` tool schema, guard tool exceptions so the loop survives, and expose `run_mcp_serve` for the CLI.

**Files:**
- Create: `packages/cli/engram_cli/mcp_server.py` (port of `packages/mcp/engram_mcp/server.py`)
- Create: `packages/cli/engram_cli/mcp_server_tests.py` (port of `packages/mcp/engram_mcp/mcp_contract_tests.py`)

**Acceptance Criteria:**
- [ ] `tools/list` returns six tools; `engram_memory_feedback` schema requires `["memory_id", "action", "reason"]` with `action` enum `["stale", "refuted"]`
- [ ] A tool handler raising an exception produces a JSON-RPC error response (code -32603) with the request id, and the server keeps processing subsequent lines
- [ ] `run_mcp_serve(args, stdin, stdout, transport)` builds tools via `mcp_tools.build_tools(args.config_dir, transport)` and runs the loop, returning 0
- [ ] `SERVER_VERSION` is `"0.2.0"`; existing initialize/tools-list/tools-call/malformed-line behavior preserved (ported tests pass)

**Verify:** `PYTHONPATH=packages/cli python3 -m unittest engram_cli.mcp_server_tests -v` → all tests pass

**Steps:**

- [ ] **Step 1: Port tests and add new failing ones.** Copy `packages/mcp/engram_mcp/mcp_contract_tests.py` to `packages/cli/engram_cli/mcp_server_tests.py`, change the import to `from engram_cli.mcp_server import PROTOCOL_VERSION, handle_request, run_server`, add `fake_feedback` to the fake tool map:

```python
def fake_feedback(arguments: dict) -> str:
    return f"feedback {arguments.get('action')} on {arguments.get('memory_id')}"
```

and extend `build_tools()` with `"engram_memory_feedback": fake_feedback`. Update `test_tools_list_returns_all_tools` to expect six names ending with `engram_memory_feedback`, and `test_run_server_handles_ndjson_round_trip` to expect `6 == len(lines[1]["result"]["tools"])`. Add:

```python
    def test_tools_list_feedback_schema(self) -> None:
        response = handle_request(
            {"jsonrpc": "2.0", "id": 10, "method": "tools/list"}, build_tools()
        )
        feedback = response["result"]["tools"][5]

        self.assertEqual("engram_memory_feedback", feedback["name"])
        self.assertEqual(
            ["memory_id", "action", "reason"], feedback["inputSchema"]["required"]
        )
        self.assertEqual(
            ["stale", "refuted"],
            feedback["inputSchema"]["properties"]["action"]["enum"],
        )

    def test_tool_exception_returns_error_and_loop_survives(self) -> None:
        def broken(arguments: dict) -> str:
            raise RuntimeError("boom")

        tools = build_tools()
        tools["engram_search"] = broken
        stdin = io.StringIO(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "engram_search", "arguments": {"query": "x"}},
                },
            )
            + "\n"
            + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            + "\n",
        )
        stdout = io.StringIO()
        run_server(tools, stdin=stdin, stdout=stdout)
        lines = [json.loads(line) for line in stdout.getvalue().splitlines()]

        self.assertEqual(2, len(lines))
        self.assertEqual(-32603, lines[0]["error"]["code"])
        self.assertEqual(1, lines[0]["id"])
        self.assertIn("tools", lines[1]["result"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=packages/cli python3 -m unittest engram_cli.mcp_server_tests -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'engram_cli.mcp_server'`

- [ ] **Step 3: Implement.** Copy `packages/mcp/engram_mcp/server.py` to `packages/cli/engram_cli/mcp_server.py` with these changes:

1. Header/imports and constants:

```python
from __future__ import annotations

import json
import sys
from argparse import Namespace
from collections.abc import Callable
from typing import Any, TextIO

from engram_cli.http import Transport
from engram_cli.mcp_tools import build_tools

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "engram"
SERVER_VERSION = "0.2.0"

ToolFn = Callable[[dict[str, Any]], str]
ToolMap = dict[str, ToolFn]
```

2. Append the feedback tool to `list_tools()` after `engram_memory_version`:

```python
        {
            "name": "engram_memory_feedback",
            "description": "Mark an injected memory stale or refuted with a reason.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "memory_id": {"type": "string"},
                    "action": {"type": "string", "enum": ["stale", "refuted"]},
                    "reason": {"type": "string"},
                },
                "required": ["memory_id", "action", "reason"],
            },
        },
```

3. Guard the call in `handle_request` (replace the bare `text = tool_fn(arguments)`):

```python
        try:
            text = tool_fn(arguments)
        except Exception as error:  # keep the stdio loop alive on tool bugs
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": f"tool {name} failed: {error}"},
            }
```

4. Append the serve entrypoint at the end of the file:

```python
def run_mcp_serve(
    args: Namespace,
    stdin: TextIO,
    stdout: TextIO,
    transport: Transport | None = None,
) -> int:
    tools = build_tools(getattr(args, "config_dir", None), transport)
    run_server(tools, stdin=stdin, stdout=stdout)

    return 0
```

`run_server` and the rest stay as in the original (`server.py:91-156`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=packages/cli python3 -m unittest engram_cli.mcp_server_tests -v` → PASS
Run: `PYTHONPATH=packages/cli python3 -m unittest discover -s packages/cli -p '*_tests.py' -v` → PASS

- [ ] **Step 5: Commit**

```bash
git add packages/cli/engram_cli/mcp_server.py packages/cli/engram_cli/mcp_server_tests.py
git commit -m 'feat: move mcp protocol server into engram_cli with feedback tool and error guard'
```

```json:metadata
{"files": ["packages/cli/engram_cli/mcp_server.py", "packages/cli/engram_cli/mcp_server_tests.py"], "verifyCommand": "PYTHONPATH=packages/cli python3 -m unittest discover -s packages/cli -p '*_tests.py' -v", "acceptanceCriteria": ["six tools listed with feedback schema", "tool exception -> -32603 error, loop survives", "run_mcp_serve wires build_tools and returns 0", "ported contract tests pass"], "modelTier": "mechanical"}
```

---

### Task 3: CLI `mcp` command group and resolvable registration entry

**Goal:** Add `engram mcp install|serve` (keeping `mcp-install` as a deprecated alias), and rewrite the registration entry so it references a command that exists on the user's machine and contains no API key.

**Files:**
- Modify: `packages/cli/engram_cli/main.py` (imports, dispatch, parser)
- Modify: `packages/cli/engram_cli/commands.py` (`build_engram_mcp_entry`, `run_mcp_install` call site; `run_install` output line)
- Modify: `packages/cli/engram_cli/cli_lifecycle_tests.py` (adapt the 11 `mcp-install` tests at ~:2221-2456, add new ones)

**Acceptance Criteria:**
- [ ] `engram mcp install` and `engram mcp-install` both write `mcpServers.engram`; `engram mcp serve --config-dir X` runs the stdio loop via `run_mcp_serve`
- [ ] The written entry uses `shutil.which("engram")` when available (command = absolute engram path, args `["mcp", "serve"]`), else `sys.executable` with args `["-m", "engram_cli", "mcp", "serve"]`; `--config-dir` is appended to args when passed
- [ ] The written entry contains NO `env` block and no API key anywhere; `run_mcp_install` still fails early with `missing_config`/`missing_credential` when `~/.engram` is not connected
- [ ] `engram install` final output includes a line mentioning that MCP tools are delivered by the Claude plugin
- [ ] All existing CLI tests pass after adaptation

**Verify:** `PYTHONPATH=packages/cli python3 -m unittest discover -s packages/cli -p '*_tests.py' -v` → PASS

**Steps:**

- [ ] **Step 1: Write/adapt failing tests.** In `cli_lifecycle_tests.py`: update the existing `mcp-install` tests so entry assertions become:

```python
        entry = data["mcpServers"]["engram"]
        self.assertNotIn("env", entry)
        self.assertNotIn(api_key, json.dumps(data))
```

Add new tests (place next to the existing mcp-install block; reuse that block's helpers for building a connected config dir):

```python
    def test_mcp_install_entry_uses_engram_binary_when_on_path(self) -> None:
        # arrange connected config dir + tmp target config path as in existing tests
        with mock.patch(
            "engram_cli.commands.shutil.which", return_value="/usr/local/bin/engram"
        ):
            exit_code = main(
                [
                    "mcp",
                    "install",
                    "--agent",
                    "claude_code",
                    "--config-dir",
                    config_dir,
                    "--claude-code-config",
                    str(target),
                ],
                stdout=stdout,
                stderr=stderr,
            )
        data = json.loads(target.read_text(encoding="utf-8"))
        entry = data["mcpServers"]["engram"]

        self.assertEqual(0, exit_code)
        self.assertEqual("/usr/local/bin/engram", entry["command"])
        self.assertEqual(
            ["mcp", "serve", "--config-dir", config_dir], entry["args"]
        )

    def test_mcp_install_entry_falls_back_to_python_module(self) -> None:
        with mock.patch("engram_cli.commands.shutil.which", return_value=None):
            exit_code = main([...same as above...])
        entry = json.loads(target.read_text(encoding="utf-8"))["mcpServers"]["engram"]

        self.assertEqual(0, exit_code)
        self.assertEqual(sys.executable, entry["command"])
        self.assertEqual(
            ["-m", "engram_cli", "mcp", "serve", "--config-dir", config_dir],
            entry["args"],
        )

    def test_mcp_install_hyphen_alias_still_works(self) -> None:
        exit_code = main(["mcp-install", "--agent", "claude_code", ...])

        self.assertEqual(0, exit_code)

    def test_mcp_serve_round_trips_initialize_and_tools_list(self) -> None:
        stdin = io.StringIO(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
            + "\n"
            + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            + "\n",
        )
        stdout = io.StringIO()
        exit_code = main(
            ["mcp", "serve", "--config-dir", config_dir], stdin=stdin, stdout=stdout
        )
        lines = [json.loads(line) for line in stdout.getvalue().splitlines()]

        self.assertEqual(0, exit_code)
        self.assertEqual(6, len(lines[1]["result"]["tools"]))
```

Note: when a `--config-dir` was NOT passed to `mcp install`, args must NOT contain `--config-dir` — cover with an assertion inside one adapted test.

- [ ] **Step 2: Run tests → expected FAIL** (`unrecognized arguments`/entry shape mismatch)

- [ ] **Step 3: Implement.**

`main.py` — add import `from engram_cli.mcp_server import run_mcp_serve`; dispatch after the `mcp-install` branch:

```python
    if args.command == "mcp":
        if args.mcp_command == "install":
            return run_mcp_install(args, output, errors, transport)
        if args.mcp_command == "serve":
            return run_mcp_serve(args, stdin or sys.stdin, output, transport)
```

Parser — after the existing `mcp-install` block:

```python
    mcp = subparsers.add_parser("mcp")
    mcp_subparsers = mcp.add_subparsers(dest="mcp_command")
    mcp_install_group = mcp_subparsers.add_parser("install")
    mcp_install_group.add_argument(
        "--agent",
        choices=("claude_code", "claude_desktop", "both"),
        default="both",
    )
    mcp_install_group.add_argument("--config-dir")
    mcp_install_group.add_argument("--claude-code-config")
    mcp_install_group.add_argument("--claude-desktop-config")
    mcp_serve = mcp_subparsers.add_parser("serve")
    mcp_serve.add_argument("--config-dir")
```

Also handle `args.command == "mcp"` with no subcommand (`mcp_command` is None): fall through to the final `parser.print_help(file=errors); return 1`.

`commands.py` — replace `build_engram_mcp_entry` (currently :1586-1600):

```python
def build_engram_mcp_entry(*, config_dir: str | None = None) -> dict[str, object]:
    engram_bin = shutil.which("engram")
    if engram_bin:
        command = engram_bin
        args_list = ["mcp", "serve"]
    else:
        command = sys.executable
        args_list = ["-m", "engram_cli", "mcp", "serve"]
    if config_dir:
        args_list.extend(["--config-dir", config_dir])

    return {"command": command, "args": args_list}
```

In `run_mcp_install`, replace the call site (`entry = build_engram_mcp_entry(server_url=..., api_key=..., project_id=...)`) with:

```python
        entry = build_engram_mcp_entry(config_dir=args.config_dir)
```

Keep the config/credentials validation above it unchanged (it guarantees `mcp serve` will find a working `~/.engram`). In `run_install`, after the doctor step output, add:

```python
    stdout.write("MCP tools ship with the Claude Code plugin (no extra setup).\n")
```

- [ ] **Step 4: Run full package suite → PASS**

Run: `PYTHONPATH=packages/cli python3 -m unittest discover -s packages/cli -p '*_tests.py' -v`

- [ ] **Step 5: Commit**

```bash
git add packages/cli/engram_cli/main.py packages/cli/engram_cli/commands.py packages/cli/engram_cli/cli_lifecycle_tests.py
git commit -m 'feat: add engram mcp command group with resolvable secret-free registration'
```

```json:metadata
{"files": ["packages/cli/engram_cli/main.py", "packages/cli/engram_cli/commands.py", "packages/cli/engram_cli/cli_lifecycle_tests.py"], "verifyCommand": "PYTHONPATH=packages/cli python3 -m unittest discover -s packages/cli -p '*_tests.py' -v", "acceptanceCriteria": ["mcp install|serve work, mcp-install alias kept", "entry resolvable via which(engram) or sys.executable -m engram_cli", "no env/api key in written entry", "install output mentions plugin MCP delivery"], "modelTier": "standard"}
```

---

### Task 4: Claude plugin ships the MCP server

**Goal:** Bundle the MCP modules into the plugin and register the server via plugin-root `.mcp.json`, so `claude plugin install engram@engram-marketplace` delivers MCP automatically.

**Files:**
- Create: `packages/claude-plugin/.mcp.json`
- Create: `packages/claude-plugin/hooks/mcp.py`
- Modify: `packages/claude-plugin/.claude-plugin/plugin.json` (version 0.1.7 → 0.1.8)
- Modify: `.claude-plugin/marketplace.json` (version 0.1.7 → 0.1.8, description)
- Modify: `packages/claude-plugin/claude_plugin_contract_tests.py`
- Regenerate: `packages/claude-plugin/hooks/engram_cli/` via `python3 scripts/sync_plugin_bundle.py`

**Acceptance Criteria:**
- [ ] `.mcp.json` registers `mcpServers.engram` with command `python3` and args `["${CLAUDE_PLUGIN_ROOT}/hooks/mcp.py"]`, no env block
- [ ] `hooks/mcp.py` mirrors `hooks/hook.py` (sys.path shim) and calls `main(["mcp", "serve"])`
- [ ] Bundle contains `hooks/engram_cli/mcp_server.py` and `hooks/engram_cli/mcp_tools.py`; `python3 scripts/sync_plugin_bundle.py --check` exits 0
- [ ] Contract tests assert `.mcp.json` shape, shim existence/content, and bundled MCP modules
- [ ] plugin.json and marketplace.json versions match (0.1.8)

**Verify:** `python3 scripts/sync_plugin_bundle.py --check && PYTHONPATH=packages/claude-plugin python3 -m unittest discover -s packages/claude-plugin -p '*_tests.py' -v` → exit 0, all pass

**Steps:**

- [ ] **Step 1: Write failing contract tests.** In `claude_plugin_contract_tests.py` add module constants and a test:

```python
MCP_MANIFEST_PATH = PACKAGE_ROOT / ".mcp.json"
MCP_SHIM_PATH = PACKAGE_ROOT / "hooks" / "mcp.py"
BUNDLED_MCP_MODULES = (
    PACKAGE_ROOT / "hooks" / "engram_cli" / "mcp_server.py",
    PACKAGE_ROOT / "hooks" / "engram_cli" / "mcp_tools.py",
)


    def test_claude_plugin_ships_mcp_server(self) -> None:
        self.assertTrue(MCP_MANIFEST_PATH.exists(), MCP_MANIFEST_PATH)
        manifest = json.loads(MCP_MANIFEST_PATH.read_text(encoding="utf-8"))
        entry = manifest["mcpServers"]["engram"]

        self.assertEqual("python3", entry["command"])
        self.assertEqual(["${CLAUDE_PLUGIN_ROOT}/hooks/mcp.py"], entry["args"])
        self.assertNotIn("env", entry)
        self.assertTrue(MCP_SHIM_PATH.exists(), MCP_SHIM_PATH)
        shim_text = MCP_SHIM_PATH.read_text(encoding="utf-8")
        self.assertIn('main(["mcp", "serve"])', shim_text)
        for path in BUNDLED_MCP_MODULES:
            self.assertTrue(path.exists(), path)

    def test_plugin_versions_match(self) -> None:
        plugin_version = json.loads(
            PLUGIN_MANIFEST_PATH.read_text(encoding="utf-8")
        )["version"]
        marketplace = json.loads(
            (PACKAGE_ROOT.parents[1] / ".claude-plugin" / "marketplace.json").read_text(
                encoding="utf-8"
            )
        )

        self.assertEqual(plugin_version, marketplace["plugins"][0]["version"])
```

Run: `PYTHONPATH=packages/claude-plugin python3 -m unittest discover -s packages/claude-plugin -p '*_tests.py' -v` → FAIL (missing files)

- [ ] **Step 2: Create the files.**

`packages/claude-plugin/.mcp.json`:

```json
{
  "mcpServers": {
    "engram": {
      "command": "python3",
      "args": ["${CLAUDE_PLUGIN_ROOT}/hooks/mcp.py"]
    }
  }
}
```

`packages/claude-plugin/hooks/mcp.py`:

```python
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from engram_cli.main import main

raise SystemExit(main(["mcp", "serve"]))
```

Bump `"version": "0.1.8"` in `packages/claude-plugin/.claude-plugin/plugin.json` AND in `.claude-plugin/marketplace.json` `plugins[0].version`. Update both descriptions from "Thin Engram hook adapter for Claude Code." to "Thin Engram hook adapter and MCP bridge for Claude Code."

- [ ] **Step 3: Sync the bundle**

Run: `python3 scripts/sync_plugin_bundle.py` then `python3 scripts/sync_plugin_bundle.py --check`
Expected: `bundle synced` then `bundle is in sync` (exit 0). The flat-module layout means `mcp_server.py`/`mcp_tools.py` are picked up automatically.

- [ ] **Step 4: Run tests → PASS**

`PYTHONPATH=packages/claude-plugin python3 -m unittest discover -s packages/claude-plugin -p '*_tests.py' -v`
Also smoke the shim locally (no config → still must answer initialize):

```bash
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize"}' | python3 packages/claude-plugin/hooks/mcp.py
```
Expected: one JSON line with `"protocolVersion":"2024-11-05"`.

- [ ] **Step 5: Commit**

```bash
git add packages/claude-plugin/.mcp.json packages/claude-plugin/hooks/mcp.py packages/claude-plugin/.claude-plugin/plugin.json .claude-plugin/marketplace.json packages/claude-plugin/claude_plugin_contract_tests.py packages/claude-plugin/hooks/engram_cli/
git commit -m 'feat: ship mcp server with claude plugin via plugin-root mcp manifest'
```

```json:metadata
{"files": ["packages/claude-plugin/.mcp.json", "packages/claude-plugin/hooks/mcp.py", "packages/claude-plugin/.claude-plugin/plugin.json", ".claude-plugin/marketplace.json", "packages/claude-plugin/claude_plugin_contract_tests.py"], "verifyCommand": "python3 scripts/sync_plugin_bundle.py --check && PYTHONPATH=packages/claude-plugin python3 -m unittest discover -s packages/claude-plugin -p '*_tests.py' -v", "acceptanceCriteria": [".mcp.json registers python3 hooks/mcp.py with no env", "shim runs main(['mcp','serve'])", "bundle sync includes mcp modules", "plugin+marketplace versions 0.1.8"], "modelTier": "standard"}
```

---

### Task 5: Compose golden-path e2e drives MCP over stdio

**Goal:** Extend `scripts/e2e_golden_path.py` to launch `engram mcp serve` as a subprocess and exercise all six tools against the live Compose backend with an agent-capability key.

**USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Files:**
- Modify: `scripts/e2e_golden_path.py`

**Context the engineer needs:**
- The script already boots Compose, runs `engram_bootstrap_golden_path --api-key <key> --json`, connects a CLI config dir, submits a hook observation, waits for `worker_memory` (`memory_id`, `memory_version_id`, `retrieval_document_id`), and asserts a context bundle (see `main()` at `scripts/e2e_golden_path.py:40-159`).
- The bootstrap command also accepts `--agent-key <key>` and then creates a second org-wide key with capabilities `memories:read, memories:review, observations:write, observations:read, search:query, projects:agent` (see `apps/backend/engram/core/management/commands/engram_bootstrap_golden_path.py:26-33`, `:292-296`). The default key has only `memories:read, observations:write` — NOT enough for MCP tools, so the MCP steps must use the agent key.
- Helper conventions in the script: `run()`, `run_json()`, `progress()`, `assert_equal()`, `assert_secret_absent()`, `required_string()`, `pythonpath_env()`, constants `ROOT`, `COMPOSE_DIR`, `SERVER_URL`.

**Acceptance Criteria:**
- [ ] Bootstrap is called with an additional `--agent-key` value (`egk_e2e_agent_...` generated like `api_key`), and a second config dir is connected with it
- [ ] The MCP step launches `sys.executable -m engram_cli mcp serve --config-dir <agent config dir>` with stdin/stdout pipes and drives: initialize → notifications/initialized → tools/list → tools/call × 7 (search, context, observations, link, version×2, feedback)
- [ ] Assertions: 6 tools listed; search result text contains the run's memory content marker; context call returns non-error text; link renders `link_id=` and `created=True`; the two version calls render different `current_version` values (proves no silent replay); feedback renders `stale=True`; no response text contains either API key; process exits 0 after stdin closes
- [ ] `python3 scripts/e2e_golden_path.py` passes end-to-end locally (captured output showing `MCP stdio bridge passed`)

**Verify:** `python3 scripts/e2e_golden_path.py` → exit 0, output contains `MCP stdio bridge passed` and `Compose golden path passed`

**Steps:**

- [ ] **Step 1: Add the agent key + second connect.** In `main()`: `agent_key = f'egk_e2e_agent_{secrets.token_urlsafe(32)}'` next to `api_key`; pass `'--agent-key', agent_key` to the bootstrap command; after the existing `connect`, add a second temp config dir (create `mcp_config_dir = os.path.join(config_dir, 'mcp')`) and run the same connect command with the agent key and that dir. Track `agent_key` as a secret in all `run()` calls it touches.

- [ ] **Step 2: Add the MCP driver helper** (module level, single-quote style — this script uses single quotes):

```python
def drive_mcp_stdio(
    *,
    config_dir: str,
    env: dict[str, str],
    memory_id: str,
    run_id: str,
    secrets_list: list[str],
) -> None:
    requests = [
        {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'},
        {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
        _tool_call(3, 'engram_search', {'query': run_id}),
        _tool_call(4, 'engram_context', {'session_id': f'mcp-{run_id}'}),
        _tool_call(5, 'engram_observations', {'limit': 5}),
        _tool_call(
            6,
            'engram_memory_link',
            {'memory_id': memory_id, 'link_type': 'file', 'target': f'e2e/{run_id}.py'},
        ),
        _tool_call(
            7,
            'engram_memory_version',
            {'memory_id': memory_id, 'body': f'mcp e2e first update {run_id}'},
        ),
        _tool_call(
            8,
            'engram_memory_version',
            {'memory_id': memory_id, 'body': f'mcp e2e second update {run_id}'},
        ),
        _tool_call(
            9,
            'engram_memory_feedback',
            {'memory_id': memory_id, 'action': 'stale', 'reason': f'mcp e2e {run_id}'},
        ),
    ]
    process = subprocess.Popen(
        [sys.executable, '-m', 'engram_cli', 'mcp', 'serve', '--config-dir', config_dir],
        cwd=ROOT,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout, stderr = process.communicate(
        '\n'.join(json.dumps(request) for request in requests) + '\n', timeout=180
    )
    if process.returncode != 0:
        raise SystemExit(f'mcp serve exited {process.returncode}: {stderr[-2000:]}')

    responses: dict[int, dict[str, object]] = {}
    for line in stdout.splitlines():
        message = json.loads(line)
        if 'id' in message:
            responses[message['id']] = message
    tool_names = [tool['name'] for tool in responses[2]['result']['tools']]
    assert_equal(len(tool_names), 6, 'mcp tools count')
    texts = {rid: _content_text(responses[rid]) for rid in (3, 4, 5, 6, 7, 8, 9)}
    for rid, text in texts.items():
        for secret in secrets_list:
            assert_secret_absent(f'mcp response {rid}', text, secret)
        if text.startswith('Engram call failed'):
            raise SystemExit(f'mcp tool {rid} failed: {text}')
    if run_id not in texts[3]:
        raise SystemExit(f'mcp search missed run marker: {texts[3][:400]}')
    if 'link_id=' not in texts[6] or 'created=True' not in texts[6]:
        raise SystemExit(f'mcp link failed: {texts[6]}')
    first_version = _extract_field(texts[7], 'current_version')
    second_version = _extract_field(texts[8], 'current_version')
    if not first_version or first_version in ('None', second_version):
        raise SystemExit(
            f'mcp version replayed or empty: {texts[7]} / {texts[8]}'
        )
    if 'stale=True' not in texts[9]:
        raise SystemExit(f'mcp feedback failed: {texts[9]}')


def _tool_call(request_id: int, name: str, arguments: dict[str, object]) -> dict[str, object]:
    return {
        'jsonrpc': '2.0',
        'id': request_id,
        'method': 'tools/call',
        'params': {'name': name, 'arguments': arguments},
    }


def _content_text(response: dict[str, object]) -> str:
    result = response.get('result')
    if not isinstance(result, dict):
        return f"Engram call failed: {json.dumps(response.get('error'))}"

    content = result.get('content')

    return content[0]['text'] if isinstance(content, list) and content else ''


def _extract_field(text: str, field: str) -> str:
    for token in text.split():
        if token.startswith(f'{field}='):
            return token.split('=', 1)[1]

    return ''
```

- [ ] **Step 3: Call it from `main()`** after the context-audit assertions (after line ~157), using the agent config dir:

```python
            progress('Driving MCP stdio bridge')
            drive_mcp_stdio(
                config_dir=mcp_config_dir,
                env=cli_env,
                memory_id=worker_memory['memory_id'],
                run_id=run_id,
                secrets_list=[api_key, agent_key],
            )
            progress('MCP stdio bridge passed')
```

Note: run the MCP step AFTER all existing context/audit assertions — the feedback call marks the golden memory stale, which would change earlier retrieval results.

- [ ] **Step 4: Run the full e2e**

Run: `python3 scripts/e2e_golden_path.py`
Expected: exit 0; output contains `MCP stdio bridge passed` then `Compose golden path passed`. This builds Docker images — allow ~10+ minutes on WSL /mnt/c. If a failure is environmental (Docker not running), record the exact command + first decisive failure and stop rather than faking success.

- [ ] **Step 5: Commit**

```bash
git add scripts/e2e_golden_path.py
git commit -m 'test: drive mcp stdio bridge in compose golden path e2e'
```

```json:metadata
{"files": ["scripts/e2e_golden_path.py"], "verifyCommand": "python3 scripts/e2e_golden_path.py", "acceptanceCriteria": ["bootstrap --agent-key wired and second config dir connected", "all six tools called over real stdio against live backend", "two version calls yield different current_version (no replay)", "no secret in any mcp response", "script exits 0 with MCP stdio bridge passed"], "modelTier": "standard", "userGate": true, "tags": ["user-gate"], "requireEvidenceTokens": [["current_version=2", "first update", "version one"], ["current_version=3", "second update", "version two"]]}
```

---

### Task 6: Claude plugin e2e verifies MCP registration

**Goal:** Extend `scripts/e2e_claude_plugin.py` so the plugin e2e proves the installed plugin ships a working MCP server.

**USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Files:**
- Modify: `scripts/e2e_claude_plugin.py`

**Context the engineer needs:**
- The script installs the plugin via the real `claude` CLI (`claude plugin install`, around `scripts/e2e_claude_plugin.py:192`), runs a real prompt against a mock Anthropic gateway, and asserts hook→backend flows. Read the script first; reuse its existing helpers/env structure.
- The installed plugin lands under the Claude config dir used by the test (the script controls `CLAUDE_CONFIG_DIR` or equivalent isolation — find the actual mechanism in the script and locate the installed plugin root on disk under it, e.g. `<claude-config>/plugins/...`; do NOT hardcode a machine-specific path).
- The e2e backend already has a connected `~/.engram`-style config dir for hooks — reuse it (or its env) for the MCP subprocess.

**Acceptance Criteria:**
- [ ] After plugin install, the test locates the installed plugin root and asserts `.mcp.json` and `hooks/mcp.py` exist inside it
- [ ] The test launches `python3 <installed-plugin-root>/hooks/mcp.py` with `ENGRAM_HOME` pointing at the test's connected config dir, drives initialize → tools/list → `engram_search` over stdio, and asserts: protocolVersion returned, 6 tools listed, search returns a non-error text response from the live test backend
- [ ] `python3 scripts/e2e_claude_plugin.py` passes end-to-end (captured output including the new `plugin MCP bridge passed` progress line)

**Verify:** run the same command the CI workflow `.github/workflows/claude-plugin-e2e.yml` runs for `scripts/e2e_claude_plugin.py` (read the workflow for required env/services) → exit 0, output contains `plugin MCP bridge passed`

**Steps:**

- [ ] **Step 1: Read the script and workflow.** Identify: (a) where the plugin is installed and how to find its on-disk root after install; (b) which config dir/env the hooks use to reach the e2e backend; (c) the script's progress/assert helpers.

- [ ] **Step 2: Add a failing MCP verification step** after the plugin-install assertions, following the script's existing style:

```python
def assert_plugin_mcp_bridge(plugin_root: Path, engram_home: str) -> None:
    mcp_manifest = plugin_root / '.mcp.json'
    mcp_shim = plugin_root / 'hooks' / 'mcp.py'
    if not mcp_manifest.exists() or not mcp_shim.exists():
        raise SystemExit(f'plugin mcp files missing under {plugin_root}')

    manifest = json.loads(mcp_manifest.read_text(encoding='utf-8'))
    entry = manifest['mcpServers']['engram']
    if entry['command'] != 'python3' or 'env' in entry:
        raise SystemExit(f'unexpected mcp entry: {entry}')

    env = dict(os.environ)
    env['ENGRAM_HOME'] = engram_home
    requests = [
        {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
        {
            'jsonrpc': '2.0',
            'id': 3,
            'method': 'tools/call',
            'params': {'name': 'engram_search', 'arguments': {'query': 'e2e'}},
        },
    ]
    process = subprocess.run(
        ['python3', str(mcp_shim)],
        input='\n'.join(json.dumps(request) for request in requests) + '\n',
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    if process.returncode != 0:
        raise SystemExit(f'plugin mcp serve exited {process.returncode}: {process.stderr[-2000:]}')

    lines = [json.loads(line) for line in process.stdout.splitlines()]
    if lines[0]['result']['protocolVersion'] != '2024-11-05':
        raise SystemExit('plugin mcp initialize failed')

    if len(lines[1]['result']['tools']) != 6:
        raise SystemExit('plugin mcp tools/list did not return 6 tools')

    search_text = lines[2]['result']['content'][0]['text']
    if search_text.startswith('Engram call failed'):
        raise SystemExit(f'plugin mcp search failed: {search_text}')
```

Wire it into the main flow with the script's `progress('plugin MCP bridge passed')` convention. Adapt names/params to the script's actual structure discovered in Step 1 — the code above is the contract, not a paste-blind snippet.

- [ ] **Step 3: Run the e2e** with the exact env/services the CI workflow uses (mock Anthropic server, compose backend). Expected: exit 0 with the new progress line. If the real `claude` CLI is unavailable locally, record why with the exact command + error, and rely on the CI run of `claude-plugin-e2e.yml` on the PR — do not claim local success.

- [ ] **Step 4: Commit**

```bash
git add scripts/e2e_claude_plugin.py
git commit -m 'test: verify installed plugin ships working mcp bridge in claude e2e'
```

```json:metadata
{"files": ["scripts/e2e_claude_plugin.py"], "verifyCommand": "python3 scripts/e2e_claude_plugin.py (env per .github/workflows/claude-plugin-e2e.yml)", "acceptanceCriteria": ["installed plugin root contains .mcp.json and hooks/mcp.py", "shim answers initialize/tools-list/search over stdio against live test backend", "e2e exits 0 with plugin MCP bridge passed"], "modelTier": "standard", "userGate": true, "tags": ["user-gate"]}
```

---

### Task 7: Documentation alignment

**Goal:** Make every MCP-related doc match the shipped reality: commands, tool names, delivery, env vars, key prefix, deferred set.

**Files:**
- Rewrite: `docs/guides/mcp.md`
- Modify: `docs/mcp-tools.md`
- Modify: `docs/client-installation.md` (the `mcp install` bullet at :39 and :133)
- Modify: `docs/quickstart.md` (MCP mention at :226)
- Modify: `packages/claude-plugin/README.md` (mention MCP bridge ships with plugin)
- Modify: `README.md` (only if it misstates MCP status — check `grep -n -i mcp README.md`)

**Acceptance Criteria:**
- [ ] `docs/guides/mcp.md` no longer says "not yet implemented"; documents: automatic delivery via Claude plugin (`engram install` → plugin → `.mcp.json`), manual `engram mcp install` for Claude Desktop (`--agent claude_desktop`), `engram mcp serve` for any MCP client, env vars + `~/.engram` fallback precedence, real key prefix `egk_`, and a manual Codex `config.toml` snippet (`[mcp_servers.engram] command = "engram" args = ["mcp", "serve"]`) marked as manual-until-supported
- [ ] `docs/mcp-tools.md` lists the six shipped tools by their real names (`engram_search`, `engram_context`, `engram_memory_link`, `engram_observations`, `engram_memory_version`, `engram_memory_feedback`) mapped to the conceptual catalog, and moves the curator/lead set (`team.digest.*`, `memory.contradictions`, `memory.escalations`, `memory.resolve`, `memory.audit`, `memory.simulate_retrieval`, `hooks.doctor`, `memory.observe`, `memory.propose`, `memory.explain`) into an explicit Deferred section with one-line rationale
- [ ] No doc references `python -m engram_mcp`, `engram mcp-install` (except as deprecated alias note), or `sk-engram_` as the API key prefix (grep-verified)
- [ ] All commands shown in the docs parse against the real CLI (`python3 -m engram_cli mcp --help` shape)

**Verify:** `grep -rn 'engram_mcp\|sk-engram_\|not yet implemented' docs/ README.md packages/claude-plugin/README.md` → no hits related to MCP (the deprecated-alias note may mention `mcp-install` once)

**Steps:**

- [ ] **Step 1:** Read current `docs/guides/mcp.md`, `docs/mcp-tools.md`, and grep all MCP references: `grep -rn -i 'mcp' docs/ README.md packages/claude-plugin/README.md | grep -v superpowers | grep -v parity`
- [ ] **Step 2:** Rewrite/update the files per acceptance criteria. Keep the repo's doc tone (short sections, imperative, no marketing). State the delivery matrix explicitly: Claude Code = automatic via plugin; Claude Desktop = `engram mcp install --agent claude_desktop`; other MCP clients = point them at `engram mcp serve`; Codex = manual snippet.
- [ ] **Step 3:** Run the verify grep; fix leftovers.
- [ ] **Step 4: Commit**

```bash
git add docs/guides/mcp.md docs/mcp-tools.md docs/client-installation.md docs/quickstart.md packages/claude-plugin/README.md README.md
git commit -m 'chore: align mcp docs with shipped bridge and delivery channels'
```

```json:metadata
{"files": ["docs/guides/mcp.md", "docs/mcp-tools.md", "docs/client-installation.md", "docs/quickstart.md", "packages/claude-plugin/README.md"], "verifyCommand": "grep -rn 'engram_mcp\\|sk-engram_\\|not yet implemented' docs/ README.md packages/claude-plugin/README.md", "acceptanceCriteria": ["guides/mcp.md matches shipped commands and delivery", "mcp-tools.md lists real six tools + explicit deferred set", "no stale engram_mcp / sk-engram_ / not-yet-implemented references"], "modelTier": "mechanical"}
```

---

### Task 8: Retire `packages/mcp`, update CI, bump `engram-connect` to 0.2.0

**Goal:** Remove the orphaned package now that the bridge lives in the CLI, and update everything that referenced it.

**Files:**
- Delete: `packages/mcp/` (entire directory)
- Modify: `.github/workflows/backend.yml` (remove the "Run MCP tests" step at :106-107 — the moved tests already run under the "Run CLI tests" step)
- Modify: `packages/cli/pyproject.toml` (version 0.1.4 → 0.2.0; add `"mcp"` to keywords)
- Modify: `docs/release-runbook.md` (the `python -m compileall` line at :82 — drop the `packages/mcp` reference)
- Check/Modify: `docs/verification-matrix.md` (`grep -n -i mcp` — update the MCP test row to the new command)

**Acceptance Criteria:**
- [ ] `packages/mcp` no longer exists; `git grep -l 'engram_mcp'` returns only historical spec/plan/parity docs (no runtime code, no workflows, no runbooks)
- [ ] backend.yml has no step pointing at `packages/mcp`; CLI test step unchanged and green locally
- [ ] `engram-connect` version is 0.2.0
- [ ] Full local test sweep passes: CLI package, plugin package, bundle check

**Verify:** `PYTHONPATH=packages/cli python3 -m unittest discover -s packages/cli -p '*_tests.py' -v && PYTHONPATH=packages/claude-plugin python3 -m unittest discover -s packages/claude-plugin -p '*_tests.py' -v && python3 scripts/sync_plugin_bundle.py --check && git grep -l 'engram_mcp' -- ':!docs/superpowers' ':!docs/parity'` → tests pass, final grep prints nothing

**Steps:**

- [ ] **Step 1:** `git rm -r packages/mcp`
- [ ] **Step 2:** Edit backend.yml: delete the two-line "Run MCP tests" step (lines :105-107). Do not touch other steps.
- [ ] **Step 3:** pyproject version + keywords; release-runbook compileall line; verification-matrix row (replace the packages/mcp unittest command with the packages/cli one if a row exists).
- [ ] **Step 4:** Run the verify command block above; every part must pass.
- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m 'refactor: retire packages/mcp after merging bridge into engram-connect 0.2.0'
```

```json:metadata
{"files": [".github/workflows/backend.yml", "packages/cli/pyproject.toml", "docs/release-runbook.md", "docs/verification-matrix.md"], "verifyCommand": "PYTHONPATH=packages/cli python3 -m unittest discover -s packages/cli -p '*_tests.py' -v && PYTHONPATH=packages/claude-plugin python3 -m unittest discover -s packages/claude-plugin -p '*_tests.py' -v && python3 scripts/sync_plugin_bundle.py --check", "acceptanceCriteria": ["packages/mcp deleted, no runtime references remain", "backend.yml MCP step removed", "engram-connect 0.2.0", "full local sweep green"], "modelTier": "mechanical"}
```

---

## Execution notes

- Order: 1 → 2 → 3 → (4, 5, 7 parallel-safe) → 6 (needs 4) → 8 (needs 5 and 6 green).
- One git owner: the coordinating session commits; workers hand back diffs or commit per the subagent-driven flow with `--no-verify` if the worktree pre-commit hook is broken (known issue).
- After Task 8: push branch, open PR to master, record commands + exit codes + CI results in the PR body, run the security-review checklist from the spec (key-out-of-configs is the main delta), and request review.
- The two e2e tasks are user-gate tasks: capture real output, never close on "should work".
