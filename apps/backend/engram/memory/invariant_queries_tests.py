from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from engram.core.models import (
    Agent,
    AgentSession,
    CandidateStatus,
    LinkType,
    Memory,
    MemoryCandidate,
    MemoryLink,
    MemoryStatus,
    MemoryVersion,
    Observation,
    ObservationSource,
    Organization,
    Project,
    RawEventEnvelope,
    RetrievalDocument,
    Runtime,
    SessionStatus,
    Team,
    VisibilityScope,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowRunType,
    WorkflowSubjectType,
    WorkflowWork,
    WorkflowWorkType,
)
from engram.memory.conflict_links import conflict_candidate_target
from engram.memory.invariant_queries import (
    InvariantId,
    InvariantResult,
    InvariantState,
    evaluate_invariants,
    evaluate_post_cutover_p1_p2,
)
from engram.memory.workflow_work import (
    CreateWorkflowWorkInput,
    create_work,
    observation_content_digest,
    work_input_fingerprint,
)

FIXTURE_PATH = Path(__file__).parent / 'fixtures' / 'autonomous_memory_loop_baseline.json'
EXPECTED_CASES = {
    'no_run_session',
    'stale_running_work',
    'latest_failure_after_prior_success',
    'duplicate_delivery',
    'orphan_candidate',
    'partial_promotion',
    'conflict',
    'oversized_session',
}
VALID_INVARIANTS = {f'P{number}' for number in range(1, 16)}
VALID_STATES = {'healthy', 'violated', 'missing_observability'}
EXPECTED_ENTRY_KEYS = {
    'invariant_id',
    'state',
    'reason',
    'violation_count',
    'proxy_count',
    'sample_refs',
    'missing_evidence',
    'target_checkpoint',
}
FORBIDDEN_KEYS = {
    'title',
    'body',
    'payload',
    'repository_url',
    'provider',
    'model',
    'failure_reason',
    'task_args',
    'dsn',
    'secret',
}
FIXED_AS_OF = datetime(2026, 7, 10, 12, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class ScopeFixture:
    organization: Organization
    project: Project
    team: Team
    agent: Agent


def _load_manifest() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text())


def _nested_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {key for item in value.values() for key in _nested_keys(item)}

    if isinstance(value, list):
        return {key for item in value for key in _nested_keys(item)}

    return set()


def _synthetic_id(ref: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f'engram-cp0:{ref}')


def _expected_sample_ids(sample_refs: list[str]) -> tuple[str, ...]:
    qualified = []

    for ref in sample_refs:
        prefix, _separator, _name = ref.partition(':')
        qualified.append((prefix, _synthetic_id(ref)))

    return tuple(
        f'{prefix}:{entity_id}'
        for prefix, entity_id in sorted(
            qualified,
            key=lambda item: (item[1].int, item[0]),
        )
    )


def test_baseline_manifest_declares_all_synthetic_scenarios() -> None:
    manifest = _load_manifest()

    assert manifest['schema_version'] == 1
    assert set(manifest['scopes']) == {'target', 'foreign'}
    assert manifest['scopes']['target'] != manifest['scopes']['foreign']
    assert all(value.startswith('synthetic-') for scope in manifest['scopes'].values() for value in scope.values())

    scenarios = manifest['scenarios']

    assert {scenario['id'] for scenario in scenarios} == EXPECTED_CASES

    for scenario in scenarios:
        assert set(scenario) == {
            'id',
            'invariant_ids',
            'expected_characterization',
            'target',
            'foreign_tenant_control',
        }
        assert set(scenario['invariant_ids']) <= VALID_INVARIANTS
        assert [entry['invariant_id'] for entry in scenario['expected_characterization']] == scenario['invariant_ids']
        assert scenario['target']['scope_ref'] == 'target'
        assert scenario['foreign_tenant_control']['scope_ref'] == 'foreign'
        assert set(scenario['target']) == {'scope_ref', 'entity_refs'}
        assert set(scenario['foreign_tenant_control']) == {'scope_ref', 'entity_refs'}
        assert set(scenario['target']['entity_refs']).isdisjoint(
            scenario['foreign_tenant_control']['entity_refs'],
        )
        assert scenario['expected_characterization']

        for expected in scenario['expected_characterization']:
            assert set(expected) == EXPECTED_ENTRY_KEYS
            assert expected['invariant_id'] in scenario['invariant_ids']
            assert expected['state'] in VALID_STATES
            assert isinstance(expected['sample_refs'], list)


def test_baseline_manifest_excludes_sensitive_content_keys() -> None:
    assert not (_nested_keys(_load_manifest()) & FORBIDDEN_KEYS)


def _create_scope(prefix: str) -> ScopeFixture:
    organization = Organization.objects.create(
        id=_synthetic_id(f'organization:{prefix}'),
        name=f'Synthetic {prefix}',
        slug=f'synthetic-{prefix}',
    )
    project = Project.objects.create(
        id=_synthetic_id(f'project:{prefix}'),
        organization=organization,
        name=f'Synthetic project {prefix}',
        slug=f'project-{prefix}',
    )
    team = Team.objects.create(
        id=_synthetic_id(f'team:{prefix}'),
        organization=organization,
        name=f'Synthetic team {prefix}',
        slug=f'team-{prefix}',
    )
    agent = Agent.objects.create(
        id=_synthetic_id(f'agent:{prefix}'),
        organization=organization,
        runtime=Runtime.CODEX,
        external_id=f'agent-{prefix}',
    )

    return ScopeFixture(
        organization=organization,
        project=project,
        team=team,
        agent=agent,
    )


@pytest.fixture
def f_scope() -> ScopeFixture:
    return _create_scope('target')


@pytest.fixture
def f_foreign_scope() -> ScopeFixture:
    return _create_scope('foreign')


def _make_session(
    scope: ScopeFixture,
    suffix: str,
    *,
    status: str = SessionStatus.ENDED,
    entity_id: uuid.UUID | None = None,
) -> AgentSession:
    return AgentSession.objects.create(
        id=entity_id or uuid.uuid4(),
        organization=scope.organization,
        project=scope.project,
        team=scope.team,
        agent=scope.agent,
        external_session_id=f'session-{suffix}',
        runtime=Runtime.CODEX,
        status=status,
        ended_at=FIXED_AS_OF if status == SessionStatus.ENDED else None,
    )


def _make_raw_event(
    scope: ScopeFixture,
    session: AgentSession,
    suffix: str,
    *,
    entity_id: uuid.UUID | None = None,
    event_type: str = 'post_tool_use',
) -> RawEventEnvelope:
    return RawEventEnvelope.objects.create(
        id=entity_id or uuid.uuid4(),
        organization=scope.organization,
        project=scope.project,
        team=scope.team,
        agent=scope.agent,
        session=session,
        event_type=event_type,
        client_event_id=f'event-{suffix}',
        idempotency_key=f'idempotency-{suffix}',
        content_hash=f'raw-hash-{suffix}',
        runtime=Runtime.CODEX,
        payload={'synthetic': True},
        normalization_contract_version=0,
    )


def _make_observation(
    scope: ScopeFixture,
    session: AgentSession,
    suffix: str,
    *,
    observation_type: str = 'tool_use',
    entity_id: uuid.UUID | None = None,
) -> Observation:
    session_sequence = Observation.objects.filter(session=session).count() + 1

    return Observation.objects.create(
        id=entity_id or uuid.uuid4(),
        organization=scope.organization,
        project=scope.project,
        team=scope.team,
        agent=scope.agent,
        session=session,
        observation_type=observation_type,
        title=f'Synthetic observation {suffix}',
        body=f'Synthetic detail {suffix}',
        content_hash=f'observation-hash-{suffix}',
        observed_at=FIXED_AS_OF,
        session_sequence=session_sequence,
    )


