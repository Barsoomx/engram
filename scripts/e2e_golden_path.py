from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
COMPOSE_DIR = ROOT / 'deploy/compose'
SERVER_URL = 'http://127.0.0.1:8000'
MEMORY_TITLE_PREFIX = 'Hook ingest replay handling is stable'
MEMORY_BODY_PREFIX = 'The hook ingest path reuses accepted replay rows and keeps request ids idempotent.'
MEMORY_FILE = 'apps/backend/engram/hooks/services.py'
WORKER_MEMORY_NOT_READY_ERROR = 'worker-created retrieval document not ready'
WORKER_MEMORY_TIMEOUT_SECONDS = 120.0
WORKER_MEMORY_POLL_INTERVAL_SECONDS = 2.0
CONTEXT_AUDIT_NOT_READY_ERROR = 'context audit evidence not ready'
SYMBOL_TERM = 'plan_lookup_helper'


@dataclass(frozen=True)
class CommandResult:
    args: Sequence[str]
    returncode: int
    stdout: str
    stderr: str


class E2EError(Exception):
    pass


def main() -> int:
    api_key = f'egk_e2e_{secrets.token_urlsafe(32)}'
    agent_key = f'egk_e2e_agent_{secrets.token_urlsafe(32)}'
    run_id = secrets.token_hex(8)
    failed = True
    try:
        ensure_compose_env()
        progress('Clearing Compose state')
        run(['docker', 'compose', 'down', '-v'], cwd=COMPOSE_DIR, secret=api_key)
        progress('Starting Compose services')
        run(
            ['docker', 'compose', 'up', '-d', '--build', '--wait'],
            cwd=COMPOSE_DIR,
            secret=api_key,
        )
        with tempfile.TemporaryDirectory(prefix='engram-e2e-') as config_dir:
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
                secret=api_key,
            )
            assert_secret_absent('bootstrap response', json.dumps(bootstrap), agent_key)
            project_id = required_string(bootstrap, 'project_id')
            team_id = required_string(bootstrap, 'team_id')
            repository_url = required_string(bootstrap, 'repository_url')
            cli_env = pythonpath_env()

            progress('Connecting host CLI')
            connect = run(
                [
                    sys.executable,
                    '-m',
                    'engram_cli',
                    'connect',
                    '--server',
                    SERVER_URL,
                    '--api-key',
                    api_key,
                    '--project',
                    project_id,
                    '--team',
                    team_id,
                    '--agent',
                    'codex',
                    '--agent-version',
                    'e2e',
                    '--config-dir',
                    config_dir,
                ],
                cwd=ROOT,
                env=cli_env,
                secret=api_key,
            )
            assert_secret_absent('connect stdout', connect.stdout, api_key)
            assert_secret_absent('connect stderr', connect.stderr, api_key)

            progress('Connecting host CLI with agent key')
            mcp_config_dir = os.path.join(config_dir, 'mcp')
            os.makedirs(mcp_config_dir, exist_ok=True)
            agent_connect = run(
                [
                    sys.executable,
                    '-m',
                    'engram_cli',
                    'connect',
                    '--server',
                    SERVER_URL,
                    '--api-key',
                    agent_key,
                    '--project',
                    project_id,
                    '--agent',
                    'codex',
                    '--agent-version',
                    'e2e',
                    '--config-dir',
                    mcp_config_dir,
                ],
                cwd=ROOT,
                env=cli_env,
                secret=agent_key,
            )
            assert_secret_absent('agent connect stdout', agent_connect.stdout, agent_key)
            assert_secret_absent('agent connect stderr', agent_connect.stderr, agent_key)

            progress('Submitting hook observation')
            post_tool_use = run_json(
                [
                    sys.executable,
                    '-m',
                    'engram_cli',
                    'hook',
                    'post-tool-use',
                    '--config-dir',
                    config_dir,
                ],
                cwd=ROOT,
                env=cli_env,
                input_text=json.dumps(post_tool_use_payload(run_id)),
                secret=api_key,
            )
            assert_equal(post_tool_use.get('status'), 'accepted', 'post-tool-use status')
            assert_secret_absent('post-tool-use response', json.dumps(post_tool_use), api_key)

            progress('Ending the same session to trigger CP3 distillation')
            session_end = run_json(
                [sys.executable, '-m', 'engram_cli', 'hook', 'session-end', '--config-dir', config_dir],
                cwd=ROOT,
                env=cli_env,
                input_text=json.dumps(session_end_payload(run_id)),
                secret=api_key,
            )
            assert_equal(session_end.get('status'), 'accepted', 'session-end status')
            approve_cp3_candidate(project_id, run_id, agent_key)

            progress('Waiting for worker-created retrieval document')
            worker_memory = wait_for_worker_memory(project_id, run_id, api_key)
            required_string(worker_memory, 'memory_id')
            required_string(worker_memory, 'memory_version_id')
            retrieval_document_id = required_string(worker_memory, 'retrieval_document_id')

            progress('Requesting future session context')
            context = run_json(
                [
                    sys.executable,
                    '-m',
                    'engram_cli',
                    'hook',
                    'session-start',
                    '--config-dir',
                    config_dir,
                ],
                cwd=ROOT,
                env=cli_env,
                input_text=json.dumps(session_start_payload(run_id)),
                secret=api_key,
            )
            assert_context_response(context, worker_memory)
            context_bundle_id = required_string(context, 'context_bundle_id')
            request_id = required_string(context, 'request_id')
            audit_evidence = assert_context_audit_evidence(
                context_bundle_id=context_bundle_id,
                retrieval_document_id=retrieval_document_id,
                request_id=request_id,
                secret=api_key,
            )
            required_string(audit_evidence, 'context_bundle_item_id')
            required_string(audit_evidence, 'audit_event_id')
            assert_secret_absent('context response', json.dumps(context), api_key)

            progress('Driving MCP stdio bridge')
            drive_mcp_stdio(
                config_dir=mcp_config_dir,
                env=cli_env,
                memory_id=worker_memory['memory_id'],
                run_id=run_id,
                secrets_list=[api_key, agent_key],
            )
            progress('MCP stdio bridge passed')

            progress('Submitting second hook observation for repo-url-mode drive')
            run_id_repo_url = f'{run_id}-repourl'
            post_tool_use_repo_url = run_json(
                [
                    sys.executable,
                    '-m',
                    'engram_cli',
                    'hook',
                    'post-tool-use',
                    '--config-dir',
                    config_dir,
                ],
                cwd=ROOT,
                env=cli_env,
                input_text=json.dumps(post_tool_use_payload(run_id_repo_url)),
                secret=api_key,
            )
            assert_equal(post_tool_use_repo_url.get('status'), 'accepted', 'post-tool-use (repo-url) status')
            assert_secret_absent('post-tool-use (repo-url) response', json.dumps(post_tool_use_repo_url), api_key)

            progress('Ending the same repo-url session to trigger CP3 distillation')
            session_end_repo_url = run_json(
                [sys.executable, '-m', 'engram_cli', 'hook', 'session-end', '--config-dir', config_dir],
                cwd=ROOT,
                env=cli_env,
                input_text=json.dumps(session_end_payload(run_id_repo_url)),
                secret=api_key,
            )
            assert_equal(session_end_repo_url.get('status'), 'accepted', 'session-end (repo-url) status')
            approve_cp3_candidate(project_id, run_id_repo_url, agent_key)

            progress('Waiting for second worker-created retrieval document')
            worker_memory_repo_url = wait_for_worker_memory(project_id, run_id_repo_url, api_key)
            repo_url_memory_id = required_string(worker_memory_repo_url, 'memory_id')

            progress('Preparing repo-url-mode workspace')
            repo_url_config_dir = os.path.join(config_dir, 'mcp-repo-url')
            os.makedirs(repo_url_config_dir, exist_ok=True)
            workspace_dir = Path(os.path.join(config_dir, 'workspace'))
            workspace_dir.mkdir(parents=True, exist_ok=True)
            run(['git', 'init'], cwd=workspace_dir, secret=agent_key)
            run(['git', 'remote', 'add', 'origin', repository_url], cwd=workspace_dir, secret=agent_key)

            progress('Connecting host CLI with agent key (repo-url mode, no project)')
            agent_connect_repo_url = run(
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
                    'codex',
                    '--agent-version',
                    'e2e',
                    '--config-dir',
                    repo_url_config_dir,
                ],
                cwd=ROOT,
                env=cli_env,
                secret=agent_key,
            )
            assert_secret_absent('agent connect (repo-url) stdout', agent_connect_repo_url.stdout, agent_key)
            assert_secret_absent('agent connect (repo-url) stderr', agent_connect_repo_url.stderr, agent_key)

            progress('Driving MCP stdio bridge in repo-url mode')
            drive_mcp_stdio(
                config_dir=repo_url_config_dir,
                env=cli_env,
                memory_id=repo_url_memory_id,
                run_id=run_id_repo_url,
                secrets_list=[api_key, agent_key],
                cwd=workspace_dir,
                feedback_action='refuted',
            )
            progress('MCP repo-url mode passed')

        progress('Compose golden path passed')

        failed = False

        return 0
    finally:
        if failed:
            progress('Golden path failed — dumping compose logs')
            run(
                ['docker', 'compose', 'logs', '--no-color', '--tail=150'],
                cwd=COMPOSE_DIR,
                secret=api_key,
                check=False,
            )
        progress('Stopping Compose services')
        run(
            ['docker', 'compose', 'down', '-v'],
            cwd=COMPOSE_DIR,
            secret=api_key,
            check=False,
        )


