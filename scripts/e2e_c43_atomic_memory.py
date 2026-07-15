from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from textwrap import dedent

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = (ROOT / 'deploy/compose/docker-compose.yml').resolve()
PROJECT_PATTERN = re.compile(r'engram-c43-atomic-[0-9a-f]{16}\Z')
GLOBAL_TIMEOUT = 25 * 60.0
STARTUP_TIMEOUT = 12 * 60.0
COMMAND_TIMEOUT = 180.0
POLL_INTERVAL = 1.0
POLL_TIMEOUT = 5 * 60.0
OUTPUT_LIMIT = 4000
MAX_FAKE_PROVIDER_DELAY_MS = 5000


class HarnessError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MemorySnapshot:
    candidate_id: str
    memory_id: str
    version_id: str
    transition_id: str
    document_id: str
    work_id: str
    current_transition_id: str
    transition_exact_document_id: str
    exact_projection_hash: str
    embedding_projection_hash: str
    embedding_reference: str
    embedding_vector_count: int
    embedding_pgvector_is_null: bool
    transition_count: int
    audit_count: int
    document_count: int
    work_count: int
    work_execution_state: str
    active_run_count: int


_SNAPSHOT_FIELDS = frozenset(MemorySnapshot.__dataclass_fields__)
_ID_FIELDS = (
    'candidate_id',
    'memory_id',
    'version_id',
    'transition_id',
    'document_id',
    'work_id',
)
_UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
_HASH_RE = re.compile(r'^[0-9a-f]{64}$')


def _snapshot_error(message: str) -> ValueError:
    return ValueError(f'snapshot {message}')


def _decode_snapshot(raw: str) -> dict[str, object]:
    try:
        decoded = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as error:
        raise _snapshot_error('is not valid JSON') from error
    if not isinstance(decoded, dict) or set(decoded) != _SNAPSHOT_FIELDS:
        raise _snapshot_error('must be an object with the required fields')

    return decoded


def _validate_snapshot_ids(decoded: Mapping[str, object]) -> None:
    for field in _ID_FIELDS + ('current_transition_id', 'transition_exact_document_id'):
        value = decoded[field]
        if not isinstance(value, str) or _UUID_RE.fullmatch(value) is None:
            raise _snapshot_error(f'{field} is malformed')


def _validate_snapshot_projection(decoded: Mapping[str, object]) -> None:
    exact_hash = decoded['exact_projection_hash']
    embedding_hash = decoded['embedding_projection_hash']
    if not isinstance(exact_hash, str) or _HASH_RE.fullmatch(exact_hash) is None:
        raise _snapshot_error('exact_projection_hash is malformed')
    if not isinstance(embedding_hash, str) or (embedding_hash and _HASH_RE.fullmatch(embedding_hash) is None):
        raise _snapshot_error('embedding_projection_hash is malformed')
    if not isinstance(decoded['embedding_reference'], str):
        raise _snapshot_error('embedding_reference is malformed')
    if type(decoded['embedding_vector_count']) is not int or decoded['embedding_vector_count'] < 0:
        raise _snapshot_error('embedding_vector_count is malformed')
    if type(decoded['embedding_pgvector_is_null']) is not bool:
        raise _snapshot_error('embedding_pgvector_is_null is malformed')


def _validate_snapshot_counts(decoded: Mapping[str, object]) -> None:
    for field in (
        'transition_count',
        'audit_count',
        'document_count',
        'work_count',
        'active_run_count',
    ):
        if type(decoded[field]) is not int or decoded[field] < 0:
            raise _snapshot_error(f'{field} is malformed')
    if not isinstance(decoded['work_execution_state'], str) or not decoded['work_execution_state']:
        raise _snapshot_error('work_execution_state is malformed')


def parse_memory_snapshot(raw: str) -> MemorySnapshot:
    decoded = _decode_snapshot(raw)
    _validate_snapshot_ids(decoded)
    _validate_snapshot_projection(decoded)
    _validate_snapshot_counts(decoded)

    return MemorySnapshot(**decoded)


