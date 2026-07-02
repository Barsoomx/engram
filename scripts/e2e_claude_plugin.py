from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_DIR = ROOT / 'deploy/compose'
SERVER_URL = 'http://127.0.0.1:8000'
CANONICAL_REPO_URL = 'git@github.com:engram-e2e/demo-repo.git'
FAKE_ANTHROPIC_KEY = 'sk-ant-e2e-mock-0123456789'
CLAUDE_TIMEOUT_SECONDS = 300
QUEUE_TIMEOUT_SECONDS = 120.0
QUEUE_POLL_INTERVAL_SECONDS = 2.0
DB_NOT_READY_ERROR = 'e2e database state not ready'


class E2EError(Exception):
    pass


def progress(message: str) -> None:
    print(f'[claude-plugin-e2e] {message}', flush=True)


def run(
    args: Sequence[str],
    *,
    cwd: Path,
    secret: str,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    check: bool = True,
    timeout: int = 600,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        args,
        cwd=cwd,
        env=env,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )
    if check and completed.returncode != 0:
        command = ' '.join(args).replace(secret, '[REDACTED]')
        stdout = completed.stdout.replace(secret, '[REDACTED]')
        stderr = completed.stderr.replace(secret, '[REDACTED]')

        raise E2EError(f'Command failed ({completed.returncode}): {command}\nstdout:\n{stdout}\nstderr:\n{stderr}')

    return completed


def run_json(args: Sequence[str], *, cwd: Path, secret: str, **kwargs: object) -> dict[str, object]:
    completed = run(args, cwd=cwd, secret=secret, **kwargs)  # type: ignore[arg-type]
    try:
        payload = json.loads(completed.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as error:
        raise E2EError(f'Expected JSON output, got:\n{completed.stdout[-2000:]}') from error
    if not isinstance(payload, dict):
        raise E2EError('Expected JSON object output')

    return payload


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))

        return sock.getsockname()[1]


def wait_for_http(url: str, timeout: float = 30.0) -> None:
    import urllib.request

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2):  # noqa: S310 - local mock
                return
        except OSError:
            time.sleep(0.3)

    raise E2EError(f'Timed out waiting for {url}')


def ensure_compose_env() -> None:
    env_file = COMPOSE_DIR / '.env'
    if not env_file.exists():
        shutil.copyfile(COMPOSE_DIR / '.env.example', env_file)


def ensure_claude_cli() -> str:
    claude = shutil.which('claude')
    if claude:
        return claude

    if os.environ.get('E2E_INSTALL_CLAUDE') == '1':
        progress('Installing Claude Code CLI via npm')
        run(
            ['npm', 'install', '-g', '@anthropic-ai/claude-code'],
            cwd=ROOT,
            secret='',
        )
        claude = shutil.which('claude')
        if claude:
            return claude

    raise E2EError('claude CLI not found; install it or set E2E_INSTALL_CLAUDE=1')


def scenario_turns(repo: Path) -> list[dict[str, object]]:
    return [
        {
            'tool_uses': [
                {'name': 'Read', 'input': {'file_path': str(repo / 'README.md')}},
            ],
        },
        {
            'tool_uses': [
                {
                    'name': 'Write',
                    'input': {
                        'file_path': str(repo / 'e2e_note.md'),
                        'content': 'engram e2e observation note',
                    },
                },
            ],
        },
        {
            'tool_uses': [
                {'name': 'Bash', 'input': {'command': 'echo engram-e2e-ok'}},
            ],
        },
        {'text': 'E2E run complete.'},
    ]


def claude_env(home: Path, mock_port: int) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            'HOME': str(home),
            'ANTHROPIC_API_KEY': FAKE_ANTHROPIC_KEY,
            'ANTHROPIC_BASE_URL': f'http://127.0.0.1:{mock_port}',
            'DISABLE_TELEMETRY': '1',
            'DISABLE_ERROR_REPORTING': '1',
            'DISABLE_AUTOUPDATER': '1',
            'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1',
            'IS_SANDBOX': '1',
        },
    )

    return env


