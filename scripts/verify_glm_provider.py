#!/usr/bin/env python3
"""Verify a GLM/ZhipuAI Anthropic-compatible provider key end to end.

Reads the key from the environment only; never logs or commits it.

Usage:
    export ENGRAM_E2E_PROVIDER_KEY="<your glm/z.ai key>"
    python3 scripts/verify_glm_provider.py

Env:
    ENGRAM_E2E_PROVIDER_KEY   required, the provider API key (z.ai auth token)
    ENGRAM_PROVIDER_BASE_URL  optional, default https://api.z.ai/api/anthropic
    ENGRAM_PROVIDER_MODEL     optional, default glm-4.7
    ENGRAM_PROVIDER_PROMPT    optional, default 'Reply with exactly: ENGRAM_OK'
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    key = os.environ.get('ENGRAM_E2E_PROVIDER_KEY')
    if not key:
        print('Set ENGRAM_E2E_PROVIDER_KEY before running this script.')

        return 1

    base_url = os.environ.get('ENGRAM_PROVIDER_BASE_URL', 'https://api.z.ai/api/anthropic').rstrip('/')
    model = os.environ.get('ENGRAM_PROVIDER_MODEL', 'glm-4.7')
    prompt = os.environ.get('ENGRAM_PROVIDER_PROMPT', 'Reply with exactly: ENGRAM_OK')
    body = json.dumps(
        {'model': model, 'max_tokens': 50, 'messages': [{'role': 'user', 'content': prompt}]},
    ).encode()
    request = urllib.request.Request(
        f'{base_url}/v1/messages',
        data=body,
        headers={
            'x-api-key': key,
            'anthropic-version': '2023-06-01',
            'content-type': 'application/json',
        },
        method='POST',
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data = json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        print(f'HTTP ERROR {error.code}')
        print(error.read().decode()[:500])

        return 2
    except urllib.error.URLError as error:
        print(f'NETWORK ERROR {error.reason}')

        return 3

    print('STATUS: ok')
    print(f'MODEL: {data.get("model")}')
    print(f'CONTENT: {data["content"][0]["text"]}')
    print(f'USAGE: {data.get("usage")}')

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
