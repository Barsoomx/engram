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
PROVIDER_SECRET = 'sk-engram_golden_path_local_provider_secret_1234567890'
GENERATION_TITLE_PREFIX = 'E2E memory'
DIGEST_TITLE_PREFIX = 'E2E digest'
SESSION_TITLE_PREFIX = 'E2E session memory'
PLANTED_MARKER = 'sk-' + 'e2eplanted1234567890'
EMBEDDING_DIMENSION = 1536
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
                {'name': 'Bash', 'input': {'command': f'echo engram-e2e-ok {PLANTED_MARKER}'}},
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


def locate_installed_plugin_root(home: Path) -> Path:
    for plugin_manifest in (home / '.claude').rglob('plugin.json'):
        try:
            manifest = json.loads(plugin_manifest.read_text(encoding='utf-8'))
        except json.JSONDecodeError:
            continue
        if manifest.get('name') != 'engram':
            continue
        if plugin_manifest.parent.name == '.claude-plugin':
            return plugin_manifest.parent.parent

        return plugin_manifest.parent

    raise E2EError(f'Could not locate installed engram plugin root under {home / ".claude"}')


def assert_plugin_mcp_bridge(plugin_root: Path, engram_home: Path, repo: Path) -> None:
    mcp_manifest = plugin_root / '.mcp.json'
    mcp_shim = plugin_root / 'hooks' / 'mcp.py'
    if not mcp_manifest.exists() or not mcp_shim.exists():
        raise E2EError(f'plugin mcp files missing under {plugin_root}')

    manifest = json.loads(mcp_manifest.read_text(encoding='utf-8'))
    entry = manifest.get('mcpServers', {}).get('engram', {})
    if entry.get('command') != 'python3' or 'env' in entry:
        raise E2EError(f'unexpected mcp entry in {mcp_manifest}: {entry}')

    env = dict(os.environ)
    env['ENGRAM_HOME'] = str(engram_home)
    requests = [
        {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
        {
            'jsonrpc': '2.0',
            'id': 3,
            'method': 'tools/call',
            'params': {'name': 'engram_search', 'arguments': {'query': 'e2e'}},
        },
    ]
    completed = run(
        ['python3', str(mcp_shim)],
        cwd=repo,
        env=env,
        secret='',
        input_text='\n'.join(json.dumps(request) for request in requests) + '\n',
        timeout=120,
    )

    lines = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
    if len(lines) != 3:
        raise E2EError(f'expected 3 JSON-RPC responses from plugin mcp bridge, got {len(lines)}:\n{completed.stdout}')

    if lines[0].get('result', {}).get('protocolVersion') != '2024-11-05':
        raise E2EError(f'plugin mcp initialize failed: {lines[0]}')

    tools = lines[1].get('result', {}).get('tools') or []
    if len(tools) != 6:
        raise E2EError(f'plugin mcp tools/list did not return 6 tools: {tools}')

    content = lines[2].get('result', {}).get('content') or []
    search_text = content[0].get('text', '') if content else ''
    if not search_text or search_text.startswith('Engram call failed'):
        raise E2EError(f'plugin mcp search failed: {lines[2]}')

    if 'not configured' in search_text or 'requires a connected project' in search_text:
        raise E2EError(f'plugin mcp search did not reach the backend: {search_text}')


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
from engram.core.models import (
    AgentSession,
    AuditEvent,
    Memory,
    MemoryCandidate,
    MemoryCandidateSource,
    Observation,
    Project,
    RawEventEnvelope,
    RetrievalDocument,
    WorkflowRun,
)
from engram.model_policy.models import ProviderCallRecord
from django_celery_outbox.models import CeleryOutbox, CeleryOutboxDeadLetter

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

candidates = list(MemoryCandidate.objects.filter(project=project))
if not candidates:
    raise SystemExit({json.dumps(DB_NOT_READY_ERROR)} + ': no memory candidates yet')

memories = list(Memory.objects.filter(project=project).exclude(kind='digest'))
if not memories:
    raise SystemExit({json.dumps(DB_NOT_READY_ERROR)} + ': no promoted memories yet')

documents = list(RetrievalDocument.objects.filter(project=project))
if not documents:
    raise SystemExit({json.dumps(DB_NOT_READY_ERROR)} + ': no retrieval documents yet')
missing_embeddings = [str(d.id) for d in documents if d.embedding_pgvector is None]
if missing_embeddings:
    raise SystemExit({json.dumps(DB_NOT_READY_ERROR)} + f': documents without pgvector {{missing_embeddings}}')

outbox_pending = CeleryOutbox.objects.count()
if outbox_pending:
    raise SystemExit({json.dumps(DB_NOT_READY_ERROR)} + f': outbox still has {{outbox_pending}} rows')

sessions_ended = AgentSession.objects.filter(project=project, status='ended', ended_at__isnull=False).count()
if not sessions_ended:
    raise SystemExit({json.dumps(DB_NOT_READY_ERROR)} + ': no ended agent session yet')

workflow_statuses = sorted(
    WorkflowRun.objects.filter(project=project, run_type='session_distillation').values_list('status', flat=True),
)
if 'succeeded' not in workflow_statuses:
    raise SystemExit({json.dumps(DB_NOT_READY_ERROR)} + f': session distillation workflow {{workflow_statuses}}')

session_candidate_titles = sorted(
    MemoryCandidateSource.objects.filter(candidate__project=project)
    .values_list('candidate__title', flat=True)
    .distinct()
)
if not session_candidate_titles:
    raise SystemExit({json.dumps(DB_NOT_READY_ERROR)} + ': no session-distilled candidates yet')

planted = {json.dumps(PLANTED_MARKER)}
planted_leaks = []
for observation in observations:
    if planted in observation.body:
        planted_leaks.append(f'observation:{{observation.observation_type}}')
for raw_event in raw_events:
    if planted in json.dumps(raw_event.payload):
        planted_leaks.append(f'raw_event:{{raw_event.event_type}}')
for candidate in candidates:
    if planted in candidate.title or planted in candidate.body or planted in json.dumps(candidate.evidence):
        planted_leaks.append('memory_candidate')
for memory in memories:
    if planted in memory.title or planted in memory.body:
        planted_leaks.append('memory')
redaction_marker_seen = any('[REDACTED]' in o.body for o in observations)

audit_events = AuditEvent.objects.filter(organization=project.organization).count()
provider_calls = ProviderCallRecord.objects.filter(project=project)

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
    'memory_candidates': len(candidates),
    'candidate_titles': sorted({{c.title for c in candidates}})[:10],
    'memory_count': len(memories),
    'memory_titles': sorted({{m.title for m in memories}})[:10],
    'document_count': len(documents),
    'embedding_dims': sorted({{len(list(d.embedding_pgvector)) for d in documents}}),
    'embedding_references': sorted({{d.embedding_reference.split(':')[0] for d in documents}}),
    'provider_call_tasks': sorted(set(provider_calls.values_list('task_type', flat=True))),
    'provider_call_transports': sorted(
        {{str(record.metadata.get('transport') or 'none') for record in provider_calls}},
    ),
    'audit_events': audit_events,
    'sessions_ended': sessions_ended,
    'session_workflow_statuses': workflow_statuses,
    'session_candidate_titles': session_candidate_titles[:5],
    'planted_leaks': sorted(set(planted_leaks)),
    'redaction_marker_seen': redaction_marker_seen,
    'dead_letters': CeleryOutboxDeadLetter.objects.count(),
}}))
"""


def digest_query() -> str:
    return f"""