def _check_chain(snapshot: MemorySnapshot) -> None:
    if snapshot.current_transition_id != snapshot.transition_id:
        raise ValueError('transition current pointer does not match transition')
    if snapshot.transition_exact_document_id != snapshot.document_id:
        raise ValueError('document transition pointer does not match document')
    if snapshot.transition_count != 1:
        raise ValueError('transition count must be exactly one')
    if snapshot.audit_count != 1:
        raise ValueError('audit count must be exactly one')
    if snapshot.document_count != 1:
        raise ValueError('document count must be exactly one')
    if snapshot.work_count != 1:
        raise ValueError('work count must be exactly one')


def _check_same_identity(snapshot: MemorySnapshot, baseline: MemorySnapshot) -> None:
    for field in _ID_FIELDS + ('current_transition_id', 'transition_exact_document_id'):
        if getattr(snapshot, field) != getattr(baseline, field):
            raise ValueError(f'{field.removesuffix("_id")} identity changed')
    if snapshot.exact_projection_hash != baseline.exact_projection_hash:
        raise ValueError('exact projection hash changed')


def validate_pre_kill(snapshot: MemorySnapshot) -> None:
    _check_chain(snapshot)
    if snapshot.work_execution_state != 'ready':
        raise ValueError('work must be ready before worker start')
    if snapshot.active_run_count != 0:
        raise ValueError('active run count must be zero before worker start')
    if snapshot.embedding_projection_hash or snapshot.embedding_reference:
        raise ValueError('embedding projection must be blank before worker start')
    if snapshot.embedding_vector_count != 0 or not snapshot.embedding_pgvector_is_null:
        raise ValueError('embedding vector must be blank before worker start')


def validate_active_claim(snapshot: MemorySnapshot, baseline: MemorySnapshot) -> None:
    _check_chain(snapshot)
    _check_same_identity(snapshot, baseline)
    if snapshot.work_execution_state != 'leased' or snapshot.active_run_count != 1:
        raise ValueError('work must have exactly one active lease')
    if snapshot.embedding_projection_hash or snapshot.embedding_reference:
        raise ValueError('embedding projection must remain blank while leased')
    if snapshot.embedding_vector_count != 0 or not snapshot.embedding_pgvector_is_null:
        raise ValueError('embedding vector must remain blank while leased')


def validate_recovered(snapshot: MemorySnapshot, baseline: MemorySnapshot) -> None:
    _check_chain(snapshot)
    _check_same_identity(snapshot, baseline)
    if snapshot.work_execution_state != 'settled':
        raise ValueError('work must be settled after recovery')
    if snapshot.active_run_count != 0:
        raise ValueError('active run count must be zero after recovery')
    if snapshot.embedding_projection_hash != snapshot.exact_projection_hash:
        raise ValueError('embedding projection hash must equal exact projection hash')
    if not snapshot.embedding_reference:
        raise ValueError('embedding reference must be populated after recovery')
    if snapshot.embedding_vector_count <= 0 or snapshot.embedding_pgvector_is_null:
        raise ValueError('one current embedding vector must be populated after recovery')


def validate_project_name(project: str) -> None:
    if PROJECT_PATTERN.fullmatch(project) is None:
        raise HarnessError(f'Refusing unsafe disposable Compose project {project!r}')


def _validate_path(path: Path, label: str) -> None:
    if not path.is_absolute() or path != path.resolve():
        raise HarnessError(f'{label} must be an absolute canonical path')


def deterministic_env(env_file: Path, *, source: Mapping[str, str] = os.environ) -> dict[str, str]:
    _validate_path(env_file, 'generated env file')
    allowed = (
        'PATH',
        'HOME',
        'USER',
        'TEMP',
        'TMP',
        'SYSTEMROOT',
        'COMSPEC',
        'PATHEXT',
        'USERPROFILE',
        'APPDATA',
        'LOCALAPPDATA',
        'ProgramFiles',
        'XDG_RUNTIME_DIR',
        'DOCKER_HOST',
        'DOCKER_CONTEXT',
        'DOCKER_CONFIG',
    )
    result = {key: source[key] for key in allowed if source.get(key)}
    result.update({'LC_ALL': 'C.UTF-8', 'COMPOSE_ANSI': 'never', 'ENGRAM_ENV_FILE': str(env_file)})
    return result


