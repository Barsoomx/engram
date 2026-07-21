from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from engram_cli import read_tools
from engram_cli.commands import (
    KINDS_ERROR_MESSAGE,
    is_kinds_error_body,
    observation_meta_line,
    render_citations,
    render_warnings,
    search_item_suffix,
    search_match_line,
    workspace_repository_url,
)
from engram_cli.config import as_string, local_paths, read_json
from engram_cli.http import Transport, get_json, post_json, urllib_transport

ToolFn = Callable[[dict[str, Any]], str]
HandlerFn = Callable[[dict[str, Any], str | None, Transport], str]
INTERNAL_REPOSITORY_URL_ARGUMENT = "__engram_repository_url"

NOT_CONFIGURED_MESSAGE = (
    "Engram MCP bridge is not configured. Run `engram connect` first, or set "
    "ENGRAM_SERVER_URL and ENGRAM_API_KEY."
)
PROJECT_NOT_FOUND_MESSAGE = (
    "No Engram project exists for this repository yet — it is created on the "
    "first hook ingest."
)
MISSING_PROPOSE_CAPABILITY_MESSAGE = (
    "This Engram API key cannot propose memory. Re-issue an agent key that "
    "includes the memories:propose capability (run `engram connect` again)."
)


@dataclass(frozen=True)
class McpRuntime:
    server_url: str
    api_key: str
    project_id: str
    team_id: str
    repository_url: str
    agent_runtime: str


def resolve_runtime(
    config_dir: str | None = None,
    *,
    project_override: str = "",
    repository_override: str | None = None,
    team_override: str = "",
) -> McpRuntime | None:
    paths = local_paths(config_dir)
    config = _read_optional_json(paths.config)
    credentials = _read_optional_json(paths.credentials)
    server_url = (
        os.environ.get("ENGRAM_SERVER_URL") or as_string(config.get("server_url"))
    ).rstrip("/")
    api_key = os.environ.get("ENGRAM_API_KEY") or as_string(credentials.get("api_key"))
    project_id = (
        project_override
        or os.environ.get("ENGRAM_PROJECT_ID")
        or as_string(config.get("project_id"))
    )
    team_id = (
        team_override
        or os.environ.get("ENGRAM_TEAM_ID")
        or as_string(config.get("team_id"))
    )
    agent_runtime = os.environ.get("ENGRAM_AGENT_RUNTIME") or "codex"
    if project_id:
        repository_url = ""
    elif repository_override is None:
        repository_url = workspace_repository_url()
    else:
        repository_url = repository_override
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


def _require_runtime(
    config_dir: str | None,
    *,
    project_override: str = "",
    repository_override: str | None = None,
    team_override: str = "",
) -> tuple[McpRuntime | None, str]:
    runtime = resolve_runtime(
        config_dir,
        project_override=project_override,
        repository_override=repository_override,
        team_override=team_override,
    )
    if runtime is None:
        return None, NOT_CONFIGURED_MESSAGE

    return runtime, ""


def _require_runtime_for_arguments(
    config_dir: str | None,
    arguments: dict[str, Any],
) -> tuple[McpRuntime | None, str]:
    repository_override = None
    if INTERNAL_REPOSITORY_URL_ARGUMENT in arguments:
        repository_override = as_string(
            arguments.get(INTERNAL_REPOSITORY_URL_ARGUMENT)
        )

    return _require_runtime(
        config_dir,
        project_override=as_string(arguments.get("project_id")),
        repository_override=repository_override,
        team_override=as_string(arguments.get("team_id")),
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
        "engram_memory_propose": bind(propose_memory),
        "engram_memory_get": bind(memory_get),
        "engram_audit": bind(audit),
    }