def _make_source(
    scope: ScopeFixture,
    observation: Observation,
    raw_event: RawEventEnvelope,
    suffix: str,
    *,
    entity_id: uuid.UUID | None = None,
    source_type: str = 'raw_event',
    source_id: str | None = None,
    metadata: dict[str, object] | None = None,
) -> ObservationSource:
    return ObservationSource.objects.create(
        id=entity_id or uuid.uuid4(),
        organization=scope.organization,
        project=scope.project,
        observation=observation,
        raw_event=raw_event,
        source_type=source_type,
        source_id=source_id or f'source-{suffix}',
        metadata=metadata or {},
    )


def _make_typed_raw_event(
    scope: ScopeFixture,
    session: AgentSession,
    suffix: str,
    *,
    policy: dict[str, object] | None = None,
    disposition: str = 'observation',
    reason: str | None = None,
    entity_id: uuid.UUID | None = None,
    event_type: str = 'post_tool_use',
) -> RawEventEnvelope:
    raw_event = _make_raw_event(scope, session, suffix, entity_id=entity_id, event_type=event_type)
    raw_event.normalization_contract_version = 1
    raw_event.normalization_disposition = disposition
    raw_event.normalization_reason = reason
    raw_event.source_adapter = Runtime.CODEX
    if policy is not None:
        raw_event.metadata = {'work_policy_v1': policy}
    raw_event.save(
        update_fields=[
            'normalization_contract_version',
            'normalization_disposition',
            'normalization_reason',
            'source_adapter',
            'metadata',
            'updated_at',
        ],
    )
    return raw_event


def _make_observation_work(
    scope: ScopeFixture,
    observation: Observation,
    policy: dict[str, object],
) -> WorkflowWork:
    if not observation.source_metadata:
        observation.source_metadata = {'event_type': 'post_tool_use'}
        observation.save(update_fields=['source_metadata', 'updated_at'])

    work, _created = create_work(
        CreateWorkflowWorkInput(
            organization_id=scope.organization.id,
            project_id=scope.project.id,
            work_type=WorkflowWorkType.OBSERVATION_PROCESSING,
            subject_type=WorkflowSubjectType.OBSERVATION,
            subject_id=observation.id,
            input_snapshot={
                'schema': 'observation_processing_input/v1',
                'observation_id': str(observation.id),
                'observation_digest': observation_content_digest(observation),
                'policy': policy,
            },
        ),
    )
    return work


def _make_stored_observation_work(
    scope: ScopeFixture,
    observation: Observation,
    policy: dict[str, object],
) -> WorkflowWork:
    snapshot = {
        'schema': 'observation_processing_input/v1',
        'observation_id': str(observation.id),
        'observation_digest': observation_content_digest(observation),
        'policy': policy,
    }
    return WorkflowWork.objects.create(
        organization=scope.organization,
        project=scope.project,
        team=observation.team,
        work_type=WorkflowWorkType.OBSERVATION_PROCESSING,
        subject_type=WorkflowSubjectType.OBSERVATION,
        subject_id=observation.id,
        contract_version=1,
        occurrence_key='',
        input_fingerprint=work_input_fingerprint(
            work_type=WorkflowWorkType.OBSERVATION_PROCESSING,
            subject_type=WorkflowSubjectType.OBSERVATION,
            subject_id=observation.id,
            contract_version=1,
            occurrence_key='',
            input_snapshot=snapshot,
        ),
        input_snapshot=snapshot,
    )


def _make_candidate(
    scope: ScopeFixture,
    suffix: str,
    *,
    status: str = CandidateStatus.PROPOSED,
    entity_id: uuid.UUID | None = None,
) -> MemoryCandidate:
    return MemoryCandidate.objects.create(
        id=entity_id or uuid.uuid4(),
        organization=scope.organization,
        project=scope.project,
        team=scope.team,
        title=f'Synthetic candidate {suffix}',
        body=f'Synthetic candidate detail {suffix}',
        status=status,
        visibility_scope=VisibilityScope.PROJECT,
        content_hash=f'candidate-hash-{suffix}',
        confidence=Decimal('0.900'),
    )


def _make_memory(
    scope: ScopeFixture,
    suffix: str,
    *,
    entity_id: uuid.UUID | None = None,
    body: str | None = None,
    status: str = MemoryStatus.APPROVED,
    confidence: Decimal = Decimal('0.900'),
    stale: bool = False,
    refuted: bool = False,
    current_version: int = 1,
) -> Memory:
    return Memory.objects.create(
        id=entity_id or uuid.uuid4(),
        organization=scope.organization,
        project=scope.project,
        team=scope.team,
        title=f'Synthetic memory {suffix}',
        body=body or f'Synthetic memory detail {suffix}',
        status=status,
        visibility_scope=VisibilityScope.PROJECT,
        confidence=confidence,
        stale=stale,
        refuted=refuted,
        current_version=current_version,
    )


def _make_version(
    memory: Memory,
    suffix: str,
    *,
    body: str | None = None,
    entity_id: uuid.UUID | None = None,
) -> MemoryVersion:
    return MemoryVersion.objects.create(
        id=entity_id or uuid.uuid4(),
        organization=memory.organization,
        project=memory.project,
        memory=memory,
        version=memory.current_version,
        body=body if body is not None else memory.body,
        content_hash=f'version-hash-{suffix}',
    )


def _make_document(
    memory: Memory,
    version: MemoryVersion,
    *,
    stale: bool | None = None,
    refuted: bool | None = None,
    entity_id: uuid.UUID | None = None,
) -> RetrievalDocument:
    return RetrievalDocument.objects.create(
        id=entity_id or uuid.uuid4(),
        organization=memory.organization,
        project=memory.project,
        team=memory.team,
        memory=memory,
        memory_version=version,
        visibility_scope=memory.visibility_scope,
        full_text='Synthetic retrieval text',
        stale=memory.stale if stale is None else stale,
        refuted=memory.refuted if refuted is None else refuted,
    )


def _make_complete_memory(
    scope: ScopeFixture,
    suffix: str,
    *,
    entity_id: uuid.UUID | None = None,
    status: str = MemoryStatus.APPROVED,
    confidence: Decimal = Decimal('0.900'),
) -> tuple[Memory, MemoryVersion, RetrievalDocument]:
    memory = _make_memory(
        scope,
        suffix,
        entity_id=entity_id,
        status=status,
        confidence=confidence,
    )
    version = _make_version(
        memory,
        suffix,
        entity_id=_synthetic_id(f'{suffix}:version') if entity_id is not None else None,
    )
    document = _make_document(
        memory,
        version,
        entity_id=_synthetic_id(f'{suffix}:document') if entity_id is not None else None,
    )

    return memory, version, document


def _make_workflow_run(
    scope: ScopeFixture,
    *,
    status: str,
    session: AgentSession | None = None,
    started_at: datetime | None = None,
    entity_id: uuid.UUID | None = None,
    run_type: str = WorkflowRunType.SESSION_DISTILLATION,
) -> WorkflowRun:
    return WorkflowRun.objects.create(
        id=entity_id or uuid.uuid4(),
        organization=scope.organization,
        project=scope.project,
        team=scope.team,
        run_type=run_type,
        status=status,
        input_snapshot={'session_id': str(session.id)} if session is not None else {},
        started_at=started_at,
    )


def _result_by_id(
    scope: ScopeFixture,
    *,
    as_of: datetime = FIXED_AS_OF,
) -> dict[str, InvariantResult]:
    return {
        str(result.invariant_id): result
        for result in evaluate_invariants(
            organization_id=scope.organization.id,
            project_id=scope.project.id,
            as_of=as_of,
        )
    }


def _assert_characterization(result: InvariantResult, expected: dict[str, Any]) -> None:
    assert str(result.invariant_id) == expected['invariant_id']
    assert result.state == expected['state']
    assert result.reason == expected['reason']
    assert result.violation_count == expected['violation_count']
    assert result.proxy_count == expected['proxy_count']
    assert result.sample_ids == _expected_sample_ids(expected['sample_refs'])
    assert result.missing_evidence == expected['missing_evidence']
    assert result.target_checkpoint == expected['target_checkpoint']


