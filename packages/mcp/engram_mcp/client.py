from __future__ import annotations

import json
import os
import urllib.request
from typing import Any


def _missing_config_message() -> str:
    return (
        'Engram MCP bridge is not configured. Set ENGRAM_SERVER_URL, '
        'ENGRAM_API_KEY, and ENGRAM_PROJECT_ID before calling engram tools.'
    )


def search_memory(arguments: dict[str, Any]) -> str:
    server_url = os.environ.get('ENGRAM_SERVER_URL', '').rstrip('/')
    api_key = os.environ.get('ENGRAM_API_KEY', '')
    project_id = os.environ.get('ENGRAM_PROJECT_ID', '')
    if not server_url or not api_key or not project_id:

        return _missing_config_message()

    payload: dict[str, Any] = {
        'project_id': project_id,
        'query': arguments.get('query', ''),
        'file_paths': arguments.get('file_paths', []) or [],
        'symbols': arguments.get('symbols', []) or [],
        'limit': arguments.get('limit', 5),
    }
    team_id = os.environ.get('ENGRAM_TEAM_ID', '')
    if team_id:
        payload['team_id'] = team_id
    body = json.dumps(payload).encode()
    request = urllib.request.Request(
        f'{server_url}/v1/search/',
        data=body,
        headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            data = json.loads(response.read().decode())
    except Exception as error:
        return f'Engram search failed: {error}'

    items = data.get('items', [])
    if not items:

        return 'No memory matched the search.'

    lines = []
    for item in items:
        lines.append(f"[{item.get('citation')}] {item.get('title')}")
        lines.append(f"  {item.get('body')}")

    return '\n'.join(lines)
