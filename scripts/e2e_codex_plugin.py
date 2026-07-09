from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
from collections.abc import Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / 'deploy/test/Dockerfile.codex-plugin-e2e'
CODEX_VERSION = os.environ.get('CODEX_VERSION', '0.142.5')
PNPM_VERSION = os.environ.get('PNPM_VERSION', '11.9.0')
IMAGE = f'engram-codex-plugin-e2e:{CODEX_VERSION}'
PLUGIN_ID = 'engram@engram-marketplace'
MODEL_NAME = 'engram-e2e-model'
MODEL_KEY = 'sk-codex-e2e-model-key'
ENGRAM_KEY = 'egk_codex_e2e_not_a_real_secret'
REPOSITORY_URL = 'https://example.test/engram/codex-e2e.git'
SESSION_CONTEXT_MARKER = 'ENGRAM_SESSION_CONTEXT_MARKER'
PROMPT_CONTEXT_MARKER = 'ENGRAM_PROMPT_CONTEXT_MARKER'
MCP_RESULT_MARKER = 'ENGRAM_MCP_RESULT_MARKER'
EXPECTED_MCP_TOOLS = {
    'engram_context',
    'engram_memory_feedback',
    'engram_memory_link',
    'engram_memory_version',
    'engram_observations',
    'engram_search',
}
EXPECTED_HOOK_PATHS = {
    '/v1/hooks/post-tool-use',
    '/v1/hooks/session-end',
    '/v1/hooks/session-start',
    '/v1/hooks/user-prompt-submit',
}


class E2EError(Exception):
    pass


def progress(message: str) -> None:
    print(f'[codex-plugin-e2e] {message}', flush=True)


def redact(value: str) -> str:
    return value.replace(ENGRAM_KEY, '[REDACTED]').replace(MODEL_KEY, '[REDACTED]')


def run(
    args: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    timeout: int = 180,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        args,
        cwd=cwd,
        env=env,
        input=input_text,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if check and completed.returncode != 0:
        command = ' '.join(args).replace(ENGRAM_KEY, '[REDACTED]')
        stdout = redact(completed.stdout)
        stderr = redact(completed.stderr)

        raise E2EError(
            f'command failed ({completed.returncode}): {command}\n'
            f'stdout:\n{stdout[-8000:]}\nstderr:\n{stderr[-8000:]}'
        )

    return completed


def run_json(
    args: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int = 180,
) -> dict[str, Any]:
    completed = run(args, cwd=cwd, env=env, timeout=timeout)
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise E2EError(f'expected JSON from {" ".join(args)}:\n{completed.stdout}') from error
    if not isinstance(payload, dict):
        raise E2EError(f'expected JSON object from {" ".join(args)}')

    return payload


def repeat_native_command(
    args: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str],
) -> None:
    completed = run(args, cwd=cwd, env=env, check=False)
    combined = f'{completed.stdout}\n{completed.stderr}'.strip()
    progress(
        f'repeat native command exited {completed.returncode}: '
        f'{" ".join(args[:4])}; {combined[:500] or "no output"}'
    )
    if completed.returncode != 0 and 'already' not in combined.lower():
        raise E2EError(
            f'repeated native command failed for a reason other than already-present state: '
            f'{" ".join(args)}\n{combined}'
        )


def tree_snapshot(root: Path) -> dict[str, tuple[str, int, str]]:
    snapshot: dict[str, tuple[str, int, str]] = {}
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root).as_posix()
        mode = stat.S_IMODE(path.lstat().st_mode)
        if path.is_symlink():
            snapshot[relative] = ('symlink', mode, os.readlink(path))
        elif path.is_dir():
            snapshot[relative] = ('directory', mode, '')
        else:
            snapshot[relative] = ('file', mode, hashlib.sha256(path.read_bytes()).hexdigest())

    return snapshot