@pytest.mark.django_db
def test_evaluation_fails_closed_for_mismatched_organization_and_project(
    f_scope: ScopeFixture,
    f_foreign_scope: ScopeFixture,
) -> None:
    with pytest.raises(Project.DoesNotExist):
        evaluate_invariants(
            organization_id=f_scope.organization.id,
            project_id=f_foreign_scope.project.id,
            as_of=FIXED_AS_OF,
        )


@pytest.mark.django_db
def test_evaluation_rejects_naive_as_of(f_scope: ScopeFixture) -> None:
    with pytest.raises(ValueError, match='timezone-aware'):
        evaluate_invariants(
            organization_id=f_scope.organization.id,
            project_id=f_scope.project.id,
            as_of=datetime(2026, 7, 10, 12),  # noqa: DTZ001 - intentionally naive contract input
        )


@pytest.mark.django_db
def test_evaluation_returns_p1_through_p15_in_order(f_scope: ScopeFixture) -> None:
    results = evaluate_invariants(
        organization_id=f_scope.organization.id,
        project_id=f_scope.project.id,
        as_of=FIXED_AS_OF,
    )

    assert tuple(result.invariant_id for result in results) == tuple(InvariantId)


@pytest.mark.django_db
def test_p1_is_healthy_without_raw_events_and_detects_missing_source(
    f_scope: ScopeFixture,
) -> None:
    healthy = _result_by_id(f_scope)['P1']

    assert healthy.state == InvariantState.HEALTHY
    assert healthy.reason == 'scoped_raw_events_normalized'
    assert healthy.violation_count == 0
    assert healthy.proxy_count is None
    assert healthy.sample_ids == ()
    assert healthy.target_checkpoint == 'CP1'

    session = _make_session(f_scope, 'p1-missing')
    raw_event = _make_raw_event(
        f_scope,
        session,
        'p1-missing',
        entity_id=uuid.UUID('00000000-0000-0000-0000-000000000011'),
    )

    violated = _result_by_id(f_scope)['P1']

    assert violated.state == InvariantState.VIOLATED
    assert violated.reason == 'raw_event_normalization_cardinality_invalid'
    assert violated.violation_count == 1
    assert violated.proxy_count is None
    assert violated.sample_ids == (f'raw_event:{raw_event.id}',)
    assert violated.missing_evidence is None
    assert violated.target_checkpoint == 'CP1'


@pytest.mark.django_db
def test_p1_requires_exactly_one_total_and_same_scope_source(
    f_scope: ScopeFixture,
    f_foreign_scope: ScopeFixture,
) -> None:
    target_session = _make_session(f_scope, 'p1-cardinality')
    duplicate_raw = _make_raw_event(
        f_scope,
        target_session,
        'p1-duplicate',
        entity_id=uuid.UUID('00000000-0000-0000-0000-000000000021'),
    )
    first = _make_observation(f_scope, target_session, 'p1-first')
    second = _make_observation(f_scope, target_session, 'p1-second')
    _make_source(f_scope, first, duplicate_raw, 'p1-first')
    _make_source(f_scope, second, duplicate_raw, 'p1-second')

    corrupt_raw = _make_raw_event(
        f_scope,
        target_session,
        'p1-corrupt',
        entity_id=uuid.UUID('00000000-0000-0000-0000-000000000022'),
    )
    target_observation = _make_observation(f_scope, target_session, 'p1-corrupt-target')
    source = _make_source(f_scope, target_observation, corrupt_raw, 'p1-corrupt')
    foreign_session = _make_session(f_foreign_scope, 'p1-corrupt-foreign')
    foreign_observation = _make_observation(
        f_foreign_scope,
        foreign_session,
        'p1-corrupt-foreign',
    )
    ObservationSource.objects.filter(id=source.id).update(observation_id=foreign_observation.id)

    result = _result_by_id(f_scope)['P1']

    assert result.violation_count == 2
    assert result.sample_ids == (
        f'raw_event:{duplicate_raw.id}',
        f'raw_event:{corrupt_raw.id}',
    )


@pytest.mark.django_db
def test_p1_foreign_project_anomaly_is_neither_counted_nor_sampled(
    f_scope: ScopeFixture,
    f_foreign_scope: ScopeFixture,
) -> None:
    target_session = _make_session(f_scope, 'p1-target')
    target_raw = _make_raw_event(
        f_scope,
        target_session,
        'p1-target',
        entity_id=uuid.UUID('00000000-0000-0000-0000-000000000031'),
    )
    foreign_session = _make_session(f_foreign_scope, 'p1-foreign')
    foreign_raw = _make_raw_event(
        f_foreign_scope,
        foreign_session,
        'p1-foreign',
        entity_id=uuid.UUID('00000000-0000-0000-0000-000000000001'),
    )

    result = _result_by_id(f_scope)['P1']

    assert result.violation_count == 1
    assert result.sample_ids == (f'raw_event:{target_raw.id}',)
    assert f'raw_event:{foreign_raw.id}' not in result.sample_ids


@pytest.mark.django_db
def test_post_cutover_p1_rejects_cross_team_observation_source(
    f_scope: ScopeFixture,
) -> None:
    session = _make_session(f_scope, 'typed-cross-team')
    raw_event = _make_typed_raw_event(f_scope, session, 'typed-cross-team')
    observation = _make_observation(f_scope, session, 'typed-cross-team')
    _make_source(f_scope, observation, raw_event, 'typed-cross-team')
    other_team = Team.objects.create(
        organization=f_scope.organization,
        name='Other team',
        slug='other-team',
    )
    Observation.objects.filter(id=observation.id).update(team_id=other_team.id)

    typed_p1, _typed_p2 = evaluate_post_cutover_p1_p2(
        organization_id=f_scope.organization.id,
        project_id=f_scope.project.id,
    )

    assert typed_p1.state == InvariantState.VIOLATED
    assert typed_p1.violation_count == 1
    assert typed_p1.sample_ids == (f'raw_event:{raw_event.id}',)


@pytest.mark.django_db
def test_post_cutover_p1_team_equality_is_null_safe(
    f_scope: ScopeFixture,
) -> None:
    session = _make_session(f_scope, 'typed-null-team')
    AgentSession.objects.filter(id=session.id).update(team_id=None)
    raw_event = _make_typed_raw_event(f_scope, session, 'typed-null-team')
    RawEventEnvelope.objects.filter(id=raw_event.id).update(team_id=None)
    observation = _make_observation(f_scope, session, 'typed-null-team')
    Observation.objects.filter(id=observation.id).update(team_id=None)
    _make_source(f_scope, observation, raw_event, 'typed-null-team')

    typed_p1, _typed_p2 = evaluate_post_cutover_p1_p2(
        organization_id=f_scope.organization.id,
        project_id=f_scope.project.id,
    )

    assert typed_p1.state == InvariantState.HEALTHY
    assert typed_p1.violation_count == 0


@pytest.mark.django_db(transaction=True)
def test_post_cutover_p1_observation_requires_no_normalization_reason(
    f_scope: ScopeFixture,
) -> None:
    session = _make_session(f_scope, 'typed-observation-reason')
    raw_event = _make_typed_raw_event(f_scope, session, 'typed-observation-reason')
    observation = _make_observation(f_scope, session, 'typed-observation-reason')
    _make_source(f_scope, observation, raw_event, 'typed-observation-reason')

    constraint = next(
        constraint
        for constraint in RawEventEnvelope._meta.constraints
        if constraint.name == 'core_raw_norm_final_valid'
    )
    with connection.schema_editor() as schema_editor:
        schema_editor.remove_constraint(RawEventEnvelope, constraint)
    try:
        RawEventEnvelope.objects.filter(id=raw_event.id).update(
            normalization_reason='evidence_only',
        )

        typed_p1, _typed_p2 = evaluate_post_cutover_p1_p2(
            organization_id=f_scope.organization.id,
            project_id=f_scope.project.id,
        )

        assert typed_p1.state == InvariantState.VIOLATED
        assert typed_p1.violation_count == 1
        assert typed_p1.sample_ids == (f'raw_event:{raw_event.id}',)
    finally:
        try:
            RawEventEnvelope.objects.filter(id=raw_event.id).update(
                normalization_contract_version=1,
                normalization_disposition='observation',
                normalization_reason=None,
            )
        finally:
            with connection.schema_editor() as schema_editor:
                schema_editor.add_constraint(RawEventEnvelope, constraint)


