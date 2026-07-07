from __future__ import annotations

import json
import sys
from argparse import Namespace
from typing import Any, TextIO

from engram_cli.http import Transport
from engram_cli.mcp_tools import ToolFn, build_tools

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "engram"
SERVER_VERSION = "0.3.2"

ToolMap = dict[str, ToolFn]


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
                "error text)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "file_paths": {"type": "array", "items": {"type": "string"}},
                    "symbols": {"type": "array", "items": {"type": "string"}},
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
                "when the injected Engram context looks stale."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "query": {"type": "string"},
                    "file_paths": {"type": "array", "items": {"type": "string"}},
                    "symbols": {"type": "array", "items": {"type": "string"}},
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
                "ground-truth detail, or to audit what Engram captured."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer"},
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
        arguments = params.get("arguments") or {}
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
