from __future__ import annotations

import json
import os
import sys
from argparse import Namespace
from importlib import metadata
from pathlib import Path
from typing import Any, TextIO

from engram_cli.commands import git_remote_url
from engram_cli.http import Transport
from engram_cli.mcp_tools import (
    INTERNAL_REPOSITORY_URL_ARGUMENT,
    ToolFn,
    build_tools,
)

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "engram"
CODEX_TURN_METADATA_KEY = "x-codex-turn-metadata"
CODEX_TURN_METADATA_MAX_LENGTH = 65536
CODEX_MCP_SCOPE_ENV = "ENGRAM_MCP_CODEX_SCOPE"


def _server_version() -> str:
    try:
        return metadata.version("engram-connect")
    except metadata.PackageNotFoundError:
        return "bundled"


SERVER_VERSION = _server_version()

ToolMap = dict[str, ToolFn]


def _codex_turn_metadata(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or len(value) > CODEX_TURN_METADATA_MAX_LENGTH:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}

    return parsed if isinstance(parsed, dict) else {}


def _codex_repository_url(params: dict[str, Any]) -> str:
    request_meta = params.get("_meta")
    if not isinstance(request_meta, dict):
        return ""
    thread_id = request_meta.get("threadId")
    if not isinstance(thread_id, str) or not thread_id.strip():
        return ""
    turn_metadata = _codex_turn_metadata(request_meta.get(CODEX_TURN_METADATA_KEY))
    metadata_ids = (
        turn_metadata.get("session_id"),
        turn_metadata.get("thread_id"),
    )
    if any(value != thread_id for value in metadata_ids):
        return ""
    workspaces = turn_metadata.get("workspaces")
    if not isinstance(workspaces, dict) or len(workspaces) != 1:
        return ""
    workspace, workspace_metadata = next(iter(workspaces.items()))
    if (
        not isinstance(workspace, str)
        or not Path(workspace).is_absolute()
        or not isinstance(workspace_metadata, dict)
    ):
        return ""

    return git_remote_url(workspace)


