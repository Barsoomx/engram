from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from urllib.parse import urlencode


Transport = Callable[
    [str, str, dict[str, str], dict[str, object] | None, float],
    tuple[int, dict[str, object]],
]


def urllib_transport(
    method: str,
    url: str,
    headers: dict[str, str],
    payload: dict[str, object] | None,
    timeout: float,
) -> tuple[int, dict[str, object]]:
    try:
        request_headers = dict(headers)
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            request_headers.setdefault("Content-Type", "application/json")
        request_headers.setdefault("Accept", "application/json")
        request = urllib.request.Request(
            url, data=body, headers=request_headers, method=method
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, parse_json_body(response.read())
    except urllib.error.HTTPError as error:
        return error.code, parse_json_body(error.read())
    except (OSError, TimeoutError, urllib.error.URLError, ValueError):
        return 503, {"code": "server_unavailable", "detail": "Server is unavailable"}


def parse_json_body(data: bytes) -> dict[str, object]:
    if not data:
        return {}
    try:
        payload = json.loads(data.decode())
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"code": "invalid_response", "detail": "Server returned invalid JSON"}

    if not isinstance(payload, dict):
        return {"code": "invalid_response", "detail": "Server returned non-object JSON"}

    return payload


def post_dry_run(
    *,
    transport: Transport,
    server_url: str,
    api_key: str,
    project_id: str,
    team_id: str,
    agent_runtime: str,
    agent_version: str,
    request_id: str,
    timeout: float = 2.0,
) -> tuple[int, dict[str, object]]:
    payload: dict[str, object] = {
        "project_id": project_id,
        "agent_runtime": agent_runtime,
        "agent_version": agent_version,
        "request_id": request_id,
    }
    if team_id:
        payload["team_id"] = team_id

    return transport(
        "POST",
        f"{server_url}/v1/hooks/dry-run",
        {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        payload,
        timeout,
    )


def post_json(
    *,
    transport: Transport,
    server_url: str,
    path: str,
    api_key: str,
    payload: dict[str, object],
    timeout: float = 2.0,
) -> tuple[int, dict[str, object]]:
    return transport(
        "POST",
        f"{server_url}{path}",
        {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        payload,
        timeout,
    )


def get_health(
    *,
    transport: Transport,
    server_url: str,
    timeout: float = 2.0,
) -> tuple[int, dict[str, object]]:
    return transport("GET", f"{server_url}/-/healthz/", {}, None, timeout)


def get_json(
    *,
    transport: Transport,
    server_url: str,
    path: str,
    api_key: str,
    params: dict[str, str] | None = None,
    timeout: float = 2.0,
) -> tuple[int, dict[str, object]]:
    query = ""
    if params:
        query = "?" + urlencode(params)

    return transport(
        "GET",
        f"{server_url}{path}{query}",
        {"Authorization": f"Bearer {api_key}"},
        None,
        timeout,
    )
