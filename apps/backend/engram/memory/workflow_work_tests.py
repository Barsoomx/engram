from __future__ import annotations

import ast
import hashlib
import json
import threading
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from django.db import close_old_connections, transaction
from django.db.transaction import TransactionManagementError

from engram.core.models import (
    Agent,
    AgentSession,
    Observation,
    Organization,
    Project,
    ProjectTeam,
    Team,
    WorkflowSubjectType,
    WorkflowWork,
    WorkflowWorkDisposition,
    WorkflowWorkResolutionReason,
    WorkflowWorkType,
)
from engram.memory.workflow_work import (
    CreateWorkflowWorkInput,
    WorkflowWorkCollisionError,
    WorkflowWorkScopeError,
    WorkflowWorkStateError,
    canonical_json_bytes,
    create_work,
    observation_content_digest,
    resolve_work_no_input,
    resolve_work_no_signal,
    resolve_work_succeeded,
    work_input_fingerprint,
)

C11Scope = tuple[Organization, Team, Project, Agent, AgentSession]


def create_scope(suffix: str) -> C11Scope:
    organization = Organization.objects.create(name=f'Organization {suffix}', slug=f'organization-{suffix}')
    team = Team.objects.create(organization=organization, name=f'Team {suffix}', slug=f'team-{suffix}')
    project = Project.objects.create(organization=organization, name=f'Project {suffix}', slug=f'project-{suffix}')
    agent = Agent.objects.create(
        organization=organization,
        runtime='codex',
        external_id=f'agent-{suffix}',
    )
    session = AgentSession.objects.create(
        organization=organization,
        project=project,
        team=team,
        agent=agent,
        external_session_id=f'session-{suffix}',
        runtime='codex',
        observation_sequence_cursor=0,
    )

    return organization, team, project, agent, session


def create_observation(
    scope: C11Scope,
    *,
    suffix: str,
    source_metadata: dict[str, object] | None = None,
    session_sequence: int = 1,
) -> Observation:
    organization, team, project, agent, session = scope

    return Observation.objects.create(
        organization=organization,
        project=project,
        team=team,
        agent=agent,
        session=session,
        observation_type='decision',
        title=f'Decision {suffix}',
        subtitle=f'Subtitle {suffix}',
        body=f'Body {suffix}',
        facts=[f'fact-{suffix}'],
        narrative=f'Narrative {suffix}',
        concepts=['reliability', suffix],
        files_read=[f'src/{suffix}.py'],
        files_modified=[f'tests/{suffix}.py'],
        session_sequence=session_sequence,
        content_hash=f'client-hash-{suffix}',
        source_metadata=source_metadata or {'event_type': 'post_tool_use'},
    )


def observation_snapshot(
    observation: Observation,
    *,
    realtime_enabled: bool = True,
    fallback: bool = False,
) -> dict[str, object]:
    return {
        'schema': 'observation_processing_input/v1',
        'observation_id': str(observation.id),
        'observation_digest': observation_content_digest(observation),
        'policy': {
            'schema': 'hook_work_policy/v1',
            'realtime_candidates_enabled': realtime_enabled,
            'legacy_policy_fallback': fallback,
        },
    }


def observation_work_input(
    scope: C11Scope,
    observation: Observation,
    *,
    snapshot: dict[str, object] | None = None,
) -> CreateWorkflowWorkInput:
    organization, _team, project, _agent, _session = scope

    return CreateWorkflowWorkInput(
        organization_id=organization.id,
        project_id=project.id,
        work_type=WorkflowWorkType.OBSERVATION_PROCESSING,
        subject_type=WorkflowSubjectType.OBSERVATION,
        subject_id=observation.id,
        input_snapshot=snapshot or observation_snapshot(observation),
    )


def canonical_for_test(value: object) -> bytes:
    def normalize(item: object) -> object:
        if isinstance(item, uuid.UUID):
            return str(item)
        if isinstance(item, datetime):
            normalized = item.astimezone(UTC)
            timespec = 'microseconds' if normalized.microsecond else 'seconds'

            return normalized.isoformat(timespec=timespec).replace('+00:00', 'Z')
        if isinstance(item, list):
            return [normalize(child) for child in item]
        if isinstance(item, dict):
            return {key: normalize(child) for key, child in item.items()}

        return item

    return json.dumps(
        normalize(value),
        ensure_ascii=False,
        separators=(',', ':'),
        sort_keys=True,
    ).encode()


def manual_observation_digest(observation: Observation) -> str:
    values = (
        observation.id,
        observation.observation_type,
        observation.title,
        observation.subtitle,
        observation.body,
        observation.facts,
        observation.narrative,
        observation.concepts,
        observation.files_read,
        observation.files_modified,
        observation.source_metadata,
    )
    digest = hashlib.sha256()
    for value in values:
        encoded = canonical_for_test(value)
        digest.update(len(encoded).to_bytes(8, 'big', signed=False))
        digest.update(encoded)

    return digest.hexdigest()