def search_memory(
    arguments: dict[str, Any], config_dir: str | None, transport: Transport
) -> str:
    runtime, error = _require_runtime_for_arguments(config_dir, arguments)
    if runtime is None:
        return error

    payload = _scope_payload(runtime)
    payload.update(
        {
            "query": as_string(arguments.get("query")),
            "file_paths": arguments.get("file_paths") or [],
            "symbols": arguments.get("symbols") or [],
            "limit": arguments.get("limit") or 5,
            "request_id": _new_request_id(arguments),
        },
    )
    kinds = _optional_kinds(arguments)
    if kinds is not None:
        payload["kinds"] = kinds
    status, body = post_json(
        transport=transport,
        server_url=runtime.server_url,
        path="/v1/search/",
        api_key=runtime.api_key,
        payload=payload,
    )
    if status != 200:
        return _error_text(status, body)

    warnings_block = render_warnings(body.get("warnings"))
    items = body.get("items")
    if not isinstance(items, list) or not items:
        message = "No memory matched the search."
        if warnings_block:
            return f"{message}\n{warnings_block}"

        return message

    lines: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        lines.append(
            f"[{item.get('citation')}] {item.get('title')} (memory_id={item.get('memory_id')})"
            f"{search_item_suffix(item)}"
        )
        match_line = search_match_line(item)
        if match_line:
            lines.append(match_line)
        lines.append(f"  {item.get('body')}")
    if warnings_block:
        lines.append(warnings_block)

    return "\n".join(lines)


def fetch_context(
    arguments: dict[str, Any], config_dir: str | None, transport: Transport
) -> str:
    session_id = as_string(arguments.get("session_id"))
    if not session_id:
        return "engram_context requires session_id."

    runtime, error = _require_runtime_for_arguments(config_dir, arguments)
    if runtime is None:
        return error

    payload = _scope_payload(runtime)
    payload.update(
        {
            "agent_runtime": runtime.agent_runtime,
            "session_id": session_id,
            "request_id": f"mcp-{uuid.uuid4()}",
            "query": as_string(arguments.get("query")),
            "file_paths": arguments.get("file_paths") or [],
            "symbols": arguments.get("symbols") or [],
            "limit": arguments.get("limit") or 5,
        },
    )
    kinds = _optional_kinds(arguments)
    if kinds is not None:
        payload["kinds"] = kinds
    token_budget = _optional_token_budget(arguments)
    if token_budget is not None:
        payload["token_budget"] = token_budget
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
        message = "Engram returned no context for this session."
        warnings_block = render_warnings(body.get("warnings"))
        if warnings_block:
            return f"{message}\n{warnings_block}"

        return message

    citations = render_citations(body.get("items"))
    if citations:
        return f"{rendered}\n{citations}"

    return rendered


def create_memory_link(
    arguments: dict[str, Any], config_dir: str | None, transport: Transport
) -> str:
    memory_id = as_string(arguments.get("memory_id"))
    link_type = as_string(arguments.get("link_type"))
    target = as_string(arguments.get("target"))
    if not memory_id or not link_type or not target:
        return "engram_memory_link requires memory_id, link_type, and target."

    runtime, error = _require_runtime_for_arguments(config_dir, arguments)
    if runtime is None:
        return error

    payload = _scope_payload(runtime)
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
    runtime, error = _require_runtime_for_arguments(config_dir, arguments)
    if runtime is None:
        return error

    params: dict[str, str] = {"limit": str(arguments.get("limit") or 10)}
    params["request_id"] = _new_request_id(arguments)
    if runtime.project_id:
        params["project_id"] = runtime.project_id
    elif runtime.repository_url:
        params["repository_url"] = runtime.repository_url
    if runtime.team_id:
        params["team_id"] = runtime.team_id
    for name in ("observation_type", "session_id", "since", "until"):
        value = _optional_string_param(arguments, name)
        if value is not None:
            params[name] = value
    offset = _optional_offset(arguments)
    if offset is not None:
        params["offset"] = str(offset)
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
        meta = observation_meta_line(item)
        if meta:
            lines.append(meta)
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

    runtime, error = _require_runtime_for_arguments(config_dir, arguments)
    if runtime is None:
        return error

    payload = _scope_payload(runtime)
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
    if not memory_id or action not in ("stale", "refuted", "confirmed") or not reason:
        return (
            "engram_memory_feedback requires memory_id, action "
            "(stale, refuted, or confirmed), and reason."
        )

    runtime, error = _require_runtime_for_arguments(config_dir, arguments)
    if runtime is None:
        return error

    payload = _scope_payload(runtime)
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
        f"confirmed_at={body.get('confirmed_at')} "
        f"already_applied={body.get('already_applied')}"
    )