@pytest.mark.django_db
def test_post_cutover_p1_is_typed_while_global_p1_retains_legacy_gap(
    f_scope: ScopeFixture,
) -> None:
    legacy_session = _make_session(f_scope, 'typed-legacy')
    legacy_raw = _make_raw_event(f_scope, legacy_session, 'typed-legacy')
    typed_session = _make_session(f_scope, 'typed-healthy')
    typed_raw = _make_typed_raw_event(f_scope, typed_session, 'typed-healthy')
    typed_observation = _make_observation(f_scope, typed_session, 'typed-healthy')
    _make_source(f_scope, typed_observation, typed_raw, 'typed-healthy')

    typed_p1, _typed_p2 = evaluate_post_cutover_p1_p2(
        organization_id=f_scope.organization.id,
        project_id=f_scope.project.id,
    )
    global_p1 = _result_by_id(f_scope)['P1']
    global_p2 = _result_by_id(f_scope)['P2']

    assert typed_p1.state == InvariantState.HEALTHY
    assert typed_p1.violation_count == 0
    assert typed_p1.sample_ids == ()
    assert global_p1.state == InvariantState.VIOLATED
    assert global_p1.violation_count == 1
    assert global_p1.sample_ids == (f'raw_event:{legacy_raw.id}',)
    assert global_p2.state == InvariantState.MISSING_OBSERVABILITY


@pytest.mark.django_db
def test_post_cutover_p1_requires_observation_source_and_noop_has_none(
    f_scope: ScopeFixture,
) -> None:
    session = _make_session(f_scope, 'typed-cardinality')
    missing_source = _make_typed_raw_event(f_scope, session, 'typed-missing-source')
    no_op = _make_typed_raw_event(
        f_scope,
        session,
        'typed-no-op',
        disposition='no_op',
        reason='evidence_only',
    )
    observation = _make_observation(f_scope, session, 'typed-no-op')
    _make_source(f_scope, observation, no_op, 'typed-no-op')

    typed_p1, _typed_p2 = evaluate_post_cutover_p1_p2(
        organization_id=f_scope.organization.id,
        project_id=f_scope.project.id,
    )

    assert typed_p1.state == InvariantState.VIOLATED
    assert typed_p1.violation_count == 2
    assert typed_p1.sample_ids == tuple(
        sorted(
            (f'raw_event:{missing_source.id}', f'raw_event:{no_op.id}'),
            key=lambda sample_id: uuid.UUID(sample_id.split(':', maxsplit=1)[1]).int,
        ),
    )


@pytest.mark.django_db
def test_post_cutover_p1_rejects_cross_team_evidence_only_noop(
    f_scope: ScopeFixture,
) -> None:
    session = _make_session(f_scope, 'typed-no-op-cross-team')
    raw_event = _make_typed_raw_event(
        f_scope,
        session,
        'typed-no-op-cross-team',
        disposition='no_op',
        reason='evidence_only',
    )
    other_team = Team.objects.create(
        organization=f_scope.organization,
        name='No-op other team',
        slug='no-op-other-team',
    )
    AgentSession.objects.filter(id=session.id).update(team_id=other_team.id)

    typed_p1, _typed_p2 = evaluate_post_cutover_p1_p2(
        organization_id=f_scope.organization.id,
        project_id=f_scope.project.id,
    )

    assert typed_p1.state == InvariantState.VIOLATED
    assert typed_p1.violation_count == 1
    assert typed_p1.sample_ids == (f'raw_event:{raw_event.id}',)


@pytest.mark.django_db
def test_post_cutover_p1_evidence_only_noop_team_equality_is_null_safe(
    f_scope: ScopeFixture,
) -> None:
    session = _make_session(f_scope, 'typed-no-op-null-team')
    AgentSession.objects.filter(id=session.id).update(team_id=None)
    raw_event = _make_typed_raw_event(
        f_scope,
        session,
        'typed-no-op-null-team',
        disposition='no_op',
        reason='evidence_only',
    )
    RawEventEnvelope.objects.filter(id=raw_event.id).update(team_id=None)

    typed_p1, _typed_p2 = evaluate_post_cutover_p1_p2(
        organization_id=f_scope.organization.id,
        project_id=f_scope.project.id,
    )

    assert typed_p1.state == InvariantState.HEALTHY
    assert typed_p1.violation_count == 0


@pytest.mark.django_db
@pytest.mark.parametrize('scope_mismatch', ('organization', 'project'))
def test_post_cutover_p1_and_p2_reject_cross_scope_session_linkage(
    scope_mismatch: str,
    f_scope: ScopeFixture,
    f_foreign_scope: ScopeFixture,
) -> None:
    policy = {
        'schema': 'hook_work_policy/v1',
        'realtime_candidates_enabled': True,
        'legacy_policy_fallback': False,
    }
    hook_session = _make_session(f_scope, f'typed-cross-{scope_mismatch}-hook')
    hook_raw = _make_typed_raw_event(
        f_scope,
        hook_session,
        f'typed-cross-{scope_mismatch}-hook',
        policy=policy,
    )
    hook_observation = _make_observation(f_scope, hook_session, f'typed-cross-{scope_mismatch}-hook')
    _make_source(
        f_scope,
        hook_observation,
        hook_raw,
        f'typed-cross-{scope_mismatch}-hook',
        source_type='hook_event',
        source_id=hook_raw.client_event_id,
        metadata={'event_type': hook_raw.event_type},
    )
    _make_observation_work(f_scope, hook_observation, policy)

    if scope_mismatch == 'organization':
        AgentSession.objects.filter(id=hook_session.id).update(
            organization_id=f_foreign_scope.organization.id,
        )
    else:
        other_project = Project.objects.create(
            organization=f_scope.organization,
            name='Other target-organization project',
            slug='other-target-organization-project',
        )
        AgentSession.objects.filter(id=hook_session.id).update(project_id=other_project.id)

    typed_p1, typed_p2 = evaluate_post_cutover_p1_p2(
        organization_id=f_scope.organization.id,
        project_id=f_scope.project.id,
    )

    assert typed_p1.state == InvariantState.VIOLATED
    assert typed_p1.violation_count == 1
    assert typed_p1.sample_ids == (f'raw_event:{hook_raw.id}',)
    assert typed_p2.state == InvariantState.VIOLATED
    assert typed_p2.violation_count == 1
    assert typed_p2.sample_ids == (f'raw_event:{hook_raw.id}',)


@pytest.mark.django_db
def test_post_cutover_p2_requires_policy_for_every_typed_hook_raw(
    f_scope: ScopeFixture,
) -> None:
    regular_session = _make_session(f_scope, 'typed-missing-policy')
    regular_raw = _make_typed_raw_event(f_scope, regular_session, 'typed-missing-policy')
    lifecycle_session = _make_session(f_scope, 'typed-lifecycle-missing-policy')
    lifecycle_raw = _make_typed_raw_event(
        f_scope,
        lifecycle_session,
        'typed-lifecycle-missing-policy',
        event_type='session_start',
    )

    _typed_p1, typed_p2 = evaluate_post_cutover_p1_p2(
        organization_id=f_scope.organization.id,
        project_id=f_scope.project.id,
    )

    assert typed_p2.state == InvariantState.VIOLATED
    assert typed_p2.violation_count == 2
    assert set(typed_p2.sample_ids) == {
        f'raw_event:{regular_raw.id}',
        f'raw_event:{lifecycle_raw.id}',
    }


