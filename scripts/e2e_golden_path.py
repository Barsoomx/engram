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
MEMORY_TITLE = 'Hook ingest replay handling is stable'
MEMORY_BODY = 'The hook ingest path reuses accepted replay rows and keeps request ids idempotent.'
MEMORY_FILE = 'apps/backend/engram/hooks/services.py'
WORKER_MEMORY_NOT_READY_ERROR = 'worker-created retrieval document not ready'
WORKER_MEMORY_TIMEOUT_SECONDS = 120.0
WORKER_MEMORY_POLL_INTERVAL_SECONDS = 2.0
CONTEXT_AUDIT_NOT_READY_ERROR = 'context audit evidence not ready'


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
    try:
        ensure_compose_env()
        progress('Starting Compose services')
        run(['docker', 'compose', 'up', '-d', '--build', '--wait'], cwd=COMPOSE_DIR, secret=api_key)
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
                    '--json',
                ],
                cwd=COMPOSE_DIR,
                secret=api_key,
            )
            project_id = required_string(bootstrap, 'project_id')
            team_id = required_string(bootstrap, 'team_id')
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
                input_text=json.dumps(post_tool_use_payload()),
                secret=api_key,
            )
            assert_equal(post_tool_use.get('status'), 'accepted', 'post-tool-use status')
            assert_secret_absent('post-tool-use response', json.dumps(post_tool_use), api_key)

            progress('Waiting for worker-created retrieval document')
            worker_memory = wait_for_worker_memory(project_id, api_key)
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
                input_text=json.dumps(session_start_payload()),
                secret=api_key,
            )
            assert_context_response(context)
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

        progress('Compose golden path passed')

        return 0
    finally:
        progress('Stopping Compose services')
        run(['docker', 'compose', 'down', '-v'], cwd=COMPOSE_DIR, secret=api_key, check=False)


def ensure_compose_env() -> None:
    env_file = COMPOSE_DIR / '.env'
    if env_file.exists():
        return

    shutil.copyfile(COMPOSE_DIR / '.env.example', env_file)
    progress('Created deploy/compose/.env from .env.example')


def post_tool_use_payload() -> dict[str, object]:
    return {
        'session_id': 'e2e-session-observation',
        'event_id': 'e2e-hook-event-1',
        'idempotency_key': 'e2e-hook-idempotency-1',
        'request_id': 'e2e-hook-request-1',
        'payload': {
            'tool_name': 'bash',
            'tool_input': {'command': 'pytest engram/hooks/hook_ingest_tests.py -v'},
            'tool_response': {'exit_code': 0},
        },
        'observation': {
            'type': 'tool_use',
            'title': MEMORY_TITLE,
            'body': MEMORY_BODY,
            'files_read': [MEMORY_FILE],
            'files_modified': [],
        },
        'repository_root': '/workspace/engram',
        'branch': 'master',
        'cwd': '/workspace/engram',
    }


def session_start_payload() -> dict[str, object]:
    return {
        'session_id': 'e2e-session-context',
        'request_id': 'e2e-context-request-1',
        'query': 'hook ingest replay handling stable',
        'file_paths': [MEMORY_FILE],
        'symbols': ['IngestHookEvent'],
        'limit': 5,
        'token_budget': 2000,
        'repository_root': '/workspace/engram',
        'branch': 'master',
        'cwd': '/workspace/engram',
    }


def pythonpath_env() -> dict[str, str]:
    env = dict(os.environ)
    env['PYTHONPATH'] = str(ROOT / 'packages/cli')

    return env


def wait_for_worker_memory(project_id: str, secret: str) -> dict[str, object]:
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
                    worker_memory_query(project_id),
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
        'Timed out waiting for worker-created approved memory and retrieval document. '
        f'Last error: {last_error}'
    )


def worker_memory_query(project_id: str) -> str:
    return f"""
import json
from engram.core.models import Memory, MemoryStatus, MemoryVersion, RetrievalDocument

memory = (
    Memory.objects.filter(
        project_id={json.dumps(project_id)},
        title={json.dumps(MEMORY_TITLE)},
        status=MemoryStatus.APPROVED,
    )
    .order_by('-created_at')
    .first()
)
if memory is None:
    raise SystemExit({json.dumps(WORKER_MEMORY_NOT_READY_ERROR)})

version = MemoryVersion.objects.filter(memory=memory, version=memory.current_version).first()
if version is None:
    raise SystemExit({json.dumps(WORKER_MEMORY_NOT_READY_ERROR)})

document = RetrievalDocument.objects.filter(
    project_id={json.dumps(project_id)},
    memory=memory,
    memory_version=version,
).first()
if document is None:
    raise SystemExit({json.dumps(WORKER_MEMORY_NOT_READY_ERROR)})

print(json.dumps({{
    'memory_id': str(memory.id),
    'memory_version_id': str(version.id),
    'retrieval_document_id': str(document.id),
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


def assert_context_response(response: dict[str, object]) -> None:
    assert_equal(response.get('status'), 'created', 'context status')
    assert_equal(response.get('purpose'), 'session_start', 'context purpose')
    items = response.get('items')
    if not isinstance(items, list) or not items:
        raise E2EError('Context response did not include memory items')
    first_item = items[0]
    if not isinstance(first_item, dict):
        raise E2EError('Context response item is not an object')
    assert_equal(first_item.get('citation'), 'M1', 'first context citation')
    assert_equal(first_item.get('title'), MEMORY_TITLE, 'memory title')
    assert_equal(first_item.get('body'), MEMORY_BODY, 'memory body')
    rendered_context = required_string(response, 'rendered_context')
    if MEMORY_TITLE not in rendered_context or MEMORY_BODY not in rendered_context:
        raise E2EError('Rendered context does not contain approved memory')
    hook_output = response.get('hook_specific_output')
    if not isinstance(hook_output, dict):
        raise E2EError('Context response missing hook-specific output')
    assert_equal(hook_output.get('hookEventName'), 'SessionStart', 'hook event name')


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
