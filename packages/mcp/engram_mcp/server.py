from __future__ import annotations

import json
import sys
from collections.abc import Callable
from typing import Any

PROTOCOL_VERSION = '2024-11-05'
SERVER_NAME = 'engram'
SERVER_VERSION = '0.1.0'

SearchFn = Callable[[dict[str, Any]], str]


def list_tools() -> list[dict[str, object]]:
    return [
        {
            'name': 'engram_search',
            'description': 'Search approved Engram memory for the connected project.',
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'query': {'type': 'string'},
                    'file_paths': {'type': 'array', 'items': {'type': 'string'}},
                    'symbols': {'type': 'array', 'items': {'type': 'string'}},
                    'limit': {'type': 'integer'},
                },
                'required': ['query'],
            },
        },
    ]


def handle_request(request: dict[str, Any], search_fn: SearchFn) -> dict[str, Any] | None:
    method = request.get('method')
    req_id = request.get('id')
    params = request.get('params') or {}

    if method == 'initialize':

        return {
            'jsonrpc': '2.0',
            'id': req_id,
            'result': {
                'protocolVersion': PROTOCOL_VERSION,
                'capabilities': {'tools': {}},
                'serverInfo': {'name': SERVER_NAME, 'version': SERVER_VERSION},
            },
        }

    if method == 'notifications/initialized':

        return None

    if method == 'tools/list':

        return {'jsonrpc': '2.0', 'id': req_id, 'result': {'tools': list_tools()}}

    if method == 'tools/call':
        name = params.get('name')
        arguments = params.get('arguments') or {}
        if name == 'engram_search':
            text = search_fn(arguments)

            return {
                'jsonrpc': '2.0',
                'id': req_id,
                'result': {'content': [{'type': 'text', 'text': text}]},
            }

        return {
            'jsonrpc': '2.0',
            'id': req_id,
            'error': {'code': -32601, 'message': f'unknown tool {name}'},
        }

    return {
        'jsonrpc': '2.0',
        'id': req_id,
        'error': {'code': -32601, 'message': f'unknown method {method}'},
    }


def run_server(
    search_fn: SearchFn,
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
        response = handle_request(request, search_fn)
        if response is not None:
            stdout.write(json.dumps(response) + '\n')
            stdout.flush()