@pytest.mark.django_db
def test_post_cutover_p2_excludes_typed_claude_mem_imports_without_hook_policy(
    f_scope: ScopeFixture,
) -> None:
    observation_session = _make_session(f_scope, 'typed-import-observation')
    observation_raw = _make_typed_raw_event(
        f_scope,
        observation_session,
        'typed-import-observation',
    )
    observation = _make_observation(f_scope, observation_session, 'typed-import-observation')
    _make_source(f_scope, observation, observation_raw, 'typed-import-observation')
    RawEventEnvelope.objects.filter(id=observation_raw.id).update(source_adapter='claude_mem')

    no_op_session = _make_session(f_scope, 'typed-import-no-op')
    no_op_raw = _make_typed_raw_event(
        f_scope,
        no_op_session,
        'typed-import-no-op',
        disposition='no_op',
        reason='evidence_only',
    )
    RawEventEnvelope.objects.filter(id=no_op_raw.id).update(source_adapter='claude_mem')

    typed_p1, typed_p2 = evaluate_post_cutover_p1_p2(
        organization_id=f_scope.organization.id,
        project_id=f_scope.project.id,
    )

    assert typed_p1.state == InvariantState.HEALTHY
    assert typed_p2.state == InvariantState.HEALTHY
    assert typed_p2.violation_count == 0


@pytest.mark.django_db
def test_post_cutover_p2_requires_matching_observation_work_and_rejects_malformed_policy(
    f_scope: ScopeFixture,
) -> None:
    policy = {
        'schema': 'hook_work_policy/v1',
        'realtime_candidates_enabled': True,
        'legacy_policy_fallback': False,
    }
    session = _make_session(f_scope, 'typed-work')
    raw_event = _make_typed_raw_event(f_scope, session, 'typed-work', policy=policy)
    observation = _make_observation(f_scope, session, 'typed-work')
    _make_source(
        f_scope,
        observation,
        raw_event,
        'typed-work',
        source_type='hook_event',
        source_id=raw_event.client_event_id,
        metadata={'event_type': raw_event.event_type},
    )
    _make_observation_work(f_scope, observation, policy)

    _typed_p1, typed_p2 = evaluate_post_cutover_p1_p2(
        organization_id=f_scope.organization.id,
        project_id=f_scope.project.id,
    )
    assert typed_p2.state == InvariantState.HEALTHY
    assert typed_p2.violation_count == 0

    WorkflowWork.objects.all().delete()
    typed_p2 = evaluate_post_cutover_p1_p2(
        organization_id=f_scope.organization.id,
        project_id=f_scope.project.id,
    )[1]
    assert typed_p2.state == InvariantState.VIOLATED
    assert typed_p2.violation_count == 1
    assert typed_p2.sample_ids == (f'raw_event:{raw_event.id}',)


@pytest.mark.django_db
def test_post_cutover_p2_requires_false_legacy_policy_fallback(
    f_scope: ScopeFixture,
) -> None:
    policy = {
        'schema': 'hook_work_policy/v1',
        'realtime_candidates_enabled': True,
        'legacy_policy_fallback': True,
    }
    session = _make_session(f_scope, 'typed-fallback')
    raw_event = _make_typed_raw_event(f_scope, session, 'typed-fallback', policy=policy)
    observation = _make_observation(f_scope, session, 'typed-fallback')
    observation.source_metadata = {'event_type': raw_event.event_type}
    observation.save(update_fields=['source_metadata', 'updated_at'])
    _make_source(
        f_scope,
        observation,
        raw_event,
        'typed-fallback',
        source_type='hook_event',
        source_id=raw_event.client_event_id,
        metadata={'event_type': raw_event.event_type},
    )

    _typed_p1, typed_p2 = evaluate_post_cutover_p1_p2(
        organization_id=f_scope.organization.id,
        project_id=f_scope.project.id,
    )

    assert typed_p2.state == InvariantState.VIOLATED
    assert typed_p2.violation_count == 1
    assert typed_p2.sample_ids == (f'raw_event:{raw_event.id}',)


@pytest.mark.django_db
def test_post_cutover_p2_rejects_noncanonical_hook_source_before_work_check(
    f_scope: ScopeFixture,
) -> None:
    policy = {
        'schema': 'hook_work_policy/v1',
        'realtime_candidates_enabled': True,
        'legacy_policy_fallback': False,
    }
    session = _make_session(f_scope, 'typed-relation')
    raw_event = _make_typed_raw_event(f_scope, session, 'typed-relation', policy=policy)
    observation = _make_observation(f_scope, session, 'typed-relation')
    observation.source_metadata = {'event_type': raw_event.event_type}
    observation.save(update_fields=['source_metadata', 'updated_at'])
    source = _make_source(
        f_scope,
        observation,
        raw_event,
        'typed-relation',
        source_type='hook_event',
        source_id=raw_event.client_event_id,
        metadata={'event_type': raw_event.event_type},
    )
    _make_observation_work(f_scope, observation, policy)
    ObservationSource.objects.filter(id=source.id).update(source_id='wrong-client-event')

    _typed_p1, typed_p2 = evaluate_post_cutover_p1_p2(
        organization_id=f_scope.organization.id,
        project_id=f_scope.project.id,
    )

    assert typed_p2.state == InvariantState.VIOLATED
    assert typed_p2.violation_count == 1
    assert typed_p2.sample_ids == (f'raw_event:{raw_event.id}',)


@pytest.mark.django_db
def test_post_cutover_p2_matches_stored_policy_semantics_not_fallback_provenance(
    f_scope: ScopeFixture,
) -> None:
    current_policy = {
        'schema': 'hook_work_policy/v1',
        'realtime_candidates_enabled': True,
        'legacy_policy_fallback': False,
    }
    stored_policy = {**current_policy, 'legacy_policy_fallback': True}
    session = _make_session(f_scope, 'typed-provenance')
    raw_event = _make_typed_raw_event(f_scope, session, 'typed-provenance', policy=current_policy)
    observation = _make_observation(f_scope, session, 'typed-provenance')
    observation.source_metadata = {'event_type': raw_event.event_type}
    observation.save(update_fields=['source_metadata', 'updated_at'])
    _make_source(
        f_scope,
        observation,
        raw_event,
        'typed-provenance',
        source_type='hook_event',
        source_id=raw_event.client_event_id,
        metadata={'event_type': raw_event.event_type},
    )
    _make_observation_work(f_scope, observation, stored_policy)

    _typed_p1, typed_p2 = evaluate_post_cutover_p1_p2(
        organization_id=f_scope.organization.id,
        project_id=f_scope.project.id,
    )

    assert typed_p2.state == InvariantState.HEALTHY
    assert typed_p2.violation_count == 0

    WorkflowWork.objects.all().delete()
    _make_stored_observation_work(
        f_scope,
        observation,
        {**current_policy, 'realtime_candidates_enabled': False},
    )
    typed_p2 = evaluate_post_cutover_p1_p2(
        organization_id=f_scope.organization.id,
        project_id=f_scope.project.id,
    )[1]
    assert typed_p2.state == InvariantState.VIOLATED
    assert typed_p2.violation_count == 1
    assert typed_p2.sample_ids == (f'raw_event:{raw_event.id}',)

    WorkflowWork.objects.all().delete()
    work = _make_observation_work(f_scope, observation, current_policy)
    WorkflowWork.objects.filter(id=work.id).update(
        input_snapshot={
            **work.input_snapshot,
            'policy': {'schema': 'hook_work_policy/v1'},
        },
    )
    typed_p2 = evaluate_post_cutover_p1_p2(
        organization_id=f_scope.organization.id,
        project_id=f_scope.project.id,
    )[1]
    assert typed_p2.state == InvariantState.VIOLATED
    assert typed_p2.violation_count == 1
    assert typed_p2.sample_ids == (f'raw_event:{raw_event.id}',)