class Scenario:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.engram_requests: list[tuple[str, dict[str, Any]]] = []
        self.model_requests: list[dict[str, Any]] = []
        self.errors: list[str] = []

    def record_engram(self, path: str, payload: dict[str, Any]) -> None:
        with self.lock:
            self.engram_requests.append((path, payload))

    def next_model_events(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        with self.lock:
            self.model_requests.append(payload)
            request_number = len(self.model_requests)

        if request_number == 1:
            call = find_engram_search_call(payload)
            item: dict[str, Any] = {
                'type': 'function_call',
                'call_id': 'engram-search-call-1',
                'name': call['name'],
                'arguments': json.dumps({'query': 'codex harness'}),
            }
            if call.get('namespace'):
                item['namespace'] = call['namespace']

            return [
                response_created('resp-1'),
                {'type': 'response.output_item.done', 'item': item},
                response_completed('resp-1'),
            ]
        if request_number == 2:
            return [
                response_created('resp-2'),
                {
                    'type': 'response.output_item.done',
                    'item': {
                        'type': 'message',
                        'role': 'assistant',
                        'id': 'message-2',
                        'content': [{'type': 'output_text', 'text': 'CODEX_E2E_OK'}],
                    },
                },
                response_completed('resp-2'),
            ]

        raise E2EError(f'unexpected third model request: {payload}')


def response_created(response_id: str) -> dict[str, Any]:
    return {'type': 'response.created', 'response': {'id': response_id}}


def response_completed(response_id: str) -> dict[str, Any]:
    return {
        'type': 'response.completed',
        'response': {
            'id': response_id,
            'usage': {
                'input_tokens': 0,
                'input_tokens_details': None,
                'output_tokens': 0,
                'output_tokens_details': None,
                'total_tokens': 0,
            },
        },
    }


def find_engram_search_call(payload: dict[str, Any]) -> dict[str, str]:
    tools = payload.get('tools')
    if not isinstance(tools, list):
        raise E2EError('Codex model request did not include a tools array')
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        name = tool.get('name')
        if isinstance(name, str) and name.endswith('engram_search'):
            return {'name': name}
        children = tool.get('tools')
        if not isinstance(name, str) or not isinstance(children, list):
            continue
        for child in children:
            if isinstance(child, dict) and child.get('name') == 'engram_search':
                return {'namespace': name, 'name': 'engram_search'}

    raise E2EError(f'Codex did not expose engram_search to the model: {tools}')


def make_handler(scenario: Scenario) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, _format: str, *_args: object) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802
            if self.path.rstrip('/') == '/v1/models':
                self._write_json(
                    200,
                    {
                        'object': 'list',
                        'data': [{'id': MODEL_NAME, 'object': 'model', 'owned_by': 'engram-e2e'}],
                    },
                )

                return
            if self.path == '/-/healthz/':
                self._write_json(200, {'status': 'ok'})

                return

            self._write_json(404, {'detail': 'not found'})

        def do_POST(self) -> None:  # noqa: N802
            try:
                payload = self._read_json()
                if self.path.rstrip('/') == '/v1/responses':
                    self._require_bearer(MODEL_KEY)
                    self._write_sse(scenario.next_model_events(payload))

                    return

                self._require_bearer(ENGRAM_KEY)
                scenario.record_engram(self.path, payload)
                if self.path == '/v1/context/session-start':
                    self._write_json(
                        200,
                        {
                            'items': [
                                {
                                    'citation': 'M1',
                                    'title': SESSION_CONTEXT_MARKER,
                                    'kind': 'decision',
                                    'confidence': 'high',
                                    'body': 'Codex SessionStart context was injected.',
                                }
                            ]
                        },
                    )

                    return
                if self.path == '/v1/context/user-prompt-submit':
                    self._write_json(
                        200,
                        {
                            'items': [{'citation': 'M2', 'title': PROMPT_CONTEXT_MARKER}],
                            'rendered_context': PROMPT_CONTEXT_MARKER,
                        },
                    )

                    return
                if self.path == '/v1/search/':
                    self._write_json(
                        200,
                        {
                            'items': [
                                {
                                    'citation': 'M3',
                                    'title': MCP_RESULT_MARKER,
                                    'body': 'Codex called the installed Engram MCP server.',
                                    'memory_id': 'memory-e2e-1',
                                }
                            ]
                        },
                    )

                    return
                if self.path in EXPECTED_HOOK_PATHS:
                    self._write_json(200, {'status': 'ok'})

                    return

                self._write_json(404, {'detail': 'not found'})
            except Exception as error:
                scenario.errors.append(f'{self.path}: {error}')
                self._write_json(500, {'detail': str(error)})

        def _read_json(self) -> dict[str, Any]:
            encoding = self.headers.get('Content-Encoding', 'identity')
            if encoding not in {'', 'identity'}:
                raise E2EError(f'unsupported request encoding {encoding}')
            length = int(self.headers.get('Content-Length', '0'))
            raw = self.rfile.read(length)
            payload = json.loads(raw or b'{}')
            if not isinstance(payload, dict):
                raise E2EError('request body must be a JSON object')

            return payload

        def _require_bearer(self, expected: str) -> None:
            if self.headers.get('Authorization') != f'Bearer {expected}':
                raise E2EError('unexpected authorization header')

        def _write_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()

        def _write_sse(self, events: list[dict[str, Any]]) -> None:
            body = ''.join(
                f'event: {event["type"]}\ndata: {json.dumps(event)}\n\n'
                for event in events
            ).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()

    return Handler


