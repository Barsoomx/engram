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