def drive_mcp_stdio(
    *,
    config_dir: str,
    env: dict[str, str],
    memory_id: str,
    run_id: str,
    secrets_list: list[str],
    cwd: Path = ROOT,
    feedback_action: str = 'stale',
) -> None:
    requests = [
        {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'},
        {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
        _tool_call(
            3, 'engram_search', {'query': 'Provider-synthesized memory', 'file_paths': [MEMORY_FILE]}
        ),
        _tool_call(4, 'engram_context', {'session_id': f'mcp-{run_id}'}),
        _tool_call(5, 'engram_observations', {'limit': 5}),
        _tool_call(
            6,
            'engram_memory_link',
            {'memory_id': memory_id, 'link_type': 'file', 'target': f'e2e/{run_id}.py'},
        ),
        _tool_call(
            7,
            'engram_memory_version',
            {'memory_id': memory_id, 'body': f'mcp e2e first update {run_id}'},
        ),
        _tool_call(
            8,
            'engram_memory_version',
            {
                'memory_id': memory_id,
                'body': f'mcp e2e second update {run_id} calls `{SYMBOL_TERM}()` during planning.',
            },
        ),
        _tool_call(9, 'engram_search', {'query': '', 'symbols': [SYMBOL_TERM]}),
        _tool_call(
            10,
            'engram_memory_feedback',
            {'memory_id': memory_id, 'action': feedback_action, 'reason': f'mcp e2e {run_id}'},
        ),
    ]
    process = subprocess.Popen(
        [sys.executable, '-m', 'engram_cli', 'mcp', 'serve', '--config-dir', config_dir],
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout, stderr = process.communicate(
        '\n'.join(json.dumps(request) for request in requests) + '\n', timeout=180
    )
    if process.returncode != 0:
        raise SystemExit(f'mcp serve exited {process.returncode}: {stderr[-2000:]}')

    responses: dict[int, dict[str, object]] = {}
    for line in stdout.splitlines():
        message = json.loads(line)
        if 'id' in message:
            responses[message['id']] = message
    tool_names = [tool['name'] for tool in responses[2]['result']['tools']]
    assert_equal(len(tool_names), 6, 'mcp tools count')
    texts = {rid: _content_text(responses[rid]) for rid in (3, 4, 5, 6, 7, 8, 9, 10)}
    for rid, text in texts.items():
        for secret in secrets_list:
            assert_secret_absent(f'mcp response {rid}', text, secret)
        if text.startswith('Engram call failed'):
            raise SystemExit(f'mcp tool {rid} failed: {text}')
    if memory_id not in texts[3]:
        raise SystemExit(f'mcp search missed memory_id: {texts[3][:400]}')
    if 'link_id=' not in texts[6] or 'created=True' not in texts[6]:
        raise SystemExit(f'mcp link failed: {texts[6]}')
    first_version = _extract_field(texts[7], 'current_version')
    second_version = _extract_field(texts[8], 'current_version')
    if not first_version or first_version in ('None', second_version):
        raise SystemExit(
            f'mcp version replayed or empty: {texts[7]} / {texts[8]}'
        )
    if f'memory_id={memory_id}' not in texts[9]:
        raise SystemExit(f'mcp symbol search missed memory_id: {texts[9][:400]}')
    if f'{feedback_action}=True' not in texts[10] or 'already_applied=False' not in texts[10]:
        raise SystemExit(f'mcp feedback failed: {texts[10]}')


def _tool_call(request_id: int, name: str, arguments: dict[str, object]) -> dict[str, object]:
    return {
        'jsonrpc': '2.0',
        'id': request_id,
        'method': 'tools/call',
        'params': {'name': name, 'arguments': arguments},
    }


def _content_text(response: dict[str, object]) -> str:
    result = response.get('result')
    if not isinstance(result, dict):
        return f"Engram call failed: {json.dumps(response.get('error'))}"

    content = result.get('content')

    return content[0]['text'] if isinstance(content, list) and content else ''


def _extract_field(text: str, field: str) -> str:
    for token in text.split():
        if token.startswith(f'{field}='):
            return token.split('=', 1)[1]

    return ''


def ensure_compose_env() -> None:
    env_file = COMPOSE_DIR / '.env'
    if env_file.exists():
        return

    shutil.copyfile(COMPOSE_DIR / '.env.example', env_file)
    progress('Created deploy/compose/.env from .env.example')


def memory_title(run_id: str) -> str:
    return f'{MEMORY_TITLE_PREFIX} [{run_id}]'


def memory_body(run_id: str) -> str:
    return f'{MEMORY_BODY_PREFIX} Run id: {run_id}.'


def post_tool_use_payload(run_id: str) -> dict[str, object]:
    return {
        'session_id': f'e2e-session-observation-{run_id}',
        'event_id': f'e2e-hook-event-{run_id}',
        'idempotency_key': f'e2e-hook-idempotency-{run_id}',
        'request_id': f'e2e-hook-request-{run_id}',
        'payload': {
            'tool_name': 'bash',
            'tool_input': {'command': 'pytest engram/hooks/hook_ingest_tests.py -v'},
            'tool_response': {'exit_code': 0},
        },
        'observation': {
            'type': 'tool_use',
            'title': memory_title(run_id),
            'body': memory_body(run_id),
            'files_read': [MEMORY_FILE],
            'files_modified': [],
        },
        'repository_root': '/workspace/engram',
        'branch': 'master',
        'cwd': '/workspace/engram',
    }


def session_start_payload(run_id: str) -> dict[str, object]:
    return {
        'session_id': f'e2e-session-context-{run_id}',
        'request_id': f'e2e-context-request-{run_id}',
        'query': memory_title(run_id),
        'file_paths': [MEMORY_FILE],
        'symbols': ['IngestHookEvent'],
        'limit': 5,
        'token_budget': 2000,
        'repository_root': '/workspace/engram',
        'branch': 'master',
        'cwd': '/workspace/engram',
    }


def session_end_payload(run_id: str) -> dict[str, object]:
    return {
        'session_id': f'e2e-session-observation-{run_id}',
        'event_id': f'e2e-session-end-event-{run_id}',
        'idempotency_key': f'e2e-session-end-idempotency-{run_id}',
        'request_id': f'e2e-session-end-request-{run_id}',
        'observation': {
            'type': 'session_end',
            'title': f'E2E session ended {run_id}',
            'body': 'Golden path CP3 session end',
        },
        'repository_root': '/workspace/engram',
        'branch': 'master',
        'cwd': '/workspace/engram',
    }


def pythonpath_env() -> dict[str, str]:
    env = dict(os.environ)
    env['PYTHONPATH'] = str(ROOT / 'packages/cli')

    return env


def wait_for_worker_memory(project_id: str, run_id: str, secret: str) -> dict[str, object]:
    deadline = time.monotonic() + WORKER_MEMORY_TIMEOUT_SECONDS
    last_error = 'worker-created retrieval document was not observed'
    while time.monotonic() < deadline:
        try:
            return run_json(
                [
                    'docker',
                    'compose',
                    'exec',
                    '-T',
                    'api',
                    'python',
                    'manage.py',
                    'shell',
                    '-c',
                    worker_memory_query(project_id, run_id),
                ],
                cwd=COMPOSE_DIR,
                secret=secret,
            )
        except E2EError as error:
            last_error = str(error)
            if WORKER_MEMORY_NOT_READY_ERROR not in last_error:
                raise

            time.sleep(WORKER_MEMORY_POLL_INTERVAL_SECONDS)

    raise E2EError(
        f'Timed out waiting for worker-created approved memory and retrieval document. Last error: {last_error}'
    )


def approve_cp3_candidate(project_id: str, run_id: str, secret: str) -> dict[str, object]:
    query = f"""
import json
from engram.core.models import Identity, MemoryCandidate, Organization, Project, WorkflowRun, WorkflowWork
project_id = {json.dumps(project_id)}
project = Project.objects.get(id=project_id)
client_event_id = {json.dumps(f'e2e-hook-event-{run_id}')}
request_id = {json.dumps(f'e2e-hook-request-{run_id}')}
candidate = (
    MemoryCandidate.objects.filter(
        project_id=project_id,
        decision_work_contract_version=1,
        sources__observation__raw_event__client_event_id=client_event_id,
        sources__observation__raw_event__request_id=request_id,
        status='proposed',
    ).distinct().order_by('-created_at').first()
)
if candidate is None:
    raise SystemExit({json.dumps(WORKER_MEMORY_NOT_READY_ERROR)} + ': CP3 candidate not ready')
work = WorkflowWork.objects.filter(
    project_id=project_id,
    subject_id=candidate.id,
    work_type='candidate_decision',
    execution_state='blocked',
).first()
if work is None:
    raise SystemExit({json.dumps(WORKER_MEMORY_NOT_READY_ERROR)} + ': candidate decision work missing')
run = WorkflowRun.objects.filter(work=work, status='failed', failure_code='candidate_decision_capability_unavailable').first()
if run is None:
    raise SystemExit({json.dumps(WORKER_MEMORY_NOT_READY_ERROR)} + ': candidate decision is not configuration-blocked')
organization = Organization.objects.get(id=project.organization_id)
actor = Identity.objects.get(organization=organization, external_id='golden-path-operator', active=True)
from engram.console.services import approve_memory_candidate
memory = approve_memory_candidate(organization, actor, candidate, 'CP3 golden-path typed approval')
print(json.dumps({'candidate_id': str(candidate.id), 'memory_id': str(memory.id)}))
"""
    return wait_for_db_state(query, secret=secret)


def worker_memory_query(project_id: str, run_id: str) -> str:
    client_event_id = f'e2e-hook-event-{run_id}'
    request_id = f'e2e-hook-request-{run_id}'

    return f"""
import json
from engram.core.models import Memory, MemoryStatus, MemoryVersion, RetrievalDocument

client_event_id = {json.dumps(client_event_id)}
request_id = {json.dumps(request_id)}

version = (
    MemoryVersion.objects.select_related('memory', 'source_observation__raw_event')
    .filter(
        project_id={json.dumps(project_id)},
        memory__project_id={json.dumps(project_id)},
        memory__status=MemoryStatus.APPROVED,
        source_observation__raw_event__client_event_id=client_event_id,
        source_observation__raw_event__request_id=request_id,
    )
    .order_by('-created_at')
    .first()
)
if version is None:
    raise SystemExit({json.dumps(WORKER_MEMORY_NOT_READY_ERROR)})

memory = version.memory
if version.version != memory.current_version:
    raise SystemExit({json.dumps(WORKER_MEMORY_NOT_READY_ERROR)})

if version.source_observation is None or version.source_observation.raw_event is None:
    raise SystemExit({json.dumps(WORKER_MEMORY_NOT_READY_ERROR)})

raw_event = version.source_observation.raw_event
if raw_event.client_event_id != client_event_id:
    raise SystemExit({json.dumps(WORKER_MEMORY_NOT_READY_ERROR)})
if raw_event.request_id != request_id:
    raise SystemExit({json.dumps(WORKER_MEMORY_NOT_READY_ERROR)})

document = RetrievalDocument.objects.filter(
    project_id={json.dumps(project_id)},
    memory=memory,
    memory_version=version,
).first()
if document is None:
    raise SystemExit({json.dumps(WORKER_MEMORY_NOT_READY_ERROR)})

if str(version.source_observation_id) not in document.source_observation_ids:
    raise SystemExit({json.dumps(WORKER_MEMORY_NOT_READY_ERROR)})

print(json.dumps({{
    'memory_id': str(memory.id),
    'memory_version_id': str(version.id),
    'retrieval_document_id': str(document.id),
    'source_observation_id': str(version.source_observation_id),
    'memory_title': memory.title,
    'memory_body': memory.body,
}}))
"""


def assert_context_audit_evidence(
    *,
    context_bundle_id: str,
    retrieval_document_id: str,
    request_id: str,
    secret: str,
) -> dict[str, object]:
    return run_json(
        [
            'docker',
            'compose',
            'exec',
            '-T',
            'api',
            'python',
            'manage.py',
            'shell',
            '-c',
            context_audit_query(context_bundle_id, retrieval_document_id, request_id),
        ],
        cwd=COMPOSE_DIR,
        secret=secret,
    )


def context_audit_query(context_bundle_id: str, retrieval_document_id: str, request_id: str) -> str:
    return f"""
import json
from engram.core.models import AuditEvent, ContextBundleItem

item = ContextBundleItem.objects.filter(
    bundle_id={json.dumps(context_bundle_id)},
    retrieval_document_id={json.dumps(retrieval_document_id)},
).first()
if item is None:
    raise SystemExit({json.dumps(CONTEXT_AUDIT_NOT_READY_ERROR)})

audit = (
    AuditEvent.objects.filter(
        event_type='MemoryRetrieved',
        target_type='context_bundle',
        target_id={json.dumps(context_bundle_id)},
        request_id={json.dumps(request_id)},
    )
    .order_by('-created_at')
    .first()
)
if audit is None or {json.dumps(retrieval_document_id)} not in audit.metadata.get('retrieval_document_ids', []):
    raise SystemExit({json.dumps(CONTEXT_AUDIT_NOT_READY_ERROR)})

print(json.dumps({{
    'context_bundle_item_id': str(item.id),
    'audit_event_id': str(audit.id),
}}))
"""


def run_json(
    args: Sequence[str],
    *,
    cwd: Path,
    secret: str,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
) -> dict[str, object]:
    result = run(args, cwd=cwd, env=env, input_text=input_text, secret=secret)
    assert_secret_absent('command stdout', result.stdout, secret)
    assert_secret_absent('command stderr', result.stderr, secret)
    try:
        payload = json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as error:
        raise E2EError(f'Expected JSON output from {redact(" ".join(args), secret)}') from error
    if not isinstance(payload, dict):
        raise E2EError('Expected command JSON output to be an object')

    return payload


def run(
    args: Sequence[str],
    *,
    cwd: Path,
    secret: str,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    check: bool = True,
) -> CommandResult:
    completed = subprocess.run(
        args,
        cwd=cwd,
        env=env,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )
    result = CommandResult(
        args=args,
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )
    if check and result.returncode != 0:
        raise E2EError(command_failure_message(result, secret))

    return result


def assert_context_response(response: dict[str, object], worker_memory: dict[str, object]) -> None:
    assert_equal(response.get('status'), 'injected', 'context status')
    assert_equal(response.get('purpose'), 'session_start', 'context purpose')
    items = response.get('items')
    if not isinstance(items, list) or not items:
        raise E2EError('Context response did not include memory items')
    for item in items:
        assert_item_kind_and_confidence(item)
    first_item = items[0]
    if not isinstance(first_item, dict):
        raise E2EError('Context response item is not an object')
    assert_equal(first_item.get('citation'), 'M1', 'first context citation')
    title = required_string(worker_memory, 'memory_title')
    body = required_string(worker_memory, 'memory_body')
    if not title.startswith('Provider-synthesized memory '):
        raise E2EError(f'Expected provider-synthesized memory title, got {title!r}')
    if not body.startswith('Provider-synthesized candidate body '):
        raise E2EError(f'Expected provider-synthesized memory body, got {body!r}')
    assert_equal(first_item.get('title'), title, 'memory title')
    assert_equal(first_item.get('body'), body, 'memory body')
    rendered_context = required_string(response, 'rendered_context')
    if title not in rendered_context or body not in rendered_context:
        raise E2EError('Rendered context does not contain approved memory')
    hook_output = response.get('hook_specific_output')
    if not isinstance(hook_output, dict):
        raise E2EError('Context response missing hook-specific output')
    assert_equal(hook_output.get('hookEventName'), 'SessionStart', 'hook event name')
    assert_warnings_shape(response)


def assert_item_kind_and_confidence(item: object) -> None:
    if not isinstance(item, dict):
        raise E2EError('Context/search item is not an object')
    if not isinstance(item.get('kind'), str):
        raise E2EError(f'Context/search item missing string kind: {item!r}')
    confidence = item.get('confidence')
    if confidence is not None and not isinstance(confidence, str):
        raise E2EError(f'Context/search item confidence must be string or null: {item!r}')


def assert_warnings_shape(response: dict[str, object]) -> None:
    warnings = response.get('warnings')
    if not isinstance(warnings, list):
        raise E2EError(f'Response warnings must be a list, got {warnings!r}')
    for warning in warnings:
        if not isinstance(warning, dict):
            raise E2EError(f'Warning entry is not an object: {warning!r}')
        if set(warning.keys()) != {'code', 'message', 'memory_id'}:
            raise E2EError(f'Warning entry has unexpected keys: {warning!r}')
        if not isinstance(warning['code'], str) or not isinstance(warning['message'], str):
            raise E2EError(f'Warning code/message must be strings: {warning!r}')
        if warning['memory_id'] is not None and not isinstance(warning['memory_id'], str):
            raise E2EError(f'Warning memory_id must be string or null: {warning!r}')


def required_string(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise E2EError(f'Missing string field {key}')

    return value


def assert_equal(actual: object, expected: object, label: str) -> None:
    if actual != expected:
        raise E2EError(f'Unexpected {label}: expected {expected!r}, got {actual!r}')


def assert_secret_absent(label: str, value: str, secret: str) -> None:
    if secret and secret in value:
        raise E2EError(f'{label} leaked the generated API key')


def command_failure_message(result: CommandResult, secret: str) -> str:
    command = redact(' '.join(result.args), secret)
    stdout = redact(result.stdout.strip(), secret)
    stderr = redact(result.stderr.strip(), secret)

    return f'Command failed ({result.returncode}): {command}\nstdout:\n{stdout}\nstderr:\n{stderr}'


def redact(value: str, secret: str) -> str:
    return value.replace(secret, '[REDACTED]')


def progress(message: str) -> None:
    print(f'[engram-e2e] {message}', flush=True)


if __name__ == '__main__':
    raise SystemExit(main())