def write_json(path: Path, payload: dict[str, Any], *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    path.chmod(mode)


def setup_demo_repo(repo: Path, env: dict[str, str]) -> None:
    repo.mkdir(parents=True)
    (repo / 'README.md').write_text('# Codex plugin E2E\n', encoding='utf-8')
    run(['git', 'init', '-b', 'master'], cwd=repo, env=env)
    run(['git', 'config', 'user.name', 'Engram E2E'], cwd=repo, env=env)
    run(['git', 'config', 'user.email', 'e2e@engram.test'], cwd=repo, env=env)
    run(['git', 'add', 'README.md'], cwd=repo, env=env)
    run(['git', 'commit', '-m', 'fixture'], cwd=repo, env=env)
    run(['git', 'remote', 'add', 'origin', REPOSITORY_URL], cwd=repo, env=env)


def write_runtime_config(
    *,
    code_home: Path,
    engram_home: Path,
    server_url: str,
) -> None:
    code_home.mkdir(parents=True)
    (code_home / 'config.toml').write_text(
        '\n'.join(
            (
                f'model = "{MODEL_NAME}"',
                'model_provider = "engram_e2e"',
                'approval_policy = "never"',
                'sandbox_mode = "read-only"',
                'check_for_update_on_startup = false',
                'hide_agent_reasoning = true',
                '',
                '[features]',
                'hooks = true',
                'plugins = true',
                'enable_request_compression = false',
                '',
                '[model_providers.engram_e2e]',
                'name = "Engram E2E mock"',
                f'base_url = "{server_url}/v1"',
                'env_key = "OPENAI_API_KEY"',
                'wire_api = "responses"',
                '',
            )
        ),
        encoding='utf-8',
    )
    write_json(
        engram_home / 'config.json',
        {
            'version': 1,
            'server_url': server_url,
            'project_id': None,
            'team_id': None,
            'agent_runtimes': ['codex'],
            'agent_version': CODEX_VERSION,
        },
    )
    write_json(
        engram_home / 'credentials.json',
        {'version': 1, 'api_key': ENGRAM_KEY},
        mode=0o600,
    )


def plugin_records(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    records = payload.get(key)
    if not isinstance(records, list):
        return []

    return [record for record in records if isinstance(record, dict)]


def assert_plugin_record(payload: dict[str, Any], key: str) -> None:
    records = plugin_records(payload, key)
    if not any(
        record.get('name') == 'engram'
        and record.get('marketplaceName') == 'engram-marketplace'
        for record in records
    ):
        raise E2EError(f'Engram missing from Codex plugin {key}: {payload}')


def locate_installed_plugin(code_home: Path) -> Path:
    for manifest_path in sorted((code_home / 'plugins/cache').rglob('plugin.json')):
        if manifest_path.parent.name != '.codex-plugin':
            continue
        payload = json.loads(manifest_path.read_text(encoding='utf-8'))
        if payload.get('name') == 'engram':
            plugin_root = manifest_path.parent.parent.resolve()
            if not plugin_root.is_relative_to(code_home.resolve()):
                raise E2EError(f'installed plugin escaped CODEX_HOME: {plugin_root}')

            return plugin_root

    raise E2EError(f'could not locate installed Engram under {code_home}')


def inspect_installed_bundle(plugin_root: Path) -> None:
    required = (
        '.codex-plugin/plugin.json',
        '.mcp.json',
        'hooks/hook.py',
        'hooks/hooks.json',
        'hooks/mcp.py',
        'skills/how-it-works/SKILL.md',
        'skills/learn-codebase/SKILL.md',
        'skills/mem-search/SKILL.md',
    )
    missing = [relative for relative in required if not (plugin_root / relative).is_file()]
    if missing:
        raise E2EError(f'installed plugin is incomplete: {missing}')
    manifest = json.loads((plugin_root / '.codex-plugin/plugin.json').read_text(encoding='utf-8'))
    if manifest.get('hooks') is not None:
        raise E2EError('installed plugin must use default hooks/hooks.json discovery')


def mcp_tool_names(plugin_root: Path, env: dict[str, str]) -> set[str]:
    manifest = json.loads((plugin_root / '.mcp.json').read_text(encoding='utf-8'))
    entry = manifest['mcpServers']['engram']
    requests = '\n'.join(
        (
            json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'}),
            json.dumps({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}),
        )
    ) + '\n'
    completed = run(
        [entry['command'], *entry.get('args', [])],
        cwd=(plugin_root / entry.get('cwd', '.')).resolve(),
        env=env,
        input_text=requests,
    )
    responses = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
    if len(responses) != 2 or responses[0].get('result', {}).get('protocolVersion') != '2024-11-05':
        raise E2EError(f'installed MCP initialize failed: {responses}')
    tools = responses[1].get('result', {}).get('tools')
    if not isinstance(tools, list):
        raise E2EError(f'installed MCP tools/list failed: {responses[1]}')

    return {tool.get('name') for tool in tools if isinstance(tool, dict)}


def assert_real_codex_run(
    *,
    scenario: Scenario,
    completed: subprocess.CompletedProcess[str],
) -> None:
    if 'CODEX_E2E_OK' not in completed.stdout:
        raise E2EError(f'Codex did not complete the mock turn:\n{completed.stdout}\n{completed.stderr}')
    if scenario.errors:
        raise E2EError(f'mock server errors: {scenario.errors}')
    if len(scenario.model_requests) != 2:
        raise E2EError(f'expected two Responses API calls, got {len(scenario.model_requests)}')

    first_request = json.dumps(scenario.model_requests[0], sort_keys=True)
    for marker in (SESSION_CONTEXT_MARKER, PROMPT_CONTEXT_MARKER):
        if marker not in first_request:
            raise E2EError(f'{marker} was not injected into the real Codex model request')

    paths = {path for path, _payload in scenario.engram_requests}
    missing_hooks = EXPECTED_HOOK_PATHS - paths
    if missing_hooks:
        raise E2EError(f'Codex did not fire installed hooks: {sorted(missing_hooks)}; saw {sorted(paths)}')

    search_payloads = [
        payload for path, payload in scenario.engram_requests if path == '/v1/search/'
    ]
    if not search_payloads:
        raise E2EError(
            'Codex discovered the bundled MCP tool but its process did not route the user '
            'workspace to Engram; plugin cwd/project fallback is unsafe\n'
            f'Codex stdout:\n{redact(completed.stdout[-8000:])}\n'
            f'Codex stderr:\n{redact(completed.stderr[-8000:])}'
        )
    if search_payloads[0].get('repository_url') != REPOSITORY_URL:
        raise E2EError(f'MCP routed the wrong repository: {search_payloads[0]}')

    second_request = json.dumps(scenario.model_requests[1], sort_keys=True)
    if MCP_RESULT_MARKER not in second_request:
        raise E2EError('real Codex did not return the Engram MCP result to the model')

    post_tool_payloads = [
        payload for path, payload in scenario.engram_requests if path == '/v1/hooks/post-tool-use'
    ]
    if not post_tool_payloads or not any(
        str(payload.get('payload', {}).get('tool_name', '')).endswith('engram_search')
        for payload in post_tool_payloads
    ):
        raise E2EError(f'PostToolUse did not capture the real MCP call: {post_tool_payloads}')


def run_inside_container() -> int:
    expected_version = os.environ.get('CODEX_E2E_EXPECTED_VERSION', CODEX_VERSION)
    actual_version = run(['codex', '--version'], cwd=ROOT).stdout.strip()
    if actual_version != f'codex-cli {expected_version}':
        raise E2EError(f'expected codex-cli {expected_version}, got {actual_version}')
    if run(['pnpm', 'config', 'get', 'minimumReleaseAge'], cwd=ROOT).stdout.strip() != '10080':
        raise E2EError('pnpm minimumReleaseAge must remain 10080')
    if run(['pnpm', 'config', 'get', 'minimumReleaseAgeStrict'], cwd=ROOT).stdout.strip() != 'true':
        raise E2EError('pnpm minimumReleaseAgeStrict must remain true')

    progress(f'runtime ready: {actual_version}, pnpm {PNPM_VERSION}, seven-day age gate enabled')
    test_env = dict(os.environ)
    test_env['PYTHONPATH'] = str(ROOT / 'packages/codex-plugin')
    progress('running package contracts and bundle drift check inside container')
    run(
        [
            'python3',
            '-m',
            'unittest',
            'discover',
            '-s',
            'packages/codex-plugin',
            '-p',
            '*_tests.py',
            '-v',
        ],
        cwd=ROOT,
        env=test_env,
    )
    run(['python3', 'scripts/sync_plugin_bundle.py', '--check'], cwd=ROOT, env=test_env)

    with tempfile.TemporaryDirectory(prefix='engram-codex-plugin-e2e-') as workdir_text:
        workdir = Path(workdir_text)
        home = workdir / 'home'
        code_home = workdir / 'codex-home'
        engram_home = home / '.engram'
        demo_repo = workdir / 'demo-repo'
        sentinel = workdir / 'external-sentinel-profile'
        home.mkdir()
        sentinel.mkdir()
        write_json(sentinel / '.codex/config.json', {'sentinel': 'must-not-change'})
        write_json(sentinel / '.codex/auth.json', {'token': 'external-sentinel'})
        sentinel_before = tree_snapshot(sentinel)

        env = dict(os.environ)
        env.update(
            {
                'CODEX_HOME': str(code_home),
                'ENGRAM_HOME': str(engram_home),
                'HOME': str(home),
                'NO_COLOR': '1',
                'OPENAI_API_KEY': MODEL_KEY,
                'XDG_CACHE_HOME': str(home / '.cache'),
                'XDG_CONFIG_HOME': str(home / '.config'),
            }
        )
        setup_demo_repo(demo_repo, env)

        scenario = Scenario()
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(scenario))
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            server_url = f'http://127.0.0.1:{server.server_address[1]}'
            write_runtime_config(code_home=code_home, engram_home=engram_home, server_url=server_url)

            progress('adding and listing the isolated repo marketplace')
            run(['codex', 'plugin', 'marketplace', 'add', str(ROOT), '--json'], cwd=ROOT, env=env)
            repeat_native_command(
                ['codex', 'plugin', 'marketplace', 'add', str(ROOT), '--json'],
                cwd=ROOT,
                env=env,
            )
            available = run_json(
                ['codex', 'plugin', 'list', '--marketplace', 'engram-marketplace', '--available', '--json'],
                cwd=ROOT,
                env=env,
            )
            assert_plugin_record(available, 'available')

            progress('installing Engram with the real Codex plugin command')
            run(['codex', 'plugin', 'add', PLUGIN_ID, '--json'], cwd=ROOT, env=env)
            repeat_native_command(
                ['codex', 'plugin', 'add', PLUGIN_ID, '--json'],
                cwd=ROOT,
                env=env,
            )
            installed = run_json(['codex', 'plugin', 'list', '--json'], cwd=ROOT, env=env)
            assert_plugin_record(installed, 'installed')
            plugin_root = locate_installed_plugin(code_home)
            inspect_installed_bundle(plugin_root)

            policy = (
                '\n[plugins."engram@engram-marketplace".mcp_servers.engram]\n'
                'enabled = true\n'
                'default_tools_approval_mode = "approve"\n'
            )
            with (code_home / 'config.toml').open('a', encoding='utf-8') as handle:
                handle.write(policy)

            progress('initializing the installed MCP bridge and checking all six tools')
            if mcp_tool_names(plugin_root, env) != EXPECTED_MCP_TOOLS:
                raise E2EError('installed MCP bridge did not expose the exact six Engram tools')
            mcp_completed = run(['codex', 'mcp', 'list', '--json'], cwd=demo_repo, env=env)
            mcp_listing = json.loads(mcp_completed.stdout)
            if not isinstance(mcp_listing, list) or not any(
                isinstance(server_entry, dict) and server_entry.get('name') == 'engram'
                for server_entry in mcp_listing
            ):
                raise E2EError(f'Codex did not list the installed Engram MCP server: {mcp_listing}')

            progress('running a real Codex thread against the local Responses API mock')
            codex_run = run(
                [
                    'codex',
                    'exec',
                    '--ephemeral',
                    '--dangerously-bypass-hook-trust',
                    '--json',
                    '--color',
                    'never',
                    'Use the Engram search tool once, then finish.',
                ],
                cwd=demo_repo,
                env=env,
                timeout=120,
            )
            assert_real_codex_run(
                scenario=scenario,
                completed=codex_run,
            )

            progress('removing Engram through Codex')
            run(['codex', 'plugin', 'remove', PLUGIN_ID, '--json'], cwd=ROOT, env=env)
            after_remove = run_json(['codex', 'plugin', 'list', '--json'], cwd=ROOT, env=env)
            if any(record.get('name') == 'engram' for record in plugin_records(after_remove, 'installed')):
                raise E2EError(f'Engram remained installed after removal: {after_remove}')
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=5)

        if tree_snapshot(sentinel) != sentinel_before:
            raise E2EError('external sentinel profile changed')

    progress('real Codex plugin E2E passed')

    return 0


