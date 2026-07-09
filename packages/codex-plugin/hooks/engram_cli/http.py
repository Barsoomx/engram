from __future__ import annotations

import functools
import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from urllib.parse import urlencode


Transport = Callable[
    [str, str, dict[str, str], dict[str, object] | None, float],
    tuple[int, dict[str, object]],
]

_RETRY_BACKOFF_SECONDS = 0.25
_RETRYABLE_HTTP_CODES = (502, 503, 504)


def urllib_transport(
    method: str,
    url: str,
    headers: dict[str, str],
    payload: dict[str, object] | None,
    timeout: float,
    max_attempts: int = 1,
) -> tuple[int, dict[str, object]]:
    request_headers = dict(headers)
    body = None
    if payload is not None:
        body = json.dumps(payload).encode()
        request_headers.setdefault("Content-Type", "application/json")
    request_headers.setdefault("Accept", "application/json")

    for attempt in range(max_attempts):
        try:
            request = urllib.request.Request(
                url, data=body, headers=request_headers, method=method
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, parse_json_body(response.read())
        except urllib.error.HTTPError as error:
            if error.code not in _RETRYABLE_HTTP_CODES or attempt == max_attempts - 1:
                return error.code, parse_json_body(error.read())

        except (OSError, TimeoutError, urllib.error.URLError):
            if attempt == max_attempts - 1:
                return 503, {
                    "code": "server_unavailable",
                    "detail": "Server is unavailable",
                }

        except ValueError:
            return 503, {
                "code": "server_unavailable",
                "detail": "Server is unavailable",
            }

        time.sleep(_RETRY_BACKOFF_SECONDS)


def _with_max_attempts(transport: Transport, max_attempts: int) -> Transport:
    if transport is urllib_transport:
        return functools.partial(urllib_transport, max_attempts=max_attempts)

    return transport


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
    timeout: float = 5.0,
    max_attempts: int = 2,
) -> tuple[int, dict[str, object]]:
    payload: dict[str, object] = {
        "agent_runtime": agent_runtime,
        "agent_version": agent_version,
        "request_id": request_id,
    }
    if project_id:
        payload["project_id"] = project_id
    if team_id:
        payload["team_id"] = team_id

    return _with_max_attempts(transport, max_attempts)(
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
    timeout: float = 30.0,
    max_attempts: int = 2,
) -> tuple[int, dict[str, object]]:
    return _with_max_attempts(transport, max_attempts)(
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


def probe_health(
    *,
    transport: Transport,
    server_url: str,
    timeout: float = 2.0,
) -> bool:
    status, body = get_health(transport=transport, server_url=server_url, timeout=timeout)

    return status == 200 and body.get("status") == "ok"


def post_login(
    *,
    transport: Transport,
    server_url: str,
    username: str,
    password: str,
    timeout: float = 5.0,
) -> tuple[int, dict[str, object]]:
    payload: dict[str, object] = {"username": username, "password": password}

    return transport(
        "POST",
        f"{server_url}/v1/auth/login",
        {"Content-Type": "application/json"},
        payload,
        timeout,
    )


def admin_get(
    *,
    transport: Transport,
    server_url: str,
    path: str,
    drf_token: str,
    organization_id: str | None = None,
    timeout: float = 5.0,
) -> tuple[int, dict[str, object]]:
    headers: dict[str, str] = {
        "Authorization": f"Token {drf_token}",
        "Accept": "application/json",
    }
    if organization_id:
        headers["X-Engram-Organization"] = organization_id

    return transport("GET", f"{server_url}{path}", headers, None, timeout)


def admin_post(
    *,
    transport: Transport,
    server_url: str,
    path: str,
    drf_token: str,
    payload: dict[str, object],
    organization_id: str | None = None,
    timeout: float = 5.0,
) -> tuple[int, dict[str, object]]:
    headers: dict[str, str] = {
        "Authorization": f"Token {drf_token}",
        "Content-Type": "application/json",
    }
    if organization_id:
        headers["X-Engram-Organization"] = organization_id

    return transport("POST", f"{server_url}{path}", headers, payload, timeout)


def get_json(
    *,
    transport: Transport,
    server_url: str,
    path: str,
    api_key: str,
    params: dict[str, str] | None = None,
    timeout: float = 30.0,
    max_attempts: int = 2,
) -> tuple[int, dict[str, object]]:
    query = ""
    if params:
        query = "?" + urlencode(params)

    return _with_max_attempts(transport, max_attempts)(
        "GET",
        f"{server_url}{path}{query}",
        {"Authorization": f"Bearer {api_key}"},
        None,
        timeout,
    )