import json
from engram.core.models import Memory, Project
from django_celery_outbox.models import CeleryOutbox

project = Project.objects.filter(repository_url={json.dumps(CANONICAL_REPO_URL)}).first()
digest = Memory.objects.filter(project=project, kind='digest').order_by('-created_at').first()
if digest is None:
    raise SystemExit({json.dumps(DB_NOT_READY_ERROR)} + ': digest memory not created yet')

if CeleryOutbox.objects.count():
    raise SystemExit({json.dumps(DB_NOT_READY_ERROR)} + ': outbox still busy after digest')

print(json.dumps({{'digest_title': digest.title, 'digest_body_head': digest.body[:120]}}))
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
    for required_type in ('user_prompt_submit', 'post_tool_use', 'session_start', 'session_end'):
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
    candidate_titles = [str(title) for title in state.get('candidate_titles') or []]
    if not any(title.startswith(GENERATION_TITLE_PREFIX) for title in candidate_titles):
        failures.append(f'no candidate generated from the mock provider: {candidate_titles}')
    memory_titles = [str(title) for title in state.get('memory_titles') or []]
    if not any(title.startswith(GENERATION_TITLE_PREFIX) for title in memory_titles):
        failures.append(f'no mock-generated memory approved through typed review: {memory_titles}')
    if state.get('embedding_dims') != [EMBEDDING_DIMENSION]:
        failures.append(f'unexpected embedding dims: {state.get("embedding_dims")}')
    if state.get('embedding_references') != ['provider']:
        failures.append(f'unexpected embedding references: {state.get("embedding_references")}')
    provider_tasks = state.get('provider_call_tasks') or []
    for required_task in ('generation', 'embedding'):
        if required_task not in provider_tasks:
            failures.append(f'missing provider call for {required_task}: {provider_tasks}')
    transports = state.get('provider_call_transports') or []
    if 'http' not in transports:
        failures.append(f'no real HTTP provider transport recorded: {transports}')
    session_titles = [str(title) for title in state.get('session_candidate_titles') or []]
    if not any(title.startswith(SESSION_TITLE_PREFIX) for title in session_titles):
        failures.append(f'session-distilled candidates not generated by mock: {session_titles}')
    if state.get('planted_leaks'):
        failures.append(f'planted secret leaked into stored data: {state["planted_leaks"]}')
    if not state.get('redaction_marker_seen'):
        failures.append('planted secret was not redacted into [REDACTED] marker in any observation')
    if state.get('dead_letters'):
        failures.append(f'outbox dead letters present: {state["dead_letters"]}')
    if failures:
        raise E2EError('Backend contract failures:\n- ' + '\n- '.join(failures))

    return state