@pytest.mark.django_db
def test_post_cutover_p2_allows_additional_valid_semantic_policy_work(
    f_scope: ScopeFixture,
) -> None:
    required_policy = {
        'schema': 'hook_work_policy/v1',
        'realtime_candidates_enabled': True,
        'legacy_policy_fallback': False,
    }
    other_policy = {**required_policy, 'realtime_candidates_enabled': False}
    session = _make_session(f_scope, 'typed-additional-policy')
    raw_event = _make_typed_raw_event(
        f_scope,
        session,
        'typed-additional-policy',
        policy=required_policy,
    )
    observation = _make_observation(f_scope, session, 'typed-additional-policy')
    observation.source_metadata = {'event_type': raw_event.event_type}
    observation.save(update_fields=['source_metadata', 'updated_at'])
    _make_source(
        f_scope,
        observation,
        raw_event,
        'typed-additional-policy',
        source_type='hook_event',
        source_id=raw_event.client_event_id,
        metadata={'event_type': raw_event.event_type},
    )
    _make_observation_work(f_scope, observation, required_policy)
    _make_stored_observation_work(f_scope, observation, other_policy)

    _typed_p1, typed_p2 = evaluate_post_cutover_p1_p2(
        organization_id=f_scope.organization.id,
        project_id=f_scope.project.id,
    )

    assert WorkflowWork.objects.filter(subject_id=observation.id).count() == 2
    assert typed_p2.state == InvariantState.HEALTHY
    assert typed_p2.violation_count == 0


@pytest.mark.django_db
def test_post_cutover_p2_disabled_and_lifecycle_rows_ignore_preexisting_work(
    f_scope: ScopeFixture,
) -> None:
    disabled_policy = {
        'schema': 'hook_work_policy/v1',
        'realtime_candidates_enabled': False,
        'legacy_policy_fallback': False,
    }
    disabled_session = _make_session(f_scope, 'typed-disabled')
    disabled_raw = _make_typed_raw_event(
        f_scope,
        disabled_session,
        'typed-disabled',
        policy=disabled_policy,
    )
    disabled_observation = _make_observation(f_scope, disabled_session, 'typed-disabled')
    disabled_observation.source_metadata = {'event_type': disabled_raw.event_type}
    disabled_observation.save(update_fields=['source_metadata', 'updated_at'])
    _make_source(
        f_scope,
        disabled_observation,
        disabled_raw,
        'typed-disabled',
        source_type='hook_event',
        source_id=disabled_raw.client_event_id,
        metadata={'event_type': disabled_raw.event_type},
    )

    lifecycle_policy = {**disabled_policy, 'realtime_candidates_enabled': True}
    _make_observation_work(f_scope, disabled_observation, lifecycle_policy)
    lifecycle_session = _make_session(f_scope, 'typed-lifecycle')
    lifecycle_raw = _make_typed_raw_event(
        f_scope,
        lifecycle_session,
        'typed-lifecycle',
        policy=lifecycle_policy,
        event_type='session_start',
    )
    lifecycle_observation = _make_observation(
        f_scope,
        lifecycle_session,
        'typed-lifecycle',
        observation_type='session_start',
    )
    lifecycle_observation.source_metadata = {'event_type': 'session_start'}
    lifecycle_observation.save(update_fields=['source_metadata', 'updated_at'])
    _make_source(
        f_scope,
        lifecycle_observation,
        lifecycle_raw,
        'typed-lifecycle',
        source_type='hook_event',
        source_id=lifecycle_raw.client_event_id,
        metadata={'event_type': lifecycle_raw.event_type},
    )
    _make_stored_observation_work(f_scope, lifecycle_observation, disabled_policy)

    _typed_p1, typed_p2 = evaluate_post_cutover_p1_p2(
        organization_id=f_scope.organization.id,
        project_id=f_scope.project.id,
    )

    assert typed_p2.state == InvariantState.HEALTHY
    assert typed_p2.violation_count == 0


@pytest.mark.django_db
def test_post_cutover_p2_query_count_is_bounded_for_multiple_rows(
    f_scope: ScopeFixture,
) -> None:
    policy = {
        'schema': 'hook_work_policy/v1',
        'realtime_candidates_enabled': True,
        'legacy_policy_fallback': False,
    }
    for index in range(25):
        session = _make_session(f_scope, f'typed-query-{index}')
        raw_event = _make_typed_raw_event(f_scope, session, f'typed-query-{index}', policy=policy)
        observation = _make_observation(f_scope, session, f'typed-query-{index}')
        observation.source_metadata = {'event_type': raw_event.event_type}
        observation.save(update_fields=['source_metadata', 'updated_at'])
        _make_source(
            f_scope,
            observation,
            raw_event,
            f'typed-query-{index}',
            source_type='hook_event',
            source_id=raw_event.client_event_id,
            metadata={'event_type': raw_event.event_type},
        )

    with CaptureQueriesContext(connection) as captured:
        _typed_p1, typed_p2 = evaluate_post_cutover_p1_p2(
            organization_id=f_scope.organization.id,
            project_id=f_scope.project.id,
        )

    assert typed_p2.state == InvariantState.VIOLATED
    assert typed_p2.violation_count == 25
    assert len(captured) <= 12


@pytest.mark.django_db
def test_p3_requires_non_lifecycle_input_and_correlates_json_session_id(
    f_scope: ScopeFixture,
) -> None:
    lifecycle_session = _make_session(f_scope, 'p3-lifecycle')
    _make_observation(
        f_scope,
        lifecycle_session,
        'p3-lifecycle',
        observation_type='session_start',
    )
    uncovered_session = _make_session(f_scope, 'p3-uncovered')
    _make_observation(f_scope, uncovered_session, 'p3-uncovered')
    covered_session = _make_session(f_scope, 'p3-covered')
    _make_observation(f_scope, covered_session, 'p3-covered')
    _make_workflow_run(
        f_scope,
        status=WorkflowRunStatus.SUCCEEDED,
        session=covered_session,
    )

    result = _result_by_id(f_scope)['P3']

    assert result.proxy_count == 1
    assert result.sample_ids == (f'session:{uncovered_session.id}',)

    _make_workflow_run(
        f_scope,
        status=WorkflowRunStatus.SUCCEEDED,
        session=uncovered_session,
    )

    result = _result_by_id(f_scope)['P3']

    assert result.proxy_count == 0
    assert result.sample_ids == ()


@pytest.mark.django_db
def test_p4_coalesces_started_at_over_created_at(f_scope: ScopeFixture) -> None:
    fallback_run = _make_workflow_run(f_scope, status=WorkflowRunStatus.RUNNING)
    recent_started_run = _make_workflow_run(
        f_scope,
        status=WorkflowRunStatus.RUNNING,
        started_at=FIXED_AS_OF - timedelta(minutes=1),
    )
    WorkflowRun.objects.filter(id__in=(fallback_run.id, recent_started_run.id)).update(
        created_at=FIXED_AS_OF - timedelta(minutes=31),
    )

    result = _result_by_id(f_scope)['P4']

    assert result.proxy_count == 1
    assert result.sample_ids == (f'workflow_run:{fallback_run.id}',)