def setup_isolated_home(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / '.claude').mkdir(exist_ok=True)
    (home / '.claude.json').write_text(
        json.dumps(
            {
                'hasCompletedOnboarding': True,
                'bypassPermissionsModeAccepted': True,
            },
        ),
        encoding='utf-8',
    )


def install_plugin(home: Path, mock_port: int) -> None:
    env = claude_env(home, mock_port)
    progress('Adding local marketplace')
    run(['claude', 'plugin', 'marketplace', 'add', str(ROOT)], cwd=ROOT, env=env, secret='')
    progress('Installing engram plugin from this checkout')
    run(['claude', 'plugin', 'install', 'engram@engram-marketplace'], cwd=ROOT, env=env, secret='')
    listing = run(['claude', 'plugin', 'list'], cwd=ROOT, env=env, secret='')
    if 'engram' not in listing.stdout:
        raise E2EError(f'Plugin not visible after install:\n{listing.stdout}\n{listing.stderr}')


def connect_cli(home: Path, agent_key: str) -> None:
    env = dict(os.environ)
    env['HOME'] = str(home)
    env['PYTHONPATH'] = str(ROOT / 'packages/cli')
    run(
        [
            sys.executable,
            '-m',
            'engram_cli',
            'connect',
            '--server',
            SERVER_URL,
            '--api-key',
            agent_key,
            '--agent',
            'claude-code',
            '--agent-version',
            'e2e',
        ],
        cwd=ROOT,
        env=env,
        secret=agent_key,
    )


def setup_demo_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    (repo / 'README.md').write_text('# Demo repo\n\nEngram plugin E2E fixture.\n', encoding='utf-8')
    git = ['git', '-c', 'user.email=e2e@engram.test', '-c', 'user.name=Engram E2E']
    run([*git, 'init', '-b', 'master'], cwd=repo, secret='')
    run([*git, 'add', '.'], cwd=repo, secret='')
    run([*git, 'commit', '-m', 'init'], cwd=repo, secret='')
    run([*git, 'remote', 'add', 'origin', CANONICAL_REPO_URL], cwd=repo, secret='')


def run_claude_prompt(home: Path, repo: Path, mock_port: int) -> subprocess.CompletedProcess[str]:
    env = claude_env(home, mock_port)
    progress('Running claude -p against mock server')

    return run(
        [
            'claude',
            '-p',
            'Read the README, write a short note file, then run the echo command.',
            '--dangerously-skip-permissions',
            '--max-turns',
            '8',
        ],
        cwd=repo,
        env=env,
        secret='',
        timeout=CLAUDE_TIMEOUT_SECONDS,
    )


def compose_shell_json(query: str, *, secret: str) -> dict[str, object]:
    return run_json(
        ['docker', 'compose', 'exec', '-T', 'api', 'python', 'manage.py', 'shell', '-c', query],
        cwd=COMPOSE_DIR,
        secret=secret,
    )


def wait_for_db_state(query: str, *, secret: str, timeout: float = QUEUE_TIMEOUT_SECONDS) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    last_error = DB_NOT_READY_ERROR
    while time.monotonic() < deadline:
        try:
            return compose_shell_json(query, secret=secret)
        except E2EError as error:
            last_error = str(error)
            if DB_NOT_READY_ERROR not in last_error:
                raise

            time.sleep(QUEUE_POLL_INTERVAL_SECONDS)

    raise E2EError(f'Timed out waiting for backend state. Last error: {last_error}')