def propose_memory(
    arguments: dict[str, Any], config_dir: str | None, transport: Transport
) -> str:
    title = as_string(arguments.get("title"))
    body_text = as_string(arguments.get("body"))
    if not title or not body_text:
        return "engram_memory_propose requires title and body."

    runtime, error = _require_runtime_for_arguments(config_dir, arguments)
    if runtime is None:
        return error

    payload = _scope_payload(runtime)
    payload.update(
        {
            "title": title,
            "body": body_text,
            "kind": as_string(arguments.get("kind")),
            "request_id": _new_request_id(arguments),
        },
    )
    status, body = post_json(
        transport=transport,
        server_url=runtime.server_url,
        path="/v1/memories/propose",
        api_key=runtime.api_key,
        payload=payload,
    )
    if status == 403 and as_string(body.get("code")) == "missing_capability":
        return MISSING_PROPOSE_CAPABILITY_MESSAGE
    if status != 202:
        return _error_text(status, body)

    return (
        f"candidate_id={body.get('candidate_id')} "
        f"status={body.get('status')} "
        f"decision_work_queued={body.get('decision_work_queued')}"
    )


def memory_get(
    arguments: dict[str, Any], config_dir: str | None, transport: Transport
) -> str:
    memory_id = as_string(arguments.get("memory_id"))
    if not memory_id:
        return "engram_memory_get requires memory_id."

    runtime, error = _require_runtime_for_arguments(config_dir, arguments)
    if runtime is None:
        return error

    outcome = read_tools.fetch_memory_get(
        transport,
        _read_scope(runtime),
        memory_id,
        _as_int(arguments.get("from_version")),
        _as_int(arguments.get("to_version")),
    )

    return _mcp_read_text(outcome)


def audit(
    arguments: dict[str, Any], config_dir: str | None, transport: Transport
) -> str:
    runtime, error = _require_runtime_for_arguments(config_dir, arguments)
    if runtime is None:
        return error

    if not runtime.project_id:
        return read_tools.audit_needs_project_message()

    target_id, target_type = read_tools.resolve_audit_target(
        as_string(arguments.get("target_id")),
        as_string(arguments.get("memory_id")),
        as_string(arguments.get("target_type")),
    )
    filters = {key: as_string(arguments.get(key)) for key in ("event_type", "correlation_id", "since", "until")}
    outcome = read_tools.fetch_audit(
        transport,
        _read_scope(runtime),
        target_id,
        target_type,
        _as_int(arguments.get("limit")) or 20,
        filters,
    )

    return _mcp_read_text(outcome)


def _read_scope(runtime: McpRuntime) -> read_tools.ReadScope:
    return read_tools.ReadScope(
        server_url=runtime.server_url,
        api_key=runtime.api_key,
        project_id=runtime.project_id,
        repository_url=runtime.repository_url,
        team_id=runtime.team_id,
    )


def _mcp_read_text(outcome: read_tools.ReadOutcome) -> str:
    if outcome.text is not None:
        return outcome.text

    error = outcome.error
    if error is None:
        return _error_text(0, {})

    if error.code == "http_error":
        return _error_text(error.status, error.body or {})

    return error.message


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


def _as_int(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _optional_kinds(arguments: dict[str, Any]) -> list[Any] | None:
    value = arguments.get("kinds")
    if value in (None, [], ""):
        return None
    if isinstance(value, list):
        return value

    raise ValueError("kinds must be an array of strings")


def _optional_token_budget(arguments: dict[str, Any]) -> int | None:
    value = arguments.get("token_budget")
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return value

    raise ValueError("token_budget must be an integer")


def _optional_string_param(arguments: dict[str, Any], name: str) -> str | None:
    value = arguments.get(name)
    if isinstance(value, str):
        return value or None
    if value is None or (isinstance(value, (list, tuple, set, dict)) and not value):
        return None

    raise ValueError(f"{name} must be a string")


def _optional_offset(arguments: dict[str, Any]) -> int | None:
    value = arguments.get("offset")
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return value if value != 0 else None

    raise ValueError("offset must be an integer")


def _new_request_id(arguments: dict[str, Any]) -> str:
    provided = as_string(arguments.get("request_id"))

    return provided or f"mcp-{uuid.uuid4()}"


def _error_text(status: int, body: dict[str, object]) -> str:
    code = as_string(body.get("code")) or "error"
    if status == 404 and code == "project_not_found":
        return PROJECT_NOT_FOUND_MESSAGE

    if is_kinds_error_body(body):
        return KINDS_ERROR_MESSAGE

    detail = as_string(body.get("detail")) or "request failed"

    return f"Engram call failed: HTTP {status} {code}: {detail}"