def daily_snapshot(project: Project, *, input_digest: str) -> dict[str, object]:
    return {
        'schema': 'daily_digest_input/v1',
        'project_id': str(project.id),
        'schedule_key': 'daily:2026-07-10',
        'window_start': '2026-07-10T00:00:00Z',
        'window_end': '2026-07-11T00:00:00Z',
        'visibility_policy': 'digest_visibility/v1',
        'allowed_team_ids': [],
        'output_visibility_scope': 'project',
        'output_team_id': None,
        'eligible_source_count': 0,
        'max_sources': 200,
        'sources_truncated': False,
        'sources': [],
        'input_digest': input_digest,
    }


def weekly_snapshot(
    project: Project,
    *,
    team_id: uuid.UUID | None,
    input_digest: str,
) -> dict[str, object]:
    return {
        'schema': 'weekly_digest_input/v1',
        'project_id': str(project.id),
        'team_id': str(team_id) if team_id else None,
        'schedule_key': 'weekly:2026-W28',
        'window_start': '2026-07-06T00:00:00Z',
        'window_end': '2026-07-13T00:00:00Z',
        'visibility_policy': 'digest_visibility/v1',
        'allowed_team_ids': [str(team_id)] if team_id else [],
        'output_visibility_scope': 'team' if team_id else 'project',
        'output_team_id': str(team_id) if team_id else None,
        'changes': [],
        'input_digest': input_digest,
    }


def create_required_work(scope: C11Scope, *, suffix: str) -> WorkflowWork:
    observation = create_observation(scope, suffix=suffix)
    with transaction.atomic():
        work, created = create_work(observation_work_input(scope, observation))

    assert created is True

    return work


def create_empty_session_work(scope: C11Scope) -> WorkflowWork:
    organization, _team, project, _agent, session = scope
    data = CreateWorkflowWorkInput(
        organization_id=organization.id,
        project_id=project.id,
        work_type=WorkflowWorkType.SESSION_DISTILLATION,
        subject_type=WorkflowSubjectType.AGENT_SESSION,
        subject_id=session.id,
        input_snapshot={
            'schema': 'session_distillation_input/v1',
            'session_id': str(session.id),
            'lower_sequence_exclusive': 0,
            'upper_sequence_inclusive': 0,
        },
    )
    with transaction.atomic():
        work, created = create_work(data)

    assert created is True

    return work