def approve_cp3_candidate(*, secret: str) -> dict[str, object]:
    return wait_for_db_state(
        f"""
import json
from engram.console.services import approve_memory_candidate
from engram.core.models import Identity, MemoryCandidate, Organization, Project, WorkflowRun, WorkflowWork

project = Project.objects.filter(repository_url={json.dumps(CANONICAL_REPO_URL)}).first()
if project is None:
    raise SystemExit({json.dumps(DB_NOT_READY_ERROR)} + ': project not auto-created')
candidate = (
    MemoryCandidate.objects.filter(
        project=project,
        status='proposed',
        decision_work_contract_version=1,
        title__startswith={json.dumps(GENERATION_TITLE_PREFIX)},
        sources__observation__raw_event__event_type='session_end',
    ).distinct().order_by('-created_at').first()
)
if candidate is None:
    raise SystemExit({json.dumps(DB_NOT_READY_ERROR)} + ': generated CP3 candidate not ready')
work = WorkflowWork.objects.filter(
    project=project,
    subject_id=candidate.id,
    work_type='candidate_decision',
    execution_state='blocked',
).first()
if work is None:
    raise SystemExit({json.dumps(DB_NOT_READY_ERROR)} + ': candidate-decision work missing')
run = WorkflowRun.objects.filter(
    work=work,
    status='failed',
    failure_code='candidate_decision_capability_unavailable',
).first()
if run is None:
    raise SystemExit({json.dumps(DB_NOT_READY_ERROR)} + ': candidate decision is not configuration-blocked')
organization = Organization.objects.get(id=project.organization_id)
actor = Identity.objects.get(organization=organization, external_id='golden-path-operator', active=True)
memory = approve_memory_candidate(organization, actor, candidate, 'CP3 Claude plugin typed approval')
print(json.dumps({'candidate_id': str(candidate.id), 'memory_id': str(memory.id)}))
""",
        secret=secret,
    )


def backdate_memories_into_daily_window() -> str:
    return f"""
import json
from datetime import timedelta
from django.utils import timezone
from engram.core.models import Memory, Project
from engram.memory.digest_scheduler import daily_bucket

project = Project.objects.filter(repository_url={json.dumps(CANONICAL_REPO_URL)}).first()
bucket = daily_bucket(as_of=timezone.now())
backdated = Memory.objects.filter(project=project).exclude(kind='digest').update(
    updated_at=bucket.window_end - timedelta(hours=1),
)
print(json.dumps({{'backdated': backdated}}))
"""


def run_daily_digest_and_verify(*, secret: str) -> dict[str, object]:
    backdated = compose_shell_json(backdate_memories_into_daily_window(), secret=secret)
    if int(backdated.get('backdated') or 0) <= 0:
        raise E2EError(f'no memories backdated into the daily digest window: {backdated}')

    run(
        ['docker', 'compose', 'exec', '-T', 'api', 'python', 'manage.py', 'engram_run_daily_digest'],
        cwd=COMPOSE_DIR,
        secret=secret,
    )
    digest = wait_for_db_state(digest_query(), secret=secret)
    title = str(digest.get('digest_title') or '')
    if DIGEST_TITLE_PREFIX not in title:
        raise E2EError(f'digest title not generated by mock provider: {title!r}')

    return digest