def verification_query() -> str:
    return f"""
import json
from engram.core.models import AuditEvent, MemoryCandidate, Observation, Project, RawEventEnvelope
from django_celery_outbox.models import CeleryOutbox

project = Project.objects.filter(repository_url={json.dumps(CANONICAL_REPO_URL)}).first()
if project is None:
    raise SystemExit({json.dumps(DB_NOT_READY_ERROR)} + ': project not auto-created')

observations = list(Observation.objects.filter(project=project).order_by('created_at'))
if len(observations) < 3:
    raise SystemExit({json.dumps(DB_NOT_READY_ERROR)} + f': only {{len(observations)}} observations')

empty_bodies = [o.observation_type for o in observations if not o.body.strip()]
missing_observed_at = [o.observation_type for o in observations if o.observed_at is None]
types = sorted({{o.observation_type for o in observations}})
files_read = sorted({{path for o in observations for path in o.files_read}})
files_modified = sorted({{path for o in observations for path in o.files_modified}})

raw_events = list(RawEventEnvelope.objects.filter(project=project))
empty_payloads = [r.event_type for r in raw_events if not r.payload]

candidates = MemoryCandidate.objects.filter(project=project).count()
if candidates < 1:
    raise SystemExit({json.dumps(DB_NOT_READY_ERROR)} + ': no memory candidates yet')

outbox_pending = CeleryOutbox.objects.count()
if outbox_pending:
    raise SystemExit({json.dumps(DB_NOT_READY_ERROR)} + f': outbox still has {{outbox_pending}} rows')

audit_events = AuditEvent.objects.filter(organization=project.organization).count()

print(json.dumps({{
    'project_id': str(project.id),
    'observation_count': len(observations),
    'observation_types': types,
    'empty_bodies': empty_bodies,
    'missing_observed_at': missing_observed_at,
    'files_read': files_read,
    'files_modified': files_modified,
    'raw_event_count': len(raw_events),
    'empty_payloads': empty_payloads,
    'memory_candidates': candidates,
    'audit_events': audit_events,
}}))
"""


def verify_backend_state(*, secret: str) -> dict[str, object]:
    state = wait_for_db_state(verification_query(), secret=secret)
    failures = []
    if state.get('empty_bodies'):
        failures.append(f'observations with empty body: {state["empty_bodies"]}')
    if state.get('missing_observed_at'):
        failures.append(f'observations without observed_at: {state["missing_observed_at"]}')
    if state.get('empty_payloads'):
        failures.append(f'raw events with empty payload: {state["empty_payloads"]}')
    types = state.get('observation_types') or []
    for required_type in ('user_prompt_submit', 'post_tool_use'):
        if required_type not in types:
            failures.append(f'missing observation type {required_type}; got {types}')
    files_read = state.get('files_read') or []
    if not any(str(path).endswith('README.md') for path in files_read):
        failures.append(f'files_read missing README.md: {files_read}')
    files_modified = state.get('files_modified') or []
    if not any(str(path).endswith('e2e_note.md') for path in files_modified):
        failures.append(f'files_modified missing e2e_note.md: {files_modified}')
    if not state.get('audit_events'):
        failures.append('no audit events recorded')
    if failures:
        raise E2EError('Backend contract failures:\n- ' + '\n- '.join(failures))

    return state


def verify_mock_traffic(log_path: Path, agent_key: str) -> dict[str, object]:
    if not log_path.exists():
        raise E2EError('Mock server saw no traffic at all')

    records = [json.loads(line) for line in log_path.read_text(encoding='utf-8').splitlines() if line.strip()]
    message_calls = [r for r in records if '/v1/messages' in str(r.get('path')) and 'count_tokens' not in str(r.get('path'))]
    if len(message_calls) < 2:
        raise E2EError(f'Expected at least 2 /v1/messages calls, saw {len(message_calls)}')

    wrong_keys = [r['path'] for r in records if FAKE_ANTHROPIC_KEY not in str(r.get('api_key'))]
    if wrong_keys:
        raise E2EError(f'Mock received requests without the fake key: {wrong_keys}')

    raw_log = log_path.read_text(encoding='utf-8')
    if agent_key in raw_log:
        raise E2EError('Engram agent API key leaked into LLM traffic')

    return {'message_calls': len(message_calls), 'total_requests': len(records)}