@pytest.mark.django_db
def test_p7_sums_guarded_promotion_version_body_and_document_anomalies(
    f_scope: ScopeFixture,
    f_foreign_scope: ScopeFixture,
) -> None:
    orphan_candidate = _make_candidate(
        f_scope,
        'p7-orphan',
        status=CandidateStatus.PROMOTED,
        entity_id=uuid.UUID(int=101),
    )
    missing_version = _make_memory(
        f_scope,
        'p7-missing-version',
        entity_id=uuid.UUID(int=102),
    )
    mismatched_body = _make_memory(
        f_scope,
        'p7-body-mismatch',
        entity_id=uuid.UUID(int=103),
    )
    _make_version(mismatched_body, 'p7-body-mismatch', body='Different synthetic detail')

    wrong_scope_document_memory, _version, wrong_scope_document = _make_complete_memory(
        f_scope,
        'p7-wrong-document-scope',
        entity_id=uuid.UUID(int=104),
    )
    RetrievalDocument.objects.filter(id=wrong_scope_document.id).update(
        organization_id=f_foreign_scope.organization.id,
        project_id=f_foreign_scope.project.id,
    )

    stale_refuted_memory = _make_memory(
        f_scope,
        'p7-stale-refuted',
        entity_id=uuid.UUID(int=105),
        stale=True,
        refuted=False,
    )
    stale_refuted_version = _make_version(stale_refuted_memory, 'p7-stale-refuted')
    _make_document(
        stale_refuted_memory,
        stale_refuted_version,
        stale=False,
        refuted=True,
    )
    _make_complete_memory(
        f_scope,
        'p7-coherent',
        entity_id=uuid.UUID(int=106),
    )

    result = _result_by_id(f_scope)['P7']

    assert result.state == InvariantState.VIOLATED
    assert result.reason == 'promotion_chain_inconsistent'
    assert result.violation_count == 6
    assert result.proxy_count is None
    assert result.sample_ids == (
        f'candidate:{orphan_candidate.id}',
        f'memory:{missing_version.id}',
        f'memory:{mismatched_body.id}',
        f'memory:{wrong_scope_document_memory.id}',
        f'memory:{stale_refuted_memory.id}',
    )
    assert result.missing_evidence == 'relational promotion provenance and transition audit identity'
    assert result.target_checkpoint == 'CP4'


@pytest.mark.django_db
def test_p7_ignores_foreign_scope_anomalies(
    f_scope: ScopeFixture,
    f_foreign_scope: ScopeFixture,
) -> None:
    _make_candidate(
        f_foreign_scope,
        'p7-foreign-orphan',
        status=CandidateStatus.PROMOTED,
    )
    _make_memory(f_foreign_scope, 'p7-foreign-missing-version')

    result = _result_by_id(f_scope)['P7']

    assert result.state == InvariantState.MISSING_OBSERVABILITY
    assert result.reason == 'promotion_provenance_audit_relation_missing'
    assert result.violation_count == 0
    assert result.proxy_count is None
    assert result.sample_ids == ()
    assert result.missing_evidence == 'relational promotion provenance and transition audit identity'
    assert result.target_checkpoint == 'CP4'


@pytest.mark.django_db
def test_p12_mirrors_review_population_but_excludes_genuine_conflicts(
    f_scope: ScopeFixture,
) -> None:
    ordinary_candidate = _make_candidate(
        f_scope,
        'p12-ordinary',
        entity_id=uuid.UUID(int=201),
    )
    conflict_candidate = _make_candidate(
        f_scope,
        'p12-conflict-candidate',
        entity_id=uuid.UUID(int=202),
    )
    conflict_source, _version, _document = _make_complete_memory(
        f_scope,
        'p12-conflict-source',
        entity_id=uuid.UUID(int=203),
    )
    MemoryLink.objects.create(
        organization=f_scope.organization,
        project=f_scope.project,
        memory=conflict_source,
        link_type=LinkType.CONFLICTS_WITH,
        target=conflict_candidate_target(conflict_candidate.id),
    )
    low_confidence = _make_memory(
        f_scope,
        'p12-low-confidence',
        entity_id=uuid.UUID(int=204),
        confidence=Decimal('0.300'),
    )
    refuted_flag = _make_memory(
        f_scope,
        'p12-refuted-flag',
        entity_id=uuid.UUID(int=205),
        refuted=True,
    )
    refuted_status = _make_memory(
        f_scope,
        'p12-refuted-status',
        entity_id=uuid.UUID(int=206),
        status=MemoryStatus.REFUTED,
    )
    _make_memory(
        f_scope,
        'p12-conflict-memory',
        entity_id=uuid.UUID(int=207),
        status=MemoryStatus.CONFLICT,
        confidence=Decimal('0.100'),
        refuted=True,
    )

    result = _result_by_id(f_scope)['P12']

    assert result.state == InvariantState.VIOLATED
    assert result.reason == 'non_conflict_item_in_human_inbox'
    assert result.violation_count == 4
    assert result.proxy_count is None
    assert result.sample_ids == (
        f'candidate:{ordinary_candidate.id}',
        f'memory:{low_confidence.id}',
        f'memory:{refuted_flag.id}',
        f'memory:{refuted_status.id}',
    )
    assert f'candidate:{conflict_candidate.id}' not in result.sample_ids
    assert result.missing_evidence is None
    assert result.target_checkpoint == 'CP5'


@pytest.mark.django_db
def test_p12_requires_scoped_link_and_same_scope_origin_memory(
    f_scope: ScopeFixture,
    f_foreign_scope: ScopeFixture,
) -> None:
    target_candidate = _make_candidate(
        f_scope,
        'p12-target',
        entity_id=uuid.UUID(int=211),
    )
    foreign_origin, _version, _document = _make_complete_memory(
        f_foreign_scope,
        'p12-foreign-origin',
        entity_id=uuid.UUID(int=212),
    )
    corrupt_link = MemoryLink.objects.create(
        organization=f_foreign_scope.organization,
        project=f_foreign_scope.project,
        memory=foreign_origin,
        link_type=LinkType.CONFLICTS_WITH,
        target=conflict_candidate_target(target_candidate.id),
    )
    MemoryLink.objects.filter(id=corrupt_link.id).update(
        organization_id=f_scope.organization.id,
        project_id=f_scope.project.id,
    )
    _make_candidate(f_foreign_scope, 'p12-foreign-candidate', entity_id=uuid.UUID(int=1))
    _make_memory(
        f_foreign_scope,
        'p12-foreign-low-memory',
        entity_id=uuid.UUID(int=2),
        confidence=Decimal('0.100'),
    )

    result = _result_by_id(f_scope)['P12']

    assert result.violation_count == 1
    assert result.sample_ids == (f'candidate:{target_candidate.id}',)


@pytest.mark.django_db
def test_p12_samples_are_bounded_and_ordered_by_uuid_then_prefix(
    f_scope: ScopeFixture,
) -> None:
    expected_samples: list[str] = []

    for number in range(300, 311):
        entity_id = uuid.UUID(int=number)
        candidate = _make_candidate(f_scope, f'p12-sample-candidate-{number}', entity_id=entity_id)
        memory = _make_memory(
            f_scope,
            f'p12-sample-memory-{number}',
            entity_id=entity_id,
            confidence=Decimal('0.100'),
        )
        expected_samples.extend((f'candidate:{candidate.id}', f'memory:{memory.id}'))

    result = _result_by_id(f_scope)['P12']

    assert result.violation_count == 22
    assert result.sample_ids == tuple(expected_samples[:20])


@pytest.mark.django_db
def test_p6_samples_are_bounded_and_ordered(f_scope: ScopeFixture) -> None:
    candidates = [
        _make_candidate(
            f_scope,
            f'p6-sample-{number}',
            entity_id=uuid.UUID(int=number),
        )
        for number in reversed(range(400, 425))
    ]
    expected = tuple(
        f'candidate:{candidate.id}' for candidate in sorted(candidates, key=lambda candidate: candidate.id.int)[:20]
    )

    result = _result_by_id(f_scope)['P6']

    assert result.proxy_count == 25
    assert result.sample_ids == expected