def cli_hook_env(home: Path) -> dict[str, str]:
    env = dict(os.environ)
    env['HOME'] = str(home)
    env['PYTHONPATH'] = str(ROOT / 'packages/cli')

    return env


def verify_replay_idempotency(home: Path, repo: Path, *, secret: str) -> dict[str, object]:
    hook_input = {
        'session_id': 'e2e-replay-session',
        'event_id': 'e2e-replay-1',
        'idempotency_key': 'e2e-replay-1',
        'request_id': 'e2e-replay-1',
        'tool_name': 'Bash',
        'tool_input': {'command': 'echo replay-check'},
        'repository_root': str(repo),
        'cwd': str(repo),
    }
    env = cli_hook_env(home)
    command = [sys.executable, '-m', 'engram_cli', 'hook', 'post-tool-use', '--agent', 'claude-code']
    first = run_json(command, cwd=repo, env=env, secret='', input_text=json.dumps(hook_input))
    second = run_json(command, cwd=repo, env=env, secret='', input_text=json.dumps(hook_input))
    if first.get('duplicate') is not False:
        raise E2EError(f'first delivery unexpectedly marked duplicate: {first}')
    if second.get('duplicate') is not True:
        raise E2EError(f'replayed delivery not marked duplicate: {second}')
    envelopes = compose_shell_json(
        """
import json
from engram.core.models import RawEventEnvelope

print(json.dumps({'count': RawEventEnvelope.objects.filter(client_event_id='e2e-replay-1').count()}))
""",
        secret=secret,
    )
    if envelopes.get('count') != 1:
        raise E2EError(f'replay produced {envelopes.get("count")} raw event envelopes, expected 1')

    return {'replay_duplicate': True, 'envelopes': 1}


def set_auto_approve_threshold(value: str, *, secret: str) -> None:
    compose_shell_json(
        f"""
import json
from decimal import Decimal
from engram.core.models import Organization, OrganizationSettings

organization = Organization.objects.get(slug='engram-e2e')
OrganizationSettings.objects.filter(organization=organization).update(
    distillation_auto_approve_threshold=Decimal('{value}'),
)
print(json.dumps({{'threshold': '{value}'}}))
""",
        secret=secret,
    )


def verify_held_for_review(home: Path, repo: Path, *, secret: str) -> dict[str, object]:
    set_auto_approve_threshold('1.000', secret=secret)
    try:
        hook_input = {
            'session_id': 'e2e-held-session',
            'event_id': 'e2e-held-1',
            'idempotency_key': 'e2e-held-1',
            'request_id': 'e2e-held-1',
            'tool_name': 'Bash',
            'tool_input': {'command': 'echo held-for-review-check'},
            'repository_root': str(repo),
            'cwd': str(repo),
        }
        run_json(
            [sys.executable, '-m', 'engram_cli', 'hook', 'post-tool-use', '--agent', 'claude-code'],
            cwd=repo,
            env=cli_hook_env(home),
            secret='',
            input_text=json.dumps(hook_input),
        )
        held = wait_for_db_state(
            f"""
import json
from engram.core.models import AuditEvent, MemoryCandidate

candidate = (
    MemoryCandidate.objects.filter(source_observation__raw_event__client_event_id='e2e-held-1')
    .order_by('-created_at')
    .first()
)
if candidate is None:
    raise SystemExit({json.dumps(DB_NOT_READY_ERROR)} + ': held candidate not created yet')

if candidate.status != 'proposed':
    raise SystemExit(f'held candidate has status {{candidate.status}}, expected proposed')

audit = AuditEvent.objects.filter(
    event_type='MemoryCandidateHeldForReview',
    target_id=str(candidate.id),
).first()
if audit is None:
    raise SystemExit({json.dumps(DB_NOT_READY_ERROR)} + ': held-for-review audit event missing')

print(json.dumps({{'held_candidate': str(candidate.id), 'promoted': candidate.promoted_memory_id is not None}}))
""",
            secret=secret,
        )
        if held.get('promoted'):
            raise E2EError('held candidate was auto-promoted despite threshold 1.000')
    finally:
        set_auto_approve_threshold('0.000', secret=secret)

    return {'held_candidate': held.get('held_candidate')}