def main() -> int:
    api_key = f'egk_e2e_{secrets.token_urlsafe(32)}'
    agent_key = f'egk_agent_{secrets.token_urlsafe(32)}'
    failed = True
    keep_up = os.environ.get('E2E_KEEP_UP') == '1'
    skip_build = os.environ.get('E2E_SKIP_BUILD') == '1'
    mock_process: subprocess.Popen[bytes] | None = None
    try:
        ensure_claude_cli()
        ensure_compose_env()
        progress('Starting Compose services')
        up_command = ['docker', 'compose', 'up', '-d', '--wait']
        if not skip_build:
            up_command.insert(3, '--build')
        run(['docker', 'compose', 'down', '-v'], cwd=COMPOSE_DIR, secret=api_key, check=False)
        run(up_command, cwd=COMPOSE_DIR, secret=api_key)

        progress('Bootstrapping golden path objects + org-wide agent key')
        bootstrap = run_json(
            [
                'docker',
                'compose',
                'exec',
                '-T',
                'api',
                'python',
                'manage.py',
                'engram_bootstrap_golden_path',
                '--api-key',
                api_key,
                '--agent-key',
                agent_key,
                '--json',
            ],
            cwd=COMPOSE_DIR,
            secret=agent_key,
        )
        if 'agent_api_key_id' not in bootstrap:
            raise E2EError('Bootstrap did not create the agent key')

        with tempfile.TemporaryDirectory(prefix='engram-claude-e2e-') as workdir_str:
            workdir = Path(workdir_str)
            home = workdir / 'home'
            repo = workdir / 'demo-repo'
            setup_isolated_home(home)
            setup_demo_repo(repo)

            mock_port = free_port()
            scenario_path = workdir / 'scenario.json'
            traffic_log = workdir / 'requests.jsonl'
            scenario_path.write_text(json.dumps(scenario_turns(repo)), encoding='utf-8')
            progress(f'Starting mock Anthropic server on port {mock_port}')
            mock_process = subprocess.Popen(
                [
                    sys.executable,
                    str(ROOT / 'scripts/mock_anthropic_server.py'),
                    '--port',
                    str(mock_port),
                    '--api-key',
                    FAKE_ANTHROPIC_KEY,
                    '--scenario',
                    str(scenario_path),
                    '--log',
                    str(traffic_log),
                ],
            )
            wait_for_http(f'http://127.0.0.1:{mock_port}/health')

            progress('Connecting engram CLI in isolated home')
            connect_cli(home, agent_key)
            install_plugin(home, mock_port)

            claude_result = run_claude_prompt(home, repo, mock_port)
            progress(f'claude exited with {claude_result.returncode}')
            if claude_result.returncode != 0:
                raise E2EError(
                    f'claude run failed:\nstdout:\n{claude_result.stdout[-4000:]}\nstderr:\n{claude_result.stderr[-4000:]}',
                )

            progress('Verifying backend contract state')
            state = verify_backend_state(secret=agent_key)
            progress(f'Backend state OK: {json.dumps(state)}')

            progress('Verifying sniffed mock traffic')
            traffic = verify_mock_traffic(traffic_log, agent_key)
            progress(f'Mock traffic OK: {json.dumps(traffic)}')

        progress('Claude plugin full E2E passed')
        failed = False

        return 0
    finally:
        if mock_process is not None:
            mock_process.terminate()
        if failed:
            progress('E2E failed — dumping compose logs')
            run(
                ['docker', 'compose', 'logs', '--no-color', '--tail=120'],
                cwd=COMPOSE_DIR,
                secret=api_key,
                check=False,
            )
        if not keep_up:
            progress('Stopping Compose services')
            run(['docker', 'compose', 'down', '-v'], cwd=COMPOSE_DIR, secret=api_key, check=False)


if __name__ == '__main__':
    raise SystemExit(main())