def run_concurrent_creates(
    inputs: tuple[CreateWorkflowWorkInput, CreateWorkflowWorkInput],
) -> tuple[list[tuple[uuid.UUID, bool]], list[BaseException]]:
    barrier = threading.Barrier(2)
    results: list[tuple[uuid.UUID, bool]] = []
    errors: list[BaseException] = []
    result_lock = threading.Lock()

    def worker(data: CreateWorkflowWorkInput) -> None:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            with transaction.atomic():
                work, created = create_work(data)
            with result_lock:
                results.append((work.id, created))
        except BaseException as error:
            with result_lock:
                errors.append(error)
        finally:
            close_old_connections()

    threads = [threading.Thread(target=worker, args=(data,)) for data in inputs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert all(not thread.is_alive() for thread in threads)

    return results, errors


def test_canonical_json_bytes_are_sorted_compact_utf8_and_key_order_invariant() -> None:
    first = {'é': '✓', 'a': [1, True, None]}
    second = {'a': [1, True, None], 'é': '✓'}

    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    assert canonical_json_bytes(first) == '{"a":[1,true,null],"é":"✓"}'.encode()


def test_canonical_json_bytes_normalize_uuid_and_aware_datetimes() -> None:
    value_id = uuid.UUID('AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA')
    offset = timezone(timedelta(hours=3))

    assert canonical_json_bytes(value_id) == b'"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"'
    assert canonical_json_bytes(datetime(2026, 7, 10, 12, 34, 56, tzinfo=offset)) == (b'"2026-07-10T09:34:56Z"')
    assert canonical_json_bytes(datetime(2026, 7, 10, 12, 34, 56, 1000, tzinfo=offset)) == (
        b'"2026-07-10T09:34:56.001000Z"'
    )


@pytest.mark.parametrize(
    'value',
    [
        datetime.fromisoformat('2026-07-10T09:34:56'),
        1.0,
        Decimal('1.0'),
        b'value',
        {'value'},
        ('value',),
        {1: 'value'},
        -(2**63) - 1,
        2**63,
        object(),
    ],
)
def test_canonical_json_bytes_reject_noncanonical_values(value: object) -> None:
    with pytest.raises(ValueError):
        canonical_json_bytes(value)


def test_canonical_json_bytes_accept_signed_64_bit_json_boundaries() -> None:
    value = {'min': -(2**63), 'max': 2**63 - 1, 'bool': True, 'none': None, 'list': ['value']}

    assert canonical_json_bytes(value) == (
        b'{"bool":true,"list":["value"],"max":9223372036854775807,"min":-9223372036854775808,"none":null}'
    )


def test_work_input_fingerprint_matches_exact_observation_projection() -> None:
    subject_id = uuid.uuid4()
    snapshot = {
        'schema': 'observation_processing_input/v1',
        'observation_id': str(subject_id),
        'observation_digest': 'a' * 64,
        'policy': {
            'schema': 'hook_work_policy/v1',
            'realtime_candidates_enabled': True,
            'legacy_policy_fallback': False,
        },
    }
    projection = {
        'contract_version': 1,
        'identity_input': {
            'observation_id': str(subject_id),
            'observation_digest': 'a' * 64,
            'realtime_candidates_enabled': True,
        },
        'occurrence_key': '',
        'subject_id': str(subject_id),
        'subject_type': 'observation',
        'work_type': 'observation_processing',
    }

    fingerprint = work_input_fingerprint(
        work_type='observation_processing',
        subject_type='observation',
        subject_id=subject_id,
        contract_version=1,
        occurrence_key='',
        input_snapshot=snapshot,
    )

    assert fingerprint == hashlib.sha256(canonical_for_test(projection)).hexdigest()

    reordered = {
        'policy': snapshot['policy'],
        'observation_digest': 'a' * 64,
        'observation_id': str(subject_id),
        'schema': snapshot['schema'],
    }
    assert fingerprint == work_input_fingerprint(
        work_type='observation_processing',
        subject_type='observation',
        subject_id=subject_id,
        contract_version=1,
        occurrence_key='',
        input_snapshot=reordered,
    )


@pytest.mark.django_db
def test_observation_content_digest_uses_exact_order_and_big_endian_frames() -> None:
    scope = create_scope('digest-exact')
    observation = create_observation(scope, suffix='digest-exact')

    assert observation_content_digest(observation) == manual_observation_digest(observation)


@pytest.mark.django_db
def test_observation_content_digest_excludes_client_hash_and_session_sequence() -> None:
    scope = create_scope('digest-fields')
    observation = create_observation(scope, suffix='digest-fields')
    original = observation_content_digest(observation)

    Observation.objects.filter(id=observation.id).update(
        content_hash='changed-client-hash',
        session_sequence=99,
    )
    observation.refresh_from_db()
    assert observation_content_digest(observation) == original

    Observation.objects.filter(id=observation.id).update(source_metadata={'event_type': 'changed'})
    observation.refresh_from_db()
    assert observation_content_digest(observation) != original


def test_observation_identity_excludes_only_legacy_fallback_provenance() -> None:
    subject_id = uuid.uuid4()
    base = {
        'schema': 'observation_processing_input/v1',
        'observation_id': str(subject_id),
        'observation_digest': 'a' * 64,
        'policy': {
            'schema': 'hook_work_policy/v1',
            'realtime_candidates_enabled': True,
            'legacy_policy_fallback': False,
        },
    }

    def fingerprint(snapshot: dict[str, object]) -> str:
        return work_input_fingerprint(
            work_type='observation_processing',
            subject_type='observation',
            subject_id=subject_id,
            contract_version=1,
            occurrence_key='',
            input_snapshot=snapshot,
        )

    fallback = json.loads(json.dumps(base))
    fallback['policy']['legacy_policy_fallback'] = True
    changed_digest = json.loads(json.dumps(base))
    changed_digest['observation_digest'] = 'b' * 64
    changed_policy = json.loads(json.dumps(base))
    changed_policy['policy']['realtime_candidates_enabled'] = False

    assert fingerprint(base) == fingerprint(fallback)
    assert fingerprint(base) != fingerprint(changed_digest)
    assert fingerprint(base) != fingerprint(changed_policy)


def test_session_and_digest_identity_bind_complete_semantic_input() -> None:
    subject_id = uuid.uuid4()
    memory_id = uuid.uuid4()
    memory_version_id = uuid.uuid4()
    session = {
        'schema': 'session_distillation_input/v1',
        'session_id': str(subject_id),
        'lower_sequence_exclusive': 0,
        'upper_sequence_inclusive': 37,
    }
    changed_session = {**session, 'upper_sequence_inclusive': 38}
    changed_lower = {**session, 'lower_sequence_exclusive': 1}

    def fingerprint(work_type: str, subject_type: str, snapshot: dict[str, object]) -> str:
        return work_input_fingerprint(
            work_type=work_type,
            subject_type=subject_type,
            subject_id=subject_id,
            contract_version=1,
            occurrence_key='daily:2026-07-10' if work_type == 'daily_digest' else '',
            input_snapshot=snapshot,
        )

    assert fingerprint('session_distillation', 'agent_session', session) != fingerprint(
        'session_distillation',
        'agent_session',
        changed_session,
    )
    with pytest.raises(ValueError):
        fingerprint('session_distillation', 'agent_session', changed_lower)

    source = {
        'render_position': 0,
        'memory_id': str(memory_id),
        'memory_version_id': str(memory_version_id),
        'version': 1,
        'content_hash': 'legacy-source-hash',
        'server_body_digest': 'c' * 64,
        'visibility_scope': 'project',
        'team_id': None,
        'source_title': 'Frozen title',
    }
    digest = {
        'schema': 'daily_digest_input/v1',
        'project_id': str(subject_id),
        'schedule_key': 'daily:2026-07-10',
        'window_start': '2026-07-10T00:00:00Z',
        'window_end': '2026-07-11T00:00:00Z',
        'visibility_policy': 'digest_visibility/v1',
        'allowed_team_ids': [],
        'output_visibility_scope': 'project',
        'output_team_id': None,
        'eligible_source_count': 1,
        'max_sources': 200,
        'sources_truncated': False,
        'sources': [source],
        'input_digest': 'a' * 64,
    }
    reordered = dict(reversed(tuple(digest.items())))
    changed_digest = {**digest, 'input_digest': 'b' * 64}
    changed_versions = {
        **digest,
        'sources': [{**source, 'version': 2}],
    }
    assert fingerprint('daily_digest', 'project', digest) == fingerprint('daily_digest', 'project', reordered)
    assert fingerprint('daily_digest', 'project', digest) != fingerprint(
        'daily_digest',
        'project',
        changed_digest,
    )
    assert fingerprint('daily_digest', 'project', digest) != fingerprint(
        'daily_digest',
        'project',
        changed_versions,
    )


@pytest.mark.django_db(transaction=True)
def test_create_work_requires_active_transaction() -> None:
    scope = create_scope('transaction-required')
    observation = create_observation(scope, suffix='transaction-required')

    with pytest.raises(TransactionManagementError):
        create_work(observation_work_input(scope, observation))

    assert WorkflowWork.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_create_work_rolls_back_with_surrounding_transaction() -> None:
    scope = create_scope('transaction-rollback')
    observation = create_observation(scope, suffix='transaction-rollback')

    with pytest.raises(RuntimeError, match='rollback'):
        with transaction.atomic():
            create_work(observation_work_input(scope, observation))
            raise RuntimeError('rollback')

    assert WorkflowWork.objects.count() == 0


def test_workflow_work_module_has_no_transport_dependencies() -> None:
    tree = ast.parse(Path(__file__).with_name('workflow_work.py').read_text())
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                package = ('engram', 'memory')[: 3 - node.level]
                module_parts = (*package, *((node.module or '').split('.') if node.module else ()))
                module = '.'.join(module_parts)
            else:
                module = node.module or ''
            if module:
                imported_modules.add(module)
                imported_modules.update(f'{module}.{alias.name}' for alias in node.names)

    forbidden = ('celery', 'celery_outbox', 'django_celery_outbox', 'engram.memory.tasks')
    assert not any(
        module == prefix or module.startswith(f'{prefix}.') for module in imported_modules for prefix in forbidden
    )


@pytest.mark.django_db
def test_create_work_resolves_supported_subjects_and_derives_team() -> None:
    scope = create_scope('supported-subjects')
    organization, team, project, _agent, session = scope
    observation = create_observation(scope, suffix='supported-subjects')
    ProjectTeam.objects.create(organization=organization, project=project, team=team)

    inputs = (
        observation_work_input(scope, observation),
        CreateWorkflowWorkInput(
            organization_id=organization.id,
            project_id=project.id,
            work_type='session_distillation',
            subject_type='agent_session',
            subject_id=session.id,
            input_snapshot={
                'schema': 'session_distillation_input/v1',
                'session_id': str(session.id),
                'lower_sequence_exclusive': 0,
                'upper_sequence_inclusive': 1,
            },
        ),
        CreateWorkflowWorkInput(
            organization_id=organization.id,
            project_id=project.id,
            work_type='daily_digest',
            subject_type='project',
            subject_id=project.id,
            occurrence_key='daily:2026-07-10',
            input_snapshot=daily_snapshot(project, input_digest='a' * 64),
        ),
        CreateWorkflowWorkInput(
            organization_id=organization.id,
            project_id=project.id,
            work_type='weekly_digest',
            subject_type='project',
            subject_id=project.id,
            occurrence_key='weekly:2026-W28',
            input_snapshot=weekly_snapshot(project, team_id=None, input_digest='b' * 64),
        ),
        CreateWorkflowWorkInput(
            organization_id=organization.id,
            project_id=project.id,
            work_type='weekly_digest',
            subject_type='team',
            subject_id=team.id,
            occurrence_key='weekly:2026-W28',
            input_snapshot=weekly_snapshot(project, team_id=team.id, input_digest='c' * 64),
        ),
    )

    with transaction.atomic():
        results = [create_work(data) for data in inputs]

    assert [created for _work, created in results] == [True] * 5
    assert [work.team_id for work, _created in results] == [team.id, team.id, None, None, team.id]


@pytest.mark.django_db
def test_create_work_rejects_unscoped_or_unlinked_subjects() -> None:
    scope = create_scope('scope-local')
    organization, _team, project, _agent, _session = scope
    foreign_scope = create_scope('scope-foreign')
    _foreign_organization, foreign_team, _foreign_project, _foreign_agent, foreign_session = foreign_scope
    foreign_observation = create_observation(foreign_scope, suffix='scope-foreign')
    unlinked_team = Team.objects.create(organization=organization, name='Unlinked', slug='unlinked')
    missing_observation_id = uuid.uuid4()
    missing_session_id = uuid.uuid4()

    inputs = (
        CreateWorkflowWorkInput(
            organization_id=organization.id,
            project_id=project.id,
            work_type='observation_processing',
            subject_type='observation',
            subject_id=foreign_observation.id,
            input_snapshot=observation_snapshot(foreign_observation),
        ),
        CreateWorkflowWorkInput(
            organization_id=organization.id,
            project_id=project.id,
            work_type='observation_processing',
            subject_type='observation',
            subject_id=missing_observation_id,
            input_snapshot={
                'schema': 'observation_processing_input/v1',
                'observation_id': str(missing_observation_id),
                'observation_digest': 'a' * 64,
                'policy': {
                    'schema': 'hook_work_policy/v1',
                    'realtime_candidates_enabled': True,
                    'legacy_policy_fallback': False,
                },
            },
        ),
        CreateWorkflowWorkInput(
            organization_id=organization.id,
            project_id=project.id,
            work_type='session_distillation',
            subject_type='agent_session',
            subject_id=foreign_session.id,
            input_snapshot={'schema': 'session_distillation_input/v1', 'session_id': str(foreign_session.id)},
        ),
        CreateWorkflowWorkInput(
            organization_id=organization.id,
            project_id=project.id,
            work_type='session_distillation',
            subject_type='agent_session',
            subject_id=missing_session_id,
            input_snapshot={'schema': 'session_distillation_input/v1', 'session_id': str(missing_session_id)},
        ),
        CreateWorkflowWorkInput(
            organization_id=organization.id,
            project_id=project.id,
            work_type='weekly_digest',
            subject_type='team',
            subject_id=foreign_team.id,
            occurrence_key='weekly:2026-W28',
            input_snapshot=weekly_snapshot(project, team_id=foreign_team.id, input_digest='a' * 64),
        ),
        CreateWorkflowWorkInput(
            organization_id=organization.id,
            project_id=project.id,
            work_type='weekly_digest',
            subject_type='team',
            subject_id=unlinked_team.id,
            occurrence_key='weekly:2026-W28',
            input_snapshot=weekly_snapshot(project, team_id=unlinked_team.id, input_digest='b' * 64),
        ),
    )

    for data in inputs:
        with pytest.raises(WorkflowWorkScopeError), transaction.atomic():
            create_work(data)

    assert WorkflowWork.objects.count() == 0


@pytest.mark.django_db
def test_create_work_rejects_unsupported_pair_or_fabricated_observation_input() -> None:
    scope = create_scope('fabricated-input')
    organization, _team, project, _agent, _session = scope
    observation = create_observation(scope, suffix='fabricated-input')
    valid_snapshot = observation_snapshot(observation)
    wrong_observation_id = {**valid_snapshot, 'observation_id': str(uuid.uuid4())}
    wrong_digest = {**valid_snapshot, 'observation_digest': 'f' * 64}
    inputs = (
        CreateWorkflowWorkInput(
            organization_id=organization.id,
            project_id=project.id,
            work_type='session_distillation',
            subject_type='observation',
            subject_id=observation.id,
            input_snapshot=valid_snapshot,
        ),
        observation_work_input(scope, observation, snapshot=wrong_observation_id),
        observation_work_input(scope, observation, snapshot=wrong_digest),
    )

    for data in inputs:
        with pytest.raises(ValueError), transaction.atomic():
            create_work(data)

    assert WorkflowWork.objects.count() == 0


@pytest.mark.django_db
def test_create_work_rejects_non_v1_or_malformed_work_snapshots() -> None:
    scope = create_scope('malformed-snapshot')
    organization, team, project, _agent, session = scope
    observation = create_observation(scope, suffix='malformed-snapshot')
    ProjectTeam.objects.create(organization=organization, project=project, team=team)
    observation_extra = {**observation_snapshot(observation), 'unknown': 'value'}
    observation_policy_extra = observation_snapshot(observation)
    observation_policy_extra['policy'] = {**observation_policy_extra['policy'], 'unknown': 'value'}
    invalid_session = {
        'schema': 'session_distillation_input/v1',
        'session_id': str(session.id),
        'lower_sequence_exclusive': -1,
        'upper_sequence_inclusive': 0,
    }
    missing_digest = daily_snapshot(project, input_digest='a' * 64)
    del missing_digest['input_digest']
    wrong_team = weekly_snapshot(project, team_id=team.id, input_digest='b' * 64)
    wrong_team['team_id'] = None
    inputs = (
        replace(observation_work_input(scope, observation), contract_version=2),
        observation_work_input(scope, observation, snapshot=observation_extra),
        observation_work_input(scope, observation, snapshot=observation_policy_extra),
        CreateWorkflowWorkInput(
            organization_id=organization.id,
            project_id=project.id,
            work_type='session_distillation',
            subject_type='agent_session',
            subject_id=session.id,
            input_snapshot=invalid_session,
        ),
        CreateWorkflowWorkInput(
            organization_id=organization.id,
            project_id=project.id,
            work_type='daily_digest',
            subject_type='project',
            subject_id=project.id,
            occurrence_key='daily:2026-07-10',
            input_snapshot=missing_digest,
        ),
        CreateWorkflowWorkInput(
            organization_id=organization.id,
            project_id=project.id,
            work_type='weekly_digest',
            subject_type='team',
            subject_id=team.id,
            occurrence_key='weekly:2026-W28',
            input_snapshot=wrong_team,
        ),
    )

    for data in inputs:
        with pytest.raises(ValueError), transaction.atomic():
            create_work(data)

    assert WorkflowWork.objects.count() == 0


@pytest.mark.django_db
def test_create_work_rejects_cross_organization_derived_teams() -> None:
    scope = create_scope('derived-team-local')
    organization, _team, project, _agent, session = scope
    observation = create_observation(scope, suffix='derived-team-local')
    _foreign_organization, foreign_team, _foreign_project, _foreign_agent, _foreign_session = create_scope(
        'derived-team-foreign'
    )
    Observation.objects.filter(id=observation.id).update(team_id=foreign_team.id)
    AgentSession.objects.filter(id=session.id).update(team_id=foreign_team.id)
    inputs = (
        observation_work_input(scope, observation),
        CreateWorkflowWorkInput(
            organization_id=organization.id,
            project_id=project.id,
            work_type='session_distillation',
            subject_type='agent_session',
            subject_id=session.id,
            input_snapshot={
                'schema': 'session_distillation_input/v1',
                'session_id': str(session.id),
                'lower_sequence_exclusive': 0,
                'upper_sequence_inclusive': 1,
            },
        ),
    )

    for data in inputs:
        with pytest.raises(WorkflowWorkScopeError), transaction.atomic():
            create_work(data)

    assert WorkflowWork.objects.count() == 0


@pytest.mark.django_db
def test_create_work_rejects_observation_without_required_semantic_policy() -> None:
    scope = create_scope('observation-policy')
    observation = create_observation(scope, suffix='observation-policy')
    lifecycle = create_observation(
        scope,
        suffix='observation-lifecycle',
        source_metadata={'event_type': 'session_start'},
        session_sequence=2,
    )

    for subject, snapshot in (
        (observation, observation_snapshot(observation, realtime_enabled=False)),
        (lifecycle, observation_snapshot(lifecycle)),
    ):
        with pytest.raises(ValueError), transaction.atomic():
            create_work(observation_work_input(scope, subject, snapshot=snapshot))

    assert WorkflowWork.objects.count() == 0


@pytest.mark.django_db
def test_observation_provenance_difference_reuses_first_snapshot() -> None:
    scope = create_scope('provenance-reuse')
    observation = create_observation(scope, suffix='provenance-reuse')
    first_snapshot = observation_snapshot(observation, fallback=True)
    second_snapshot = observation_snapshot(observation, fallback=False)

    with transaction.atomic():
        first, first_created = create_work(observation_work_input(scope, observation, snapshot=first_snapshot))
        second, second_created = create_work(observation_work_input(scope, observation, snapshot=second_snapshot))

    first.refresh_from_db()
    assert first.id == second.id
    assert (first_created, second_created) == (True, False)
    assert first.input_snapshot == first_snapshot


@pytest.mark.django_db
def test_existing_semantic_projection_or_team_collision_fails_closed() -> None:
    first_scope = create_scope('semantic-collision')
    first_organization, first_team, first_project, _first_agent, _first_session = first_scope
    first_observation = create_observation(first_scope, suffix='semantic-collision')
    first_data = observation_work_input(first_scope, first_observation)
    expected_fingerprint = work_input_fingerprint(
        work_type=first_data.work_type,
        subject_type=first_data.subject_type,
        subject_id=first_data.subject_id,
        contract_version=first_data.contract_version,
        occurrence_key=first_data.occurrence_key,
        input_snapshot=first_data.input_snapshot,
    )
    tampered_snapshot = {**first_data.input_snapshot, 'observation_digest': 'f' * 64}
    semantic_work = WorkflowWork.objects.create(
        organization=first_organization,
        project=first_project,
        team=first_team,
        work_type=first_data.work_type,
        subject_type=first_data.subject_type,
        subject_id=first_data.subject_id,
        contract_version=first_data.contract_version,
        occurrence_key='',
        input_fingerprint=expected_fingerprint,
        input_snapshot=tampered_snapshot,
    )

    with pytest.raises(WorkflowWorkCollisionError), transaction.atomic():
        create_work(first_data)

    semantic_work.refresh_from_db()
    assert semantic_work.input_snapshot == tampered_snapshot

    second_scope = create_scope('team-collision')
    second_organization, _second_team, second_project, _second_agent, _second_session = second_scope
    second_observation = create_observation(second_scope, suffix='team-collision')
    second_data = observation_work_input(second_scope, second_observation)
    wrong_team = Team.objects.create(organization=second_organization, name='Wrong team', slug='wrong-team')
    team_work = WorkflowWork.objects.create(
        organization=second_organization,
        project=second_project,
        team=wrong_team,
        work_type=second_data.work_type,
        subject_type=second_data.subject_type,
        subject_id=second_data.subject_id,
        contract_version=second_data.contract_version,
        occurrence_key='',
        input_fingerprint=work_input_fingerprint(
            work_type=second_data.work_type,
            subject_type=second_data.subject_type,
            subject_id=second_data.subject_id,
            contract_version=second_data.contract_version,
            occurrence_key='',
            input_snapshot=second_data.input_snapshot,
        ),
        input_snapshot=second_data.input_snapshot,
    )

    with pytest.raises(WorkflowWorkCollisionError), transaction.atomic():
        create_work(second_data)

    team_work.refresh_from_db()
    assert team_work.team_id == wrong_team.id


@pytest.mark.django_db
def test_digest_occurrence_reuses_first_frozen_snapshot() -> None:
    scope = create_scope('digest-occurrence')
    organization, _team, project, _agent, _session = scope
    first_snapshot = daily_snapshot(project, input_digest='a' * 64)
    second_snapshot = daily_snapshot(project, input_digest='b' * 64)

    def data(snapshot: dict[str, object]) -> CreateWorkflowWorkInput:
        return CreateWorkflowWorkInput(
            organization_id=organization.id,
            project_id=project.id,
            work_type='daily_digest',
            subject_type='project',
            subject_id=project.id,
            occurrence_key='daily:2026-07-10',
            input_snapshot=snapshot,
        )

    with transaction.atomic():
        first, first_created = create_work(data(first_snapshot))
        second, second_created = create_work(data(second_snapshot))

    first.refresh_from_db()
    assert first.id == second.id
    assert (first_created, second_created) == (True, False)
    assert first.input_snapshot == first_snapshot


@pytest.mark.django_db
def test_digest_full_identity_collision_without_occurrence_winner_fails_closed() -> None:
    scope = create_scope('digest-identity-collision')
    organization, _team, project, _agent, _session = scope
    proposed = CreateWorkflowWorkInput(
        organization_id=organization.id,
        project_id=project.id,
        work_type='daily_digest',
        subject_type='project',
        subject_id=project.id,
        occurrence_key='daily:2026-07-10',
        input_snapshot=daily_snapshot(project, input_digest='a' * 64),
    )
    fingerprint = work_input_fingerprint(
        work_type=proposed.work_type,
        subject_type=proposed.subject_type,
        subject_id=proposed.subject_id,
        contract_version=proposed.contract_version,
        occurrence_key=proposed.occurrence_key,
        input_snapshot=proposed.input_snapshot,
    )
    WorkflowWork.objects.create(
        organization=organization,
        project=project,
        team=None,
        work_type=proposed.work_type,
        subject_type=proposed.subject_type,
        subject_id=proposed.subject_id,
        contract_version=proposed.contract_version,
        occurrence_key='daily:2026-07-09',
        input_fingerprint=fingerprint,
        input_snapshot={**proposed.input_snapshot, 'schedule_key': 'daily:2026-07-09'},
    )

    with pytest.raises(WorkflowWorkCollisionError), transaction.atomic():
        create_work(proposed)

    assert WorkflowWork.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_concurrent_create_converges_on_one_work() -> None:
    scope = create_scope('concurrent')
    observation = create_observation(scope, suffix='concurrent')
    data = observation_work_input(scope, observation)
    results, errors = run_concurrent_creates((data, data))

    assert errors == []
    assert len({work_id for work_id, _created in results}) == 1
    assert {created for _work_id, created in results} == {True, False}
    assert WorkflowWork.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_concurrent_digest_occurrence_freezes_one_winner() -> None:
    scope = create_scope('concurrent-digest')
    organization, _team, project, _agent, _session = scope

    def data(input_digest: str) -> CreateWorkflowWorkInput:
        return CreateWorkflowWorkInput(
            organization_id=organization.id,
            project_id=project.id,
            work_type='daily_digest',
            subject_type='project',
            subject_id=project.id,
            occurrence_key='daily:2026-07-10',
            input_snapshot=daily_snapshot(project, input_digest=input_digest),
        )

    first = data('a' * 64)
    second = data('b' * 64)
    results, errors = run_concurrent_creates((first, second))

    assert errors == []
    assert len({work_id for work_id, _created in results}) == 1
    assert {created for _work_id, created in results} == {True, False}
    work = WorkflowWork.objects.get()
    assert work.input_snapshot in (first.input_snapshot, second.input_snapshot)


@pytest.mark.parametrize(
    ('resolver', 'disposition', 'reason'),
    [
        (resolve_work_succeeded, WorkflowWorkDisposition.COMPLETE, WorkflowWorkResolutionReason.SUCCEEDED),
        (resolve_work_no_signal, WorkflowWorkDisposition.COMPLETE, WorkflowWorkResolutionReason.NO_SIGNAL),
        (resolve_work_no_input, WorkflowWorkDisposition.NO_OP, WorkflowWorkResolutionReason.NO_INPUT),
    ],
)
@pytest.mark.django_db
def test_terminal_helpers_apply_exact_one_way_resolution(
    resolver: object,
    disposition: str,
    reason: str,
) -> None:
    scope = create_scope(f'terminal-{reason}')
    if resolver is resolve_work_no_input:
        work = create_empty_session_work(scope)
    else:
        work = create_required_work(scope, suffix=f'terminal-{reason}')

    resolved = resolver(
        work.id,
        organization_id=work.organization_id,
        project_id=work.project_id,
    )

    assert resolved.disposition == disposition
    assert resolved.resolution_reason == reason
    assert resolved.resolved_at is not None


@pytest.mark.django_db
def test_terminal_helpers_are_idempotent_only_for_same_resolution() -> None:
    scope = create_scope('terminal-idempotency')
    work = create_required_work(scope, suffix='terminal-idempotency')

    scope_kwargs = {
        'organization_id': work.organization_id,
        'project_id': work.project_id,
    }
    first = resolve_work_succeeded(work.id, **scope_kwargs)
    second = resolve_work_succeeded(work.id, **scope_kwargs)

    assert second.resolved_at == first.resolved_at

    with pytest.raises(WorkflowWorkStateError):
        resolve_work_no_signal(work.id, **scope_kwargs)

    work.refresh_from_db()
    assert work.disposition == WorkflowWorkDisposition.COMPLETE
    assert work.resolution_reason == WorkflowWorkResolutionReason.SUCCEEDED


@pytest.mark.django_db
def test_terminal_helpers_require_matching_scope_and_valid_no_input_type() -> None:
    scope = create_scope('terminal-scope')
    foreign_scope = create_scope('terminal-scope-foreign')
    work = create_required_work(scope, suffix='terminal-scope')
    foreign_organization, _foreign_team, foreign_project, _foreign_agent, _foreign_session = foreign_scope

    with pytest.raises(WorkflowWorkScopeError):
        resolve_work_succeeded(
            work.id,
            organization_id=foreign_organization.id,
            project_id=foreign_project.id,
        )
    with pytest.raises(WorkflowWorkStateError):
        resolve_work_no_input(
            work.id,
            organization_id=work.organization_id,
            project_id=work.project_id,
        )

    work.refresh_from_db()
    assert work.disposition == WorkflowWorkDisposition.REQUIRED
