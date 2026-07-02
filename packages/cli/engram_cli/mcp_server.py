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


def list_tools() -> list[dict[str, object]]:
    return [
        {
            "name": "engram_search",
            "description": "Search approved Engram memory for the connected project.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "file_paths": {"type": "array", "items": {"type": "string"}},
                    "symbols": {"type": "array", "items": {"type": "string"}},
                    "limit": {"type": "integer"},
                },
                "required": ["query"],
            },
        },
        {
            "name": "engram_context",
            "description": "Request a session-start context bundle from Engram memory.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "query": {"type": "string"},
                    "file_paths": {"type": "array", "items": {"type": "string"}},
                    "symbols": {"type": "array", "items": {"type": "string"}},
                    "limit": {"type": "integer"},
                },
                "required": ["session_id"],
            },
        },
        {
            "name": "engram_memory_link",
            "description": "Attach a file/symbol/commit/issue link to an approved memory.",
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
                },
                "required": ["memory_id", "link_type", "target"],
            },
        },
        {
            "name": "engram_observations",
            "description": "List recent Engram observations for the connected project.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer"},
                },
                "required": [],
            },
        },
        {
            "name": "engram_memory_version",
            "description": "Update an approved memory body, creating a new reviewed version.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "memory_id": {"type": "string"},
                    "body": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["memory_id", "body"],
            },
        },
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
    ]


def handle_request(request: dict[str, Any], tools: ToolMap) -> dict[str, Any] | None:
    method = request.get("method")
    req_id = request.get("id")
    params = request.get("params") or {}

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