def write_env_file(path: Path) -> None:
    _validate_path(path, 'generated env file')
    path.write_text(
        '\n'.join(
            (
                'ENGRAM_ENVIRONMENT=dev',
                'ENGRAM_SECRET_KEY=',
                'ENGRAM_DEBUG=false',
                'ENGRAM_ALLOWED_HOSTS=localhost,127.0.0.1,0.0.0.0',
                'ENGRAM_LOG_LEVEL=INFO',
                'ENGRAM_PROVIDER_MODE=fake',
                'ENGRAM_PROVIDER_HTTP_TIMEOUT=3',
                'ENGRAM_RABBITMQ_HOSTNAME=rabbitmq',
                'ENGRAM_RABBITMQ_NODENAME=rabbit@rabbitmq',
                '',
            )
        ),
        encoding='utf-8',
    )


def write_override_file(path: Path, project: str) -> None:
    _validate_path(path, 'generated Compose override')
    validate_project_name(project)
    image = f'{project}-backend:c43'
    path.write_text(
        dedent(f"""
        services:
          api:
            image: {image}
            ports: !override
              - "127.0.0.1::8000"
          relay:
            image: {image}
          worker-batch:
            image: {image}
            environment:
              ENGRAM_FAKE_PROVIDER_DELAY_MS: "5000"
    """).lstrip(),
        encoding='utf-8',
    )


def redact_diagnostics(value: str, secrets_to_redact: Sequence[str] = ()) -> str:
    result = value
    for secret in sorted({item for item in secrets_to_redact if item}, key=len, reverse=True):
        result = result.replace(secret, '[REDACTED]')
    result = re.sub(r'(?<![A-Za-z0-9])egk_[A-Za-z0-9_.-]+', '[REDACTED]', result)
    result = re.sub(r'(?<![A-Za-z0-9])sk-[A-Za-z0-9_.-]+', '[REDACTED]', result)
    return result[-OUTPUT_LIMIT:]


@dataclass(frozen=True, slots=True)
class CommandResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[..., subprocess.CompletedProcess[str]]


class Deadline:
    def __init__(self, timeout: float, *, label: str = 'deadline') -> None:
        if timeout <= 0:
            raise HarnessError(f'{label} must be positive')
        self.expires_at = time.monotonic() + timeout
        self.label = label

    def remaining(self) -> float:
        remaining = self.expires_at - time.monotonic()
        if remaining <= 0:
            raise HarnessError(f'Harness exceeded its {self.label}')
        return remaining