def verify_search_api(home: Path, repo: Path) -> dict[str, object]:
    result = run_json(
        [sys.executable, '-m', 'engram_cli', 'search', '--query', GENERATION_TITLE_PREFIX, '--json'],
        cwd=repo,
        env=cli_hook_env(home),
        secret='',
    )
    items = result.get('items')
    if not isinstance(items, list) or not items:
        raise E2EError(f'search returned no items: {result}')
    titles = [str(item.get('title') or '') for item in items]
    if not any(title.startswith(GENERATION_TITLE_PREFIX) for title in titles):
        raise E2EError(f'search did not return the promoted mock memory: {titles}')

    return {'search_items': len(items)}


def context_inclusion_reasons(home: Path, repo: Path, query: str, session_suffix: str) -> list[str]:
    hook_input = {
        'session_id': f'e2e-leg-{session_suffix}',
        'repository_root': str(repo),
        'cwd': str(repo),
        'query': query,
    }
    result = run_json(
        [sys.executable, '-m', 'engram_cli', 'hook', 'session-start', '--agent', 'claude-code'],
        cwd=repo,
        env=cli_hook_env(home),
        secret='',
        input_text=json.dumps(hook_input),
    )
    items = result.get('items') or []

    return [str(item.get('inclusion_reason') or '') for item in items if isinstance(item, dict)]


def set_lexical_recall(enabled: bool, *, secret: str) -> None:
    compose_shell_json(
        f"""
import json
from engram.core.models import Organization, OrganizationSettings

organization = Organization.objects.get(slug='engram-e2e')
OrganizationSettings.objects.filter(organization=organization).update(lexical_recall_enabled={enabled})
print(json.dumps({{'lexical_recall_enabled': {enabled}}}))
""",
        secret=secret,
    )


def verify_retrieval_legs(home: Path, repo: Path, *, secret: str) -> dict[str, object]:
    fulltext_reasons = context_inclusion_reasons(
        home,
        repo,
        'durable engineering provider zebra',
        'fulltext',
    )
    if not any(reason.startswith('full-text match:') for reason in fulltext_reasons):
        raise E2EError(f'full-text retrieval leg never matched: {fulltext_reasons}')

    set_lexical_recall(True, secret=secret)
    try:
        lexical_reasons = context_inclusion_reasons(
            home,
            repo,
            'durrable enginering memmory',
            'lexical',
        )
        if not any(reason.startswith('lexical match:') for reason in lexical_reasons):
            raise E2EError(f'lexical retrieval leg never matched: {lexical_reasons}')
    finally:
        set_lexical_recall(False, secret=secret)

    return {'fulltext_reasons': fulltext_reasons[:3], 'lexical_reasons': lexical_reasons[:3]}