def run_host() -> int:
    if shutil.which('docker') is None:
        raise E2EError('docker is required; the Codex plugin E2E only runs in a container')

    progress(
        f'building isolated image with codex {CODEX_VERSION}, pnpm {PNPM_VERSION}, '
        'minimumReleaseAge=10080 strict=true'
    )
    build = run(
        [
            'docker',
            'build',
            '--file',
            str(DOCKERFILE),
            '--build-arg',
            f'CODEX_VERSION={CODEX_VERSION}',
            '--build-arg',
            f'PNPM_VERSION={PNPM_VERSION}',
            '--tag',
            IMAGE,
            str(DOCKERFILE.parent),
        ],
        cwd=ROOT,
        timeout=900,
    )
    if build.stdout:
        print(build.stdout, end='')
    if build.stderr:
        print(build.stderr, end='', file=sys.stderr)

    progress('running with a read-only repo mount and no external network')
    completed = run(
        [
            'docker',
            'run',
            '--rm',
            '--network',
            'none',
            '--read-only',
            '--cap-drop',
            'ALL',
            '--security-opt',
            'no-new-privileges',
            '--tmpfs',
            '/tmp:rw,nosuid,nodev,size=512m',
            '--volume',
            f'{ROOT}:/workspace:ro',
            '--env',
            f'CODEX_E2E_EXPECTED_VERSION={CODEX_VERSION}',
            IMAGE,
        ],
        cwd=ROOT,
        timeout=600,
    )
    print(completed.stdout, end='')
    if completed.stderr:
        print(completed.stderr, end='', file=sys.stderr)

    return 0


def main() -> int:
    if len(sys.argv) == 2 and sys.argv[1] == '--inside-container':
        return run_inside_container()
    if len(sys.argv) != 1:
        raise E2EError('usage: python3 scripts/e2e_codex_plugin.py')

    return run_host()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except E2EError as error:
        print(f'[codex-plugin-e2e] FAILED: {error}', file=sys.stderr)
        raise SystemExit(1) from error