@pytest.mark.django_db
def test_missing_observability_catalog_is_exact(f_scope: ScopeFixture) -> None:
    expected = {
        'P2': (
            'logical_work_intent_relation_missing',
            'durable logical-work-intent relation tied to the source transition',
            'CP1',
        ),
        'P5': (
            'observation_coverage_relation_missing',
            'observation-to-window disposition coverage relation',
            'CP3',
        ),
        'P8': (
            'memory_transition_history_relation_missing',
            'immutable transition history and authoritative current pointer',
            'CP4',
        ),
        'P9': (
            'durable_conflict_evidence_relation_missing',
            'conflict evidence surviving cleanup and restart',
            'CP4/CP5',
        ),
        'P10': (
            'replay_evidence_fields_missing',
            'replay fingerprint, byte hash, authorization, and budget evidence',
            'CP6',
        ),
        'P11': (
            'temporal_eligibility_evidence_missing',
            'retrieval-time temporal eligibility evidence',
            'CP8',
        ),
        'P13': (
            'repair_run_relation_missing',
            'repair identity, progress, idempotency, and dry-run explanation',
            'CP2/CP10',
        ),
        'P14': (
            'operation_scope_resolution_evidence_missing',
            'operation-to-resolved organization/project/team evidence',
            'CP1+',
        ),
        'P15': (
            'repository_impact_coverage_relation_missing',
            'memory revision and impact-coverage revision relation',
            'CP8',
        ),
    }
    results = _result_by_id(f_scope)

    for invariant_id, (reason, missing_evidence, target_checkpoint) in expected.items():
        result = results[invariant_id]

        assert result.state == InvariantState.MISSING_OBSERVABILITY
        assert result.reason == reason
        assert result.violation_count is None
        assert result.proxy_count is None
        assert result.sample_ids == ()
        assert result.missing_evidence == missing_evidence
        assert result.target_checkpoint == target_checkpoint


@pytest.mark.django_db
def test_zero_proxies_remain_missing_observability(f_scope: ScopeFixture) -> None:
    results = _result_by_id(f_scope)

    for invariant_id in ('P3', 'P4', 'P6'):
        result = results[invariant_id]

        assert result.state == InvariantState.MISSING_OBSERVABILITY
        assert result.proxy_count == 0


@pytest.mark.django_db
def test_evaluation_uses_selects_only_and_preserves_all_read_model_counts(
    f_scope: ScopeFixture,
) -> None:
    session = _make_session(f_scope, 'read-only')
    _make_raw_event(f_scope, session, 'read-only')
    _make_observation(f_scope, session, 'read-only')
    _make_candidate(f_scope, 'read-only')
    _make_memory(f_scope, 'read-only')
    read_models = (
        Project,
        RawEventEnvelope,
        ObservationSource,
        Observation,
        AgentSession,
        WorkflowRun,
        MemoryCandidate,
        Memory,
        MemoryVersion,
        RetrievalDocument,
        MemoryLink,
    )
    before = {model: model.objects.count() for model in read_models}

    with CaptureQueriesContext(connection) as captured:
        evaluate_invariants(
            organization_id=f_scope.organization.id,
            project_id=f_scope.project.id,
            as_of=FIXED_AS_OF,
        )

    after = {model: model.objects.count() for model in read_models}

    assert captured.captured_queries
    assert all(query['sql'].lstrip().upper().startswith('SELECT') for query in captured.captured_queries)
    assert after == before


def _materialize_scenario(
    scenario_id: str,
    scope: ScopeFixture,
    entity_refs: list[str],
    *,
    as_of: datetime,
) -> None:
    if scenario_id == 'no_run_session':
        [session_ref] = entity_refs
        session = _make_session(scope, session_ref, entity_id=_synthetic_id(session_ref))
        observation_ref = f'{session_ref}:observation'
        _make_observation(
            scope,
            session,
            observation_ref,
            entity_id=_synthetic_id(observation_ref),
        )

        return

    if scenario_id == 'stale_running_work':
        [run_ref] = entity_refs
        _make_workflow_run(
            scope,
            status=WorkflowRunStatus.RUNNING,
            started_at=as_of - timedelta(minutes=31),
            entity_id=_synthetic_id(run_ref),
            run_type=WorkflowRunType.OBSERVATION_PROCESSING,
        )

        return

    if scenario_id == 'latest_failure_after_prior_success':
        [session_ref] = entity_refs
        session = _make_session(scope, session_ref, entity_id=_synthetic_id(session_ref))
        observation_ref = f'{session_ref}:observation'
        _make_observation(
            scope,
            session,
            observation_ref,
            entity_id=_synthetic_id(observation_ref),
        )
        _make_workflow_run(
            scope,
            status=WorkflowRunStatus.SUCCEEDED,
            session=session,
            entity_id=_synthetic_id(f'{session_ref}:success'),
        )
        _make_workflow_run(
            scope,
            status=WorkflowRunStatus.FAILED,
            session=session,
            entity_id=_synthetic_id(f'{session_ref}:failure'),
        )

        return

    if scenario_id == 'duplicate_delivery':
        [raw_ref] = entity_refs
        session_ref = f'{raw_ref}:session'
        session = _make_session(
            scope,
            session_ref,
            status=SessionStatus.ACTIVE,
            entity_id=_synthetic_id(session_ref),
        )
        raw_event = _make_raw_event(
            scope,
            session,
            raw_ref,
            entity_id=_synthetic_id(raw_ref),
        )
        observation_ref = f'{raw_ref}:observation'
        observation = _make_observation(
            scope,
            session,
            observation_ref,
            entity_id=_synthetic_id(observation_ref),
        )
        source_ref = f'{raw_ref}:source'
        _make_source(
            scope,
            observation,
            raw_event,
            source_ref,
            entity_id=_synthetic_id(source_ref),
        )

        return

    if scenario_id == 'orphan_candidate':
        [candidate_ref] = entity_refs
        _make_candidate(scope, candidate_ref, entity_id=_synthetic_id(candidate_ref))

        return

    if scenario_id == 'partial_promotion':
        [candidate_ref] = entity_refs
        _make_candidate(
            scope,
            candidate_ref,
            status=CandidateStatus.PROMOTED,
            entity_id=_synthetic_id(candidate_ref),
        )

        return

    if scenario_id == 'conflict':
        candidate_ref, memory_ref = entity_refs
        candidate = _make_candidate(
            scope,
            candidate_ref,
            entity_id=_synthetic_id(candidate_ref),
        )
        memory, _version, _document = _make_complete_memory(
            scope,
            memory_ref,
            entity_id=_synthetic_id(memory_ref),
        )
        MemoryLink.objects.create(
            id=_synthetic_id(f'{candidate_ref}:link'),
            organization=scope.organization,
            project=scope.project,
            memory=memory,
            link_type=LinkType.CONFLICTS_WITH,
            target=conflict_candidate_target(candidate.id),
        )

        return

    if scenario_id == 'oversized_session':
        [session_ref] = entity_refs
        session = _make_session(scope, session_ref, entity_id=_synthetic_id(session_ref))

        for index in range(101):
            observation_ref = f'{session_ref}:observation:{index:03d}'
            _make_observation(
                scope,
                session,
                observation_ref,
                entity_id=_synthetic_id(observation_ref),
            )

        return

    raise AssertionError(f'unknown synthetic scenario: {scenario_id}')


SCENARIOS = _load_manifest()['scenarios']


@pytest.mark.django_db
@pytest.mark.parametrize('scenario', SCENARIOS, ids=[scenario['id'] for scenario in SCENARIOS])
def test_manifest_scenarios_materialize_and_foreign_controls_are_isolated(
    scenario: dict[str, Any],
    f_scope: ScopeFixture,
    f_foreign_scope: ScopeFixture,
) -> None:
    _materialize_scenario(
        scenario['id'],
        f_scope,
        scenario['target']['entity_refs'],
        as_of=FIXED_AS_OF,
    )
    before_foreign = evaluate_invariants(
        organization_id=f_scope.organization.id,
        project_id=f_scope.project.id,
        as_of=FIXED_AS_OF,
    )
    before_by_id = {str(result.invariant_id): result for result in before_foreign}

    for expected in scenario['expected_characterization']:
        _assert_characterization(before_by_id[expected['invariant_id']], expected)

    _materialize_scenario(
        scenario['id'],
        f_foreign_scope,
        scenario['foreign_tenant_control']['entity_refs'],
        as_of=FIXED_AS_OF,
    )
    after_foreign = evaluate_invariants(
        organization_id=f_scope.organization.id,
        project_id=f_scope.project.id,
        as_of=FIXED_AS_OF,
    )
    after_by_id = {str(result.invariant_id): result for result in after_foreign}

    assert after_foreign == before_foreign

    for expected in scenario['expected_characterization']:
        _assert_characterization(after_by_id[expected['invariant_id']], expected)