def http_post_json(url: str, payload: dict[str, object], api_key: str) -> tuple[int, dict[str, object]]:
    import urllib.error
    import urllib.request

    request = urllib.request.Request(  # noqa: S310 - local e2e server
        url,
        data=json.dumps(payload).encode(),
        headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        body = error.read().decode()
        try:
            return error.code, json.loads(body)
        except json.JSONDecodeError:
            return error.code, {'raw': body[:500]}


def http_get_json(url: str, api_key: str) -> tuple[int, dict[str, object]]:
    import urllib.error
    import urllib.request

    request = urllib.request.Request(  # noqa: S310 - local e2e server
        url,
        headers={'Authorization': f'Bearer {api_key}'},
        method='GET',
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        body = error.read().decode()
        try:
            return error.code, json.loads(body)
        except json.JSONDecodeError:
            return error.code, {'raw': body[:500]}


def verify_feedback_loop(home: Path, repo: Path, project_id: str, agent_key: str, *, secret: str) -> dict[str, object]:
    picked = compose_shell_json(
        f"""
import json
from engram.core.models import Memory, Project

project = Project.objects.filter(repository_url={json.dumps(CANONICAL_REPO_URL)}).first()
memory = (
    Memory.objects.filter(project=project, title__startswith={json.dumps(GENERATION_TITLE_PREFIX)}, stale=False)
    .order_by('created_at')
    .first()
)
print(json.dumps({{'memory_id': str(memory.id), 'title': memory.title}}))
""",
        secret=secret,
    )
    memory_id = str(picked.get('memory_id'))

    status, inspection = http_get_json(
        f'{SERVER_URL}/v1/inspection/memories/{memory_id}?project_id={project_id}', agent_key,
    )
    if status != 200 or inspection.get('authorized_for_injection') is not True:
        raise E2EError(f'inspection before feedback expected authorized_for_injection=true: {status} {inspection}')

    status, feedback = http_post_json(
        f'{SERVER_URL}/v1/memories/{memory_id}/feedback',
        {
            'project_id': project_id,
            'action': 'stale',
            'reason': 'e2e feedback loop check',
            'request_id': 'e2e-feedback-1',
        },
        agent_key,
    )
    if status != 200:
        raise E2EError(f'memory feedback failed: {status} {feedback}')

    propagated = compose_shell_json(
        f"""
import json
from engram.core.models import Memory, RetrievalDocument

memory = Memory.objects.get(id={json.dumps(memory_id)})
documents = list(RetrievalDocument.objects.filter(memory=memory).values_list('stale', flat=True))
print(json.dumps({{'memory_stale': memory.stale, 'document_stale': documents}}))
""",
        secret=secret,
    )
    if not propagated.get('memory_stale') or not all(propagated.get('document_stale') or [False]):
        raise E2EError(f'stale feedback did not propagate: {propagated}')

    status, inspection_after = http_get_json(
        f'{SERVER_URL}/v1/inspection/memories/{memory_id}?project_id={project_id}', agent_key,
    )
    if inspection_after.get('authorized_for_injection') is not False:
        raise E2EError(f'inspection after feedback should deny injection: {inspection_after}')

    stale_title = str(picked.get('title'))
    hook_input = {
        'session_id': 'e2e-feedback-context',
        'repository_root': str(repo),
        'cwd': str(repo),
        'query': stale_title,
    }
    context = run_json(
        [sys.executable, '-m', 'engram_cli', 'hook', 'session-start', '--agent', 'claude-code'],
        cwd=repo,
        env=cli_hook_env(home),
        secret='',
        input_text=json.dumps(hook_input),
    )
    returned_ids = [str(item.get('memory_id')) for item in context.get('items') or [] if isinstance(item, dict)]
    if memory_id in returned_ids:
        raise E2EError(f'stale memory still served in context: {memory_id} in {returned_ids}')

    return {'stale_memory': memory_id, 'context_items_after': len(returned_ids)}


def verify_observations_api(project_id: str, agent_key: str) -> dict[str, object]:
    status, body = http_get_json(f'{SERVER_URL}/v1/observations/?project_id={project_id}', agent_key)
    if status != 200:
        raise E2EError(f'observations list failed: {status} {body}')
    items = body.get('items')
    if not isinstance(items, list) or not items:
        raise E2EError(f'observations list returned no items: {body}')

    return {'observations': len(items)}


def verify_rbac_negatives(project_id: str, golden_key: str) -> dict[str, object]:
    status, body = http_post_json(
        f'{SERVER_URL}/v1/hooks/dry-run',
        {'agent_runtime': 'claude_code', 'request_id': 'e2e-rbac-random'},
        'egk_random_invalid_key_000000000000000000',
    )
    if status != 401:
        raise E2EError(f'random key expected 401, got {status}: {body}')

    status, body = http_post_json(
        f'{SERVER_URL}/v1/hooks/dry-run',
        {'agent_runtime': 'claude_code', 'request_id': 'e2e-rbac-cross', 'project_id': project_id},
        golden_key,
    )
    if status != 403:
        raise E2EError(f'project-bound key against foreign project expected 403, got {status}: {body}')

    return {'random_key': 401, 'cross_project': 403}


def verify_weekly_digest(*, secret: str) -> dict[str, object]:
    result = compose_shell_json(
        f"""
import json
from engram.core.models import Memory, Project, WorkflowRun
from engram.memory.services import run_weekly_digest_with_tracking

project = Project.objects.filter(repository_url={json.dumps(CANONICAL_REPO_URL)}).first()
result = run_weekly_digest_with_tracking(project.organization_id, project.id, request_id='e2e-weekly-1')
run = WorkflowRun.objects.filter(project=project, run_type='weekly_digest').order_by('-created_at').first()
weekly = (
    Memory.objects.filter(project=project, kind='digest', metadata__digest_kind='weekly_structured')
    .order_by('-created_at')
    .first()
)
print(json.dumps({{
    'workflow_status': run.status if run else None,
    'weekly_memory': weekly.title if weekly else None,
}}))
""",
        secret=secret,
    )
    if result.get('workflow_status') != 'succeeded':
        raise E2EError(f'weekly digest workflow did not succeed: {result}')

    return result


def verify_policy_precedence(bootstrap: dict[str, object], *, secret: str) -> dict[str, object]:
    expected_policy_id = str(bootstrap.get('organization_generation_policy_id') or '')
    state = compose_shell_json(
        f"""
import json
from engram.core.models import Project
from engram.model_policy.models import ProviderCallRecord

project = Project.objects.filter(repository_url={json.dumps(CANONICAL_REPO_URL)}).first()
policy_ids = sorted(
    set(
        str(policy_id)
        for policy_id in ProviderCallRecord.objects.filter(project=project, task_type='generation').values_list(
            'policy_id', flat=True,
        )
    ),
)
print(json.dumps({{'generation_policy_ids': policy_ids}}))
""",
        secret=secret,
    )
    policy_ids = state.get('generation_policy_ids') or []
    if policy_ids != [expected_policy_id]:
        raise E2EError(
            f'auto-created project should resolve the ORGANIZATION generation policy {expected_policy_id}, got {policy_ids}',
        )

    return {'generation_policy': 'organization-scope'}


def verify_session_start_context(home: Path, repo: Path) -> dict[str, object]:
    env = cli_hook_env(home)
    hook_input = {
        'session_id': 'e2e-context-check',
        'repository_root': str(repo),
        'cwd': str(repo),
        'query': GENERATION_TITLE_PREFIX,
    }
    result = run_json(
        [sys.executable, '-m', 'engram_cli', 'hook', 'session-start', '--agent', 'claude-code'],
        cwd=repo,
        env=env,
        secret='',
        input_text=json.dumps(hook_input),
    )
    rendered = str(result.get('rendered_context') or '')
    items = result.get('items')
    if GENERATION_TITLE_PREFIX not in rendered:
        raise E2EError(f'session-start context does not include the promoted mock memory:\n{rendered[:1500]}')
    if not isinstance(items, list) or not items:
        raise E2EError('session-start context returned no items')

    return {'rendered_context_chars': len(rendered), 'context_items': len(items)}


def verify_mock_traffic(log_path: Path, agent_key: str) -> dict[str, object]:
    if not log_path.exists():
        raise E2EError('Mock server saw no traffic at all')

    records = [json.loads(line) for line in log_path.read_text(encoding='utf-8').splitlines() if line.strip()]
    message_calls = [r for r in records if '/v1/messages' in str(r.get('path')) and 'count_tokens' not in str(r.get('path'))]
    embedding_calls = [r for r in records if str(r.get('path')).endswith('/embeddings')]
    completion_calls = [r for r in records if str(r.get('path')).endswith('/chat/completions')]
    if len(message_calls) < 2:
        raise E2EError(f'Expected at least 2 /v1/messages calls, saw {len(message_calls)}')
    if not embedding_calls:
        raise E2EError('Backend never called the mock /embeddings endpoint')
    if not completion_calls:
        raise E2EError('Backend never called the mock /chat/completions endpoint')

    wrong_anthropic = [r['path'] for r in message_calls if FAKE_ANTHROPIC_KEY not in str(r.get('api_key'))]
    if wrong_anthropic:
        raise E2EError(f'Anthropic mock received requests without the fake key: {wrong_anthropic}')
    wrong_provider = [
        r['path'] for r in (*embedding_calls, *completion_calls) if PROVIDER_SECRET not in str(r.get('api_key'))
    ]
    if wrong_provider:
        raise E2EError(f'Provider mock received requests without the decrypted secret: {wrong_provider}')

    raw_log = log_path.read_text(encoding='utf-8')
    if agent_key in raw_log:
        raise E2EError('Engram agent API key leaked into LLM traffic')

    return {
        'message_calls': len(message_calls),
        'embedding_calls': len(embedding_calls),
        'completion_calls': len(completion_calls),
        'total_requests': len(records),
    }


def main() -> int:
    api_key = f'egk_e2e_{secrets.token_urlsafe(32)}'
    agent_key = f'egk_agent_{secrets.token_urlsafe(32)}'
    failed = True
    keep_up = os.environ.get('E2E_KEEP_UP') == '1'
    skip_build = os.environ.get('E2E_SKIP_BUILD') == '1'
    provider_mode = os.environ.get('E2E_PROVIDER_MODE', 'real')
    os.environ['ENGRAM_PROVIDER_MODE'] = provider_mode
    os.environ.setdefault('ENGRAM_REALTIME_MIN_CONTENT_CHARS', '1')
    os.environ.setdefault('COMPOSE_PROJECT_NAME', 'engram-plugin-e2e')
    mock_process: subprocess.Popen[bytes] | None = None
    try:
        ensure_claude_cli()
        ensure_compose_env()

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
            progress(f'Starting mock LLM server on port {mock_port} (anthropic + openai)')
            mock_process = subprocess.Popen(
                [
                    sys.executable,
                    str(ROOT / 'scripts/mock_anthropic_server.py'),
                    '--port',
                    str(mock_port),
                    '--api-key',
                    FAKE_ANTHROPIC_KEY,
                    '--provider-key',
                    PROVIDER_SECRET,
                    '--bind',
                    '0.0.0.0',
                    '--scenario',
                    str(scenario_path),
                    '--log',
                    str(traffic_log),
                ],
            )
            wait_for_http(f'http://127.0.0.1:{mock_port}/health')

            progress(f'Starting Compose services (ENGRAM_PROVIDER_MODE={provider_mode})')
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
                    '--provider-base-url',
                    f'http://host.docker.internal:{mock_port}/v1',
                    '--json',
                ],
                cwd=COMPOSE_DIR,
                secret=agent_key,
            )
            if 'agent_api_key_id' not in bootstrap:
                raise E2EError('Bootstrap did not create the agent key')

            progress('Connecting engram CLI in isolated home')
            connect_cli(home, agent_key)
            install_plugin(home, mock_port)

            progress('Verifying plugin ships a working MCP bridge')
            plugin_root = locate_installed_plugin_root(home)
            assert_plugin_mcp_bridge(plugin_root, home / '.engram', repo)
            progress('plugin MCP bridge passed')

            claude_result = run_claude_prompt(home, repo, mock_port)
            progress(f'claude exited with {claude_result.returncode}')
            if claude_result.returncode != 0:
                raise E2EError(
                    f'claude run failed:\nstdout:\n{claude_result.stdout[-4000:]}\nstderr:\n{claude_result.stderr[-4000:]}',
                )

            progress('Waiting for CP3 candidate-decision block and approving typed memory')
            approved = approve_cp3_candidate(secret=agent_key)
            progress(f'Typed approval OK: {json.dumps(approved)}')

            progress('Verifying backend contract state (distillation, promotion, embeddings)')
            state = verify_backend_state(secret=agent_key)
            progress(f'Backend state OK: {json.dumps(state)}')

            progress('Running daily digest against the mock provider')
            digest = run_daily_digest_and_verify(secret=agent_key)
            progress(f'Digest OK: {json.dumps(digest)}')

            progress('Verifying session-start context retrieval')
            context = verify_session_start_context(home, repo)
            progress(f'Context OK: {json.dumps(context)}')

            progress('Verifying search API over repo-url routing')
            search = verify_search_api(home, repo)
            progress(f'Search OK: {json.dumps(search)}')

            progress('Verifying semantic and lexical retrieval legs')
            legs = verify_retrieval_legs(home, repo, secret=agent_key)
            progress(f'Retrieval legs OK: {json.dumps(legs)}')

            progress('Verifying RBAC negatives')
            rbac = verify_rbac_negatives(str(state.get('project_id')), api_key)
            progress(f'RBAC OK: {json.dumps(rbac)}')

            progress('Verifying weekly digest plumbing')
            weekly = verify_weekly_digest(secret=agent_key)
            progress(f'Weekly digest OK: {json.dumps(weekly)}')

            progress('Verifying model policy precedence for auto-created project')
            precedence = verify_policy_precedence(bootstrap, secret=agent_key)
            progress(f'Policy precedence OK: {json.dumps(precedence)}')

            progress('Verifying observations API with agent key')
            observations = verify_observations_api(str(state.get('project_id')), agent_key)
            progress(f'Observations OK: {json.dumps(observations)}')

            progress('Verifying memory feedback loop (stale → out of retrieval)')
            feedback = verify_feedback_loop(home, repo, str(state.get('project_id')), agent_key, secret=agent_key)
            progress(f'Feedback OK: {json.dumps(feedback)}')

            progress('Verifying replay idempotency')
            replay = verify_replay_idempotency(home, repo, secret=agent_key)
            progress(f'Replay OK: {json.dumps(replay)}')

            progress('Verifying held-for-review path')
            held = verify_held_for_review(home, repo, secret=agent_key)
            progress(f'Held-for-review OK: {json.dumps(held)}')

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
            logs = run(
                ['docker', 'compose', 'logs', '--no-color', '--tail=120'],
                cwd=COMPOSE_DIR,
                secret=api_key,
                check=False,
            )
            print(logs.stdout[-20000:], flush=True)
            print(logs.stderr[-2000:], flush=True)
        if not keep_up:
            progress('Stopping Compose services')
            run(['docker', 'compose', 'down', '-v'], cwd=COMPOSE_DIR, secret=api_key, check=False)


if __name__ == '__main__':
    raise SystemExit(main())