class ComposeHarness:
    def __init__(
        self,
        project: str,
        env_file: Path,
        override_file: Path,
        secrets_to_redact: Sequence[str],
        runner: Runner = subprocess.run,
    ) -> None:
        validate_project_name(project)
        _validate_path(env_file, 'generated env file')
        _validate_path(override_file, 'generated Compose override')
        self.project = project
        self.env_file = env_file
        self.override_file = override_file
        self.secrets_to_redact = tuple(secrets_to_redact)
        self.runner = runner
        self.deadline = Deadline(GLOBAL_TIMEOUT, label='global deadline')

    @property
    def compose_prefix(self) -> list[str]:
        return [
            'docker',
            'compose',
            '--env-file',
            str(self.env_file),
            '-p',
            self.project,
            '-f',
            str(COMPOSE_FILE),
            '-f',
            str(self.override_file),
        ]

    def run(
        self,
        args: Sequence[str],
        *,
        timeout: float = COMMAND_TIMEOUT,
        check: bool = True,
    ) -> CommandResult:
        effective = min(timeout, self.deadline.remaining())
        try:
            completed = self.runner(
                list(args),
                cwd=ROOT,
                env=deterministic_env(self.env_file),
                text=True,
                capture_output=True,
                timeout=effective,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            command = redact_diagnostics(' '.join(args), self.secrets_to_redact)
            raise HarnessError(f'Command timed out after {effective:.1f}s: {command}') from error
        result = CommandResult(
            tuple(str(item) for item in args),
            completed.returncode,
            completed.stdout,
            completed.stderr,
        )
        if check and result.returncode != 0:
            command = redact_diagnostics(' '.join(result.args), self.secrets_to_redact)
            output = redact_diagnostics(result.stdout + result.stderr, self.secrets_to_redact)
            raise HarnessError(f'Command failed ({result.returncode}): {command}\n{output}')
        return result

    def compose(self, *args: str, timeout: float = COMMAND_TIMEOUT, check: bool = True) -> CommandResult:
        return self.run([*self.compose_prefix, *args], timeout=timeout, check=check)

    def assert_project_absent(self) -> None:
        for kind in ('container', 'network', 'volume'):
            flags = ['-aq'] if kind == 'container' else ['-q']
            result = self.run(
                [
                    'docker',
                    kind,
                    'ls',
                    *flags,
                    '--filter',
                    f'label=com.docker.compose.project={self.project}',
                ],
                timeout=30,
            )
            if result.stdout.strip():
                raise HarnessError(f'Refusing pre-existing Compose {kind} resources for {self.project}')

    def shell_json(self, code: str, *, timeout: float = COMMAND_TIMEOUT) -> dict[str, object]:
        result = self.compose(
            'exec',
            '-T',
            'api',
            'python',
            'manage.py',
            'shell',
            '-c',
            code,
            timeout=timeout,
        )
        for line in reversed(result.stdout.splitlines()):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                return payload
        raise HarnessError('Django shell did not emit a JSON object')

    def poll(
        self,
        label: str,
        probe: Callable[[], MemorySnapshot],
        *,
        timeout: float = POLL_TIMEOUT,
    ) -> MemorySnapshot:
        deadline = Deadline(min(timeout, self.deadline.remaining()), label=f'{label} deadline')
        last_error = 'probe did not run'
        while True:
            try:
                return probe()
            except (HarnessError, ValueError) as error:
                last_error = str(error)
            try:
                time.sleep(min(POLL_INTERVAL, deadline.remaining()))
            except HarnessError as error:
                raise HarnessError(f'Timed out waiting for {label}: {last_error}') from error


def emit(message: str) -> None:
    sys.stdout.write(f'{message}\n')
    sys.stdout.flush()


def cleanup_compose(harness: ComposeHarness) -> int:
    command = [*harness.compose_prefix, 'down', '-v', '--remove-orphans']
    try:
        result = harness.runner(
            command,
            cwd=ROOT,
            env=deterministic_env(harness.env_file),
            text=True,
            capture_output=True,
            timeout=180.0,
            check=False,
        )
    except subprocess.TimeoutExpired:
        emit('cleanup failure: Compose down timed out')

        return 1
    if result.returncode:
        emit(
            f'cleanup failure ({result.returncode}): '
            f'{redact_diagnostics(result.stdout + result.stderr, harness.secrets_to_redact)}'
        )

    return result.returncode


def _literal(value: object) -> str:
    return json.dumps(value)


def snapshot_query(ids: Mapping[str, str]) -> str:
    values = {key: str(uuid.UUID(value)) for key, value in ids.items()}
    return dedent(f"""
        import json, uuid
        from engram.core.models import (
            AuditEvent,
            MemoryCandidate,
            MemoryTransition,
            RetrievalDocument,
            WorkflowRun,
            WorkflowWork,
        )
        candidate_id = uuid.UUID({_literal(values['candidate_id'])})
        candidate = MemoryCandidate.objects.get(id=candidate_id)
        transition = (
            MemoryTransition.objects.filter(candidate_id=candidate_id)
            .order_by('created_at', 'id')
            .first()
        )
        memory = transition.result_memory if transition else None
        version = transition.result_version if transition else None
        document = transition.result_exact_document if transition else None
        work = transition.embedding_work if transition else None
        payload = {{
            'candidate_id': str(candidate.id),
            'memory_id': str(memory.id) if memory else '',
            'version_id': str(version.id) if version else '',
            'transition_id': str(transition.id) if transition else '',
            'document_id': str(document.id) if document else '',
            'work_id': str(work.id) if work else '',
            'current_transition_id': (
                str(memory.current_transition_id)
                if memory and memory.current_transition_id
                else ''
            ),
            'transition_exact_document_id': str(transition.result_exact_document_id) if transition else '',
            'exact_projection_hash': document.exact_projection_hash if document else '',
            'embedding_projection_hash': document.embedding_projection_hash if document else '',
            'embedding_reference': document.embedding_reference if document else '',
            'embedding_vector_count': len(document.embedding_vector or []) if document else 0,
            'embedding_pgvector_is_null': (document.embedding_pgvector is None) if document else True,
            'transition_count': MemoryTransition.objects.filter(candidate_id=candidate_id).count(),
            'audit_count': AuditEvent.objects.filter(id=transition.audit_event_id).count() if transition else 0,
            'document_count': RetrievalDocument.objects.filter(memory_id=memory.id).count() if memory else 0,
            'work_count': WorkflowWork.objects.filter(
                organization_id=document.organization_id,
                project_id=document.project_id,
                work_type='memory_embedding',
                subject_type='retrieval_document',
                subject_id=document.id,
                input_snapshot__exact_projection_hash=document.exact_projection_hash,
            ).count() if document else 0,
            'work_execution_state': work.execution_state if work else '',
            'active_run_count': WorkflowRun.objects.filter(work_id=work.id, status='running').count() if work else 0,
        }}
        print(json.dumps(payload, sort_keys=True))
    """).strip()


def seed_code(suffix: str, search_key: str) -> str:
    return dedent(f"""
        import hashlib, json, uuid
        from django.utils import timezone
        from engram.core.models import (
            Agent,
            AgentSession,
            MemoryCandidate,
            MemoryCandidateSource,
            Observation,
            ObservationSource,
            Organization,
            Project,
            Runtime,
            Team,
        )
        from engram.memory.import_provenance import candidate_evidence_manifest
        from engram.memory.import_provenance import (
            import_candidate_content_hash,
            import_candidate_source_anchors,
        )
        from engram.memory.transitions import (
            CandidateFence,
            PromoteMemoryCandidate,
            PromoteMemoryCandidateInput,
            TransitionRequest,
            TransitionScope,
        )
        from engram.memory.workflow_work import canonical_json_bytes
        from engram.search.services import SearchInput, SearchMemories
        seed_suffix = {_literal(suffix)}
        scope = (
            Organization.objects.get(slug='engram-e2e'),
            Project.objects.get(slug='backend'),
            Team.objects.get(slug='platform'),
        )
        organization, project, team = scope
        token = uuid.uuid4().hex
        agent = Agent.objects.create(
            organization=organization,
            runtime=Runtime.CODEX,
            external_id=f'c43-agent-{{seed_suffix}}-{{token}}',
        )
        session = AgentSession.objects.create(
            organization=organization, project=project, team=team, agent=agent,
            external_session_id=f'c43-session-{{seed_suffix}}-{{token}}', runtime=Runtime.CODEX,
            observation_sequence_cursor=1,
        )
        title = 'C4.3 exact recall marker'
        body = 'C4.3 atomic semantic commit remains exactly recallable while embedding is unavailable.'
        observation = Observation.objects.create(
            organization=organization, project=project, team=team, agent=agent, session=session,
            observation_type='tool_use', title=title, body=body, session_sequence=1,
            content_hash=f'c43-observation-{{token}}', source_metadata={{'event_type': 'post_tool_use'}},
            observed_at=timezone.now(),
        )
        import_source = ObservationSource.objects.create(
            organization=organization, project=project, observation=observation,
            source_type='claude_mem', source_id=f'c43-source-{{seed_suffix}}-{{token}}',
            metadata={{'source_store_id': 'c43-live', 'event_type': 'claude_mem.observation'}},
        )
        anchors = import_candidate_source_anchors(
            observation=observation, import_source=import_source,
            source_store_id='c43-live', event_type='claude_mem.observation',
        )
        candidate = MemoryCandidate.objects.create(
            organization=organization, project=project, team=team,
            source_observation=observation, title=title, body=body,
            status='proposed', visibility_scope='project', evidence=[{{'observation_id': str(observation.id)}},],
            content_hash=import_candidate_content_hash(import_source.source_id, observation.content_hash),
            confidence='0.900', decision_work_contract_version=1,
        )
        source = MemoryCandidateSource.objects.create(
            organization=organization, project=project, team=team, candidate=candidate,
            source_kind='import', observation=observation, import_source=import_source,
            anchors=anchors, anchors_hash=hashlib.sha256(canonical_json_bytes(anchors)).hexdigest(),
        )
        _entries, manifest_hash = candidate_evidence_manifest(candidate)
        request = TransitionRequest(
            scope=TransitionScope(organization_id=organization.id, project_id=project.id, team_id=team.id),
            idempotency_key=f'candidate:{{candidate.id}}:settle:v1', actor_type='e2e', actor_id='c43-live',
            capability='memories:write', request_id=f'c43-request:{{candidate.id}}',
            correlation_id=f'c43-correlation:{{candidate.id}}', reason='C4.3 live promotion', origin='e2e-c43',
        )
        result = PromoteMemoryCandidate().execute(PromoteMemoryCandidateInput(
            request=request,
            candidate_fence=CandidateFence(
                candidate_id=candidate.id,
                candidate_content_hash=candidate.content_hash,
                evidence_manifest_hash=manifest_hash,
            ),
        ))
        search = SearchMemories().execute(SearchInput(
            raw_key={_literal(search_key)}, project_id=project.id, team_id=team.id,
            query='C4.3 exact recall marker', file_paths=(), symbols=(), limit=1,
            request_id=f'c43-search:{{candidate.id}}', correlation_id=f'c43-search:{{candidate.id}}',
        ))
        if not any(match.document.id == result.retrieval_document.id for match in search.matches):
            raise RuntimeError('production search service did not return exact document')
        print(json.dumps({{
            'candidate_id': str(candidate.id),
            'project_id': str(project.id),
            'team_id': str(team.id),
            'memory_id': str(result.memory.id),
            'transition_id': str(result.transition.id),
            'document_id': str(result.retrieval_document.id),
            'work_id': str(result.embedding_work.id),
        }}, sort_keys=True))
    """).strip()


def expire_code(work_id: str) -> str:
    return dedent(f"""
        import json, uuid
        from datetime import timedelta
        from django.utils import timezone
        from engram.core.models import WorkflowRun, WorkflowWork
        from engram.memory.work_dispatch import queue_work_attempt
        now = timezone.now()
        work = WorkflowWork.objects.get(id=uuid.UUID({_literal(work_id)}))
        if work.execution_state != 'leased':
            raise RuntimeError(f'expected leased work, got {{work.execution_state}}')
        expired = now - timedelta(seconds=1)
        heartbeat = now - timedelta(seconds=30)
        WorkflowWork.objects.filter(id=work.id).update(
            lease_expires_at=expired,
            heartbeat_at=heartbeat,
        )
        WorkflowRun.objects.filter(work_id=work.id, status='running').update(
            lease_expires_at=expired,
            heartbeat_at=heartbeat,
        )
        queued = queue_work_attempt(work_id=work.id, now=now, origin='reconciliation')
        print(json.dumps({{'expired': 1, 'queued_run_id': str(queued.id), 'work_id': str(work.id)}}))
    """).strip()


def bootstrap_code(api_key: str, search_key: str) -> str:
    return dedent(f"""
        import json
        from engram.core.management.commands.engram_bootstrap_golden_path import bootstrap_golden_path
        result = bootstrap_golden_path(
            {_literal(api_key)},
            agent_key={_literal(search_key)},
        )
        print(json.dumps(result, sort_keys=True))
    """).strip()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Run the C4.3 atomic semantic commit Compose gate.')
    parser.add_argument('--project', default='')
    parsed = parser.parse_args(argv)
    project = parsed.project or f'engram-c43-atomic-{secrets.token_hex(8)}'
    validate_project_name(project)
    raw_api_key = f'egk_c43_api_{secrets.token_urlsafe(20)}'
    raw_search_key = f'egk_c43_agent_{secrets.token_urlsafe(20)}'
    generated_secrets = (raw_api_key, raw_search_key)
    primary_error: HarnessError | None = None
    cleanup_returncode = 0
    owns_project = False
    with tempfile.TemporaryDirectory(prefix='engram-c43-atomic-') as temporary:
        temp_dir = Path(temporary).resolve()
        env_file = (temp_dir / 'generated.env').resolve()
        override_file = (temp_dir / 'override.yml').resolve()
        write_env_file(env_file)
        write_override_file(override_file, project)
        harness = ComposeHarness(project, env_file, override_file, generated_secrets)
        try:
            harness.assert_project_absent()
            owns_project = True
            harness.compose('build', 'api', 'relay', 'worker-batch', timeout=STARTUP_TIMEOUT)
            harness.compose('up', '-d', '--wait', 'api', 'relay', timeout=STARTUP_TIMEOUT)
            harness.shell_json(bootstrap_code(raw_api_key, raw_search_key), timeout=120)
            ids = harness.shell_json(seed_code(f'c43-{secrets.token_hex(6)}', raw_search_key), timeout=180)
            baseline = parse_memory_snapshot(json.dumps(harness.shell_json(snapshot_query(ids), timeout=120)))
            validate_pre_kill(baseline)
            harness.compose('up', '-d', '--no-deps', 'worker-batch', timeout=60)

            def active_probe() -> MemorySnapshot:
                snapshot = parse_memory_snapshot(json.dumps(harness.shell_json(snapshot_query(ids), timeout=60)))
                validate_active_claim(snapshot, baseline)

                return snapshot

            harness.poll('one active embedding lease', active_probe, timeout=POLL_TIMEOUT)
            harness.compose('kill', '-s', 'SIGKILL', 'worker-batch', timeout=30)
            harness.compose('stop', '-t', '2', 'worker-batch', timeout=30, check=False)
            harness.shell_json(expire_code(ids['work_id']), timeout=60)
            harness.compose('up', '-d', '--no-deps', 'worker-batch', timeout=60)

            def recovered_probe() -> MemorySnapshot:
                snapshot = parse_memory_snapshot(json.dumps(harness.shell_json(snapshot_query(ids), timeout=60)))
                validate_recovered(snapshot, baseline)

                return snapshot

            recovered = harness.poll('same embedding work recovery', recovered_probe, timeout=POLL_TIMEOUT)
            evidence = {
                'candidate_id': recovered.candidate_id,
                'memory_id': recovered.memory_id,
                'version_id': recovered.version_id,
                'transition_id': recovered.transition_id,
                'document_id': recovered.document_id,
                'work_id': recovered.work_id,
                'exact_projection_hash': recovered.exact_projection_hash,
                'transition_count': recovered.transition_count,
                'audit_count': recovered.audit_count,
                'document_count': recovered.document_count,
                'work_count': recovered.work_count,
                'embedding_vector_count': recovered.embedding_vector_count,
            }
            emit(json.dumps({'evidence': evidence, 'status': 'passed'}, sort_keys=True))
        except (HarnessError, ValueError, RuntimeError) as error:
            primary_error = HarnessError(redact_diagnostics(str(error), generated_secrets))
            emit(f'failure: {primary_error}')
            if owns_project:
                try:
                    logs = harness.compose(
                        'logs',
                        '--no-color',
                        '--tail=120',
                        'api',
                        'relay',
                        'worker-batch',
                        timeout=30,
                        check=False,
                    )
                    diagnostic = redact_diagnostics(logs.stdout + logs.stderr, generated_secrets)
                    if diagnostic:
                        emit(f'failure logs tail:\n{diagnostic}')
                except HarnessError as log_error:
                    emit(f'failure logs unavailable: {redact_diagnostics(str(log_error), generated_secrets)}')
        finally:
            if owns_project:
                cleanup_returncode = cleanup_compose(harness)
    return 1 if primary_error is not None else cleanup_returncode


if __name__ == '__main__':
    raise SystemExit(main())
