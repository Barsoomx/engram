from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from typing import Any


def _missing_config_message() -> str:
    return (
        "Engram MCP bridge is not configured. Set ENGRAM_SERVER_URL, "
        "ENGRAM_API_KEY, and ENGRAM_PROJECT_ID before calling engram tools."
    )


def _server_call(
    path: str, payload: dict[str, Any], method: str = "POST"
) -> dict[str, Any] | str:
    server_url = os.environ.get("ENGRAM_SERVER_URL", "").rstrip("/")
    api_key = os.environ.get("ENGRAM_API_KEY", "")
    project_id = os.environ.get("ENGRAM_PROJECT_ID", "")
    if not server_url or not api_key or not project_id:
        return _missing_config_message()

    body = json.dumps(payload).encode()
    request = urllib.request.Request(
        f"{server_url}{path}",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode())
    except Exception as error:
        return f"Engram call failed: {error}"


def _server_get(path: str, params: dict[str, Any]) -> dict[str, Any] | str:
    server_url = os.environ.get("ENGRAM_SERVER_URL", "").rstrip("/")
    api_key = os.environ.get("ENGRAM_API_KEY", "")
    project_id = os.environ.get("ENGRAM_PROJECT_ID", "")
    if not server_url or not api_key or not project_id:
        return _missing_config_message()

    query = urllib.parse.urlencode({k: v for k, v in params.items() if v != ""})
    url = f"{server_url}{path}?{query}" if query else f"{server_url}{path}"
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {api_key}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode())
    except Exception as error:
        return f"Engram call failed: {error}"


def _base_payload(arguments: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {"project_id": os.environ.get("ENGRAM_PROJECT_ID", "")}
    team_id = os.environ.get("ENGRAM_TEAM_ID", "")
    if team_id:
        payload["team_id"] = team_id

    return payload


def search_memory(arguments: dict[str, Any]) -> str:
    payload = _base_payload(arguments)
    payload.update(
        {
            "query": arguments.get("query", ""),
            "file_paths": arguments.get("file_paths", []) or [],
            "symbols": arguments.get("symbols", []) or [],
            "limit": arguments.get("limit", 5),
        },
    )
    data = _server_call("/v1/search/", payload)
    if isinstance(data, str):
        return data

    items = data.get("items", [])
    if not items:
        return "No memory matched the search."

    lines = []
    for item in items:
        lines.append(f"[{item.get('citation')}] {item.get('title')}")
        lines.append(f"  {item.get('body')}")

    return "\n".join(lines)


def fetch_context(arguments: dict[str, Any]) -> str:
    session_id = arguments.get("session_id", "")
    if not session_id:
        return "engram_context requires session_id."

    payload = _base_payload(arguments)
    payload.update(
        {
            "agent_runtime": os.environ.get("ENGRAM_AGENT_RUNTIME", "codex"),
            "session_id": session_id,
            "request_id": arguments.get("request_id", f"mcp-{session_id}"),
            "query": arguments.get("query", ""),
            "file_paths": arguments.get("file_paths", []) or [],
            "symbols": arguments.get("symbols", []) or [],
            "limit": arguments.get("limit", 5),
        },
    )
    data = _server_call("/v1/context/session-start", payload)
    if isinstance(data, str):
        return data

    rendered = data.get("rendered_context") or ""
    if not rendered:
        return "Engram returned no context for this session."

    return rendered


def create_memory_link(arguments: dict[str, Any]) -> str:
    memory_id = arguments.get("memory_id", "")
    link_type = arguments.get("link_type", "")
    target = arguments.get("target", "")
    if not memory_id or not link_type or not target:
        return "engram_memory_link requires memory_id, link_type, and target."

    payload = _base_payload(arguments)
    payload.update(
        {
            "link_type": link_type,
            "target": target,
            "label": arguments.get("label", ""),
            "request_id": arguments.get("request_id", f"mcp-link-{memory_id}"),
        },
    )
    data = _server_call(f"/v1/memories/{memory_id}/links", payload)
    if isinstance(data, str):
        return data

    return (
        f"link_id={data.get('link_id')} link_type={data.get('link_type')} "
        f"target={data.get('target')} created={data.get('created')}"
    )


def list_observations(arguments: dict[str, Any]) -> str:
    params: dict[str, Any] = {
        "project_id": os.environ.get("ENGRAM_PROJECT_ID", ""),
        "limit": arguments.get("limit", 10),
    }
    team_id = os.environ.get("ENGRAM_TEAM_ID", "")
    if team_id:
        params["team_id"] = team_id

    data = _server_get("/v1/observations/", params)
    if isinstance(data, str):
        return data

    items = data.get("items", [])
    if not items:
        return "No observations found."

    lines = []
    for item in items:
        lines.append(f"[{item.get('observation_type')}] {item.get('title')}")
        body = item.get("body") or ""
        if body:
            lines.append(f"  {body}")

    return "\n".join(lines)


def update_memory_version(arguments: dict[str, Any]) -> str:
    memory_id = arguments.get("memory_id", "")
    body = arguments.get("body", "")
    if not memory_id or not body:
        return "engram_memory_version requires memory_id and body."

    payload = _base_payload(arguments)
    payload.update(
        {
            "body": body,
            "reason": arguments.get("reason", ""),
            "request_id": arguments.get("request_id", f"mcp-version-{memory_id}"),
        },
    )
    data = _server_call(f"/v1/memories/{memory_id}/version", payload)
    if isinstance(data, str):
        return data

    return (
        f"memory_id={data.get('memory_id') or memory_id} "
        f"version={data.get('version')} reason={data.get('reason')}"
    )