def list_tools() -> list[dict[str, object]]:
    return [
        {
            "name": "engram_search",
            "description": (
                "Step 1 - ALWAYS search project memory BEFORE starting any "
                "non-trivial task (bug fix, feature, refactor, debugging). "
                "Returns prior decisions, gotchas, incidents and architecture "
                "notes ranked by relevance. Call it when the user references "
                "past work ('did we', 'last time', 'as before'), names a "
                "subsystem, or reports an error you have not seen this "
                "session. Prefer short 2-4 word queries (symptom, component, "
                "error text). Filter by kinds=[convention,decision] to fetch "
                "project conventions or decisions on a topic (e.g. gitlab "
                "workflow)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "file_paths": {"type": "array", "items": {"type": "string"}},
                    "symbols": {"type": "array", "items": {"type": "string"}},
                    "kinds": {"type": "array", "items": {"type": "string"}},
                    "limit": {"type": "integer"},
                    "project_id": {"type": "string"},
                },
                "required": ["query"],
            },
        },
        {
            "name": "engram_context",
            "description": (
                "Re-request the memory context bundle that is injected at "
                "session start (recent and relevant approved memories for "
                "this project). Use after /clear or context compaction, or "
                "when the injected Engram context looks stale. Filter by "
                "kinds=[convention,decision] to fetch project conventions or "
                "decisions on a topic (e.g. gitlab workflow)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "query": {"type": "string"},
                    "file_paths": {"type": "array", "items": {"type": "string"}},
                    "symbols": {"type": "array", "items": {"type": "string"}},
                    "kinds": {"type": "array", "items": {"type": "string"}},
                    "token_budget": {"type": "integer"},
                    "limit": {"type": "integer"},
                    "project_id": {"type": "string"},
                },
                "required": ["session_id"],
            },
        },
        {
            "name": "engram_memory_link",
            "description": (
                "Attach a file/symbol/commit/issue link to an approved "
                "memory so future retrieval can find it by exact file path "
                "or symbol match."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "memory_id": {"type": "string"},
                    "link_type": {
                        "type": "string",
                        "enum": ["file", "symbol", "commit", "issue"],
                    },
                    "target": {"type": "string"},
                    "label": {"type": "string"},
                    "project_id": {"type": "string"},
                },
                "required": ["memory_id", "link_type", "target"],
            },
        },
        {
            "name": "engram_observations",
            "description": (
                "Step 2 - list recent raw observations (prompts, tool "
                "activity, hook events) captured for the connected project. "
                "Use to corroborate a memory found via engram_search with "
                "ground-truth detail, or to audit what Engram captured. Time "
                "filters since/until bound ingestion time (created_at, until "
                "exclusive); results still display and sort by observed_at."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer"},
                    "observation_type": {"type": "string"},
                    "session_id": {"type": "string"},
                    "since": {
                        "type": "string",
                        "description": (
                            "ISO-8601 lower bound (inclusive) on ingestion time "
                            "(created_at), NOT the displayed observed_at. With "
                            "delayed ingestion a returned row's observed_at may "
                            "fall outside this window."
                        ),
                    },
                    "until": {
                        "type": "string",
                        "description": (
                            "ISO-8601 upper bound (exclusive) on ingestion time "
                            "(created_at), NOT the displayed observed_at. A row "
                            "whose created_at equals until is excluded."
                        ),
                    },
                    "offset": {"type": "integer"},
                    "project_id": {"type": "string"},
                },
                "required": [],
            },
        },
        {
            "name": "engram_memory_version",
            "description": (
                "Update an approved memory body, creating a new reviewed "
                "version. Use when you verified materially better "
                "information than what the memory states."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "memory_id": {"type": "string"},
                    "body": {"type": "string"},
                    "reason": {"type": "string"},
                    "project_id": {"type": "string"},
                },
                "required": ["memory_id", "body"],
            },
        },
        {
            "name": "engram_memory_feedback",
            "description": (
                "Step 3 - close the loop: the moment you discover an "
                "injected or retrieved memory is outdated or wrong, mark it "
                "stale or refuted with a reason. Clean memory improves every "
                "future session; do not silently ignore bad memory."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "memory_id": {"type": "string"},
                    "action": {"type": "string", "enum": ["stale", "refuted"]},
                    "reason": {"type": "string"},
                    "project_id": {"type": "string"},
                },
                "required": ["memory_id", "action", "reason"],
            },
        },
    ]


def handle_request(request: dict[str, Any], tools: ToolMap) -> dict[str, Any] | None:
    method = request.get("method")
    req_id = request.get("id")
    params = request.get("params")
    if not isinstance(params, dict):
        params = {}

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }

    if method == "notifications/initialized":
        return None

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": list_tools()}}

    if method == "tools/call":
        name = params.get("name")
        raw_arguments = params.get("arguments")
        arguments = dict(raw_arguments) if isinstance(raw_arguments, dict) else {}
        arguments.pop(INTERNAL_REPOSITORY_URL_ARGUMENT, None)
        repository_url = _codex_repository_url(params)
        if repository_url or os.environ.get(CODEX_MCP_SCOPE_ENV) == "1":
            arguments[INTERNAL_REPOSITORY_URL_ARGUMENT] = repository_url
        tool_fn = tools.get(name) if isinstance(name, str) else None
        if tool_fn is None:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"unknown tool {name}"},
            }
        try:
            text = tool_fn(arguments)
        except Exception as error:  # keep the stdio loop alive on tool bugs
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": f"tool {name} failed: {error}"},
            }

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"content": [{"type": "text", "text": text}]},
        }

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"unknown method {method}"},
    }


def run_server(
    tools: ToolMap,
    stdin: Any = sys.stdin,
    stdout: Any = sys.stdout,
) -> None:
    for line in stdin:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            request = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(request, dict):
            continue
        response = handle_request(request, tools)
        if response is not None:
            stdout.write(json.dumps(response) + "\n")
            stdout.flush()


def run_mcp_serve(
    args: Namespace,
    stdin: TextIO,
    stdout: TextIO,
    transport: Transport | None = None,
) -> int:
    tools = build_tools(getattr(args, "config_dir", None), transport)
    run_server(tools, stdin=stdin, stdout=stdout)

    return 0
