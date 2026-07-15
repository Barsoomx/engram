from __future__ import annotations

import importlib
import json
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from engram.core.models import (
    Agent,
    AgentSession,
    CandidateStatus,
    DistillationCoverageOutcome,
    DistillationObservationCoverage,
    DistillationStage,
    DistillationStageKind,
    DistillationStageStatus,
    DistillationWindow,
    LinkType,
    Memory,
    MemoryCandidate,
    MemoryCandidateSource,
    MemoryConflict,
    MemoryConflictResolution,
    MemoryLink,
    MemoryStatus,
    MemoryTransition,
    MemoryTransitionType,
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
from engram.imports.services import ClaudeMemImporter
from engram.memory import work_execution
from engram.memory.candidate_work_reconciler import CandidateDecisionWorkInput
from engram.memory.conflict_links import conflict_candidate_target
from engram.memory.distillation_provenance import candidate_source_anchors, canonical_source_manifest
from engram.memory.distillation_provider_stage import stage_key as provider_stage_key
from engram.memory.distillation_provider_stage import stage_target_key
from engram.memory.distillation_window import materialize_distillation_window
from engram.memory.invariant_queries import (
    InvariantId,
    InvariantResult,
    InvariantState,
    evaluate_invariants,
    evaluate_post_cutover_p1_p2,
)
from engram.memory.observation_work_tests import create_scope
from engram.memory.reconciler_test_support import StubBuilder, ended_session_work
from engram.memory.transitions_test_support import (
    candidate_fence_for,
    candidate_in_scope,
    open_single_conflict,
    promoted_pair,
    provenanced_candidate,
    transition_request,
    transition_request_for,
    transitions_module,
)
from engram.memory.workflow_work import (
    CreateWorkflowWorkInput,
    create_work,
    observation_content_digest,
    work_input_fingerprint,
)
from engram.model_policy.models import ModelPolicy, ProviderCallRecord, ProviderSecret

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
    if expected['invariant_id'] == 'P3' and expected['reason'] == 'pre_cutover_session_distillation_unproven':
        assert result.reason == 'legacy_distillation_window_unobservable'
        assert result.violation_count is None
        assert result.proxy_count == expected['proxy_count']
        assert result.sample_ids == _expected_sample_ids(expected['sample_refs'])
        assert result.missing_evidence == 'exact latest and completed input watermarks for legacy sessions'
    elif expected['invariant_id'] == 'P5' and expected['reason'] == 'observation_coverage_relation_missing':
        assert result.reason == 'legacy_observation_coverage_unobservable'
        assert result.violation_count is None
        assert result.proxy_count is None or result.proxy_count >= 0
        assert len(result.sample_ids) <= 20
        assert result.missing_evidence == 'completed CP3 observation coverage and source relations'
    else:
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
            'legacy_observation_coverage_unobservable',
            'completed CP3 observation coverage and source relations',
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

    for invariant_id in ('P6',):
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


_EXACT_SESSION_LEASE = timedelta(seconds=720)


def _exact_results(scope: tuple[Organization, Project, AgentSession], as_of: datetime) -> dict[str, InvariantResult]:
    organization, project, _session = scope

    return {
        str(result.invariant_id): result
        for result in evaluate_invariants(
            organization_id=organization.id,
            project_id=project.id,
            as_of=as_of,
        )
    }


def _claim_session_work(work: WorkflowWork, now: datetime) -> object:
    return work_execution.claim_work(
        work_id=work.id,
        expected_work_type=WorkflowWorkType.SESSION_DISTILLATION,
        lease_owner=f'host:exact:{uuid.uuid4()}',
        now=now,
        lease_for=_EXACT_SESSION_LEASE,
    )


def _settle_session_work(work: WorkflowWork) -> None:
    now = timezone.now()
    claimed = _claim_session_work(work, now)
    work_execution.finish_work_claim(claim=claimed.claim, now=now, completion='product_succeeded')


def _make_stage_policy(scope: tuple[Organization, Project, AgentSession], suffix: str) -> ModelPolicy:
    organization, project, session = scope
    secret = ProviderSecret.objects.create(
        organization=organization,
        team=session.team,
        name=f'Invariant {suffix} secret',
        provider='openai',
        scope='team',
    )
    return ModelPolicy.objects.create(
        organization=organization,
        team=session.team,
        project=project,
        name=f'Invariant {suffix} policy',
        scope='project',
        task_type='curation',
        provider='openai',
        model='gpt-4.1-mini',
        secret=secret,
        version=1,
    )


def _make_stage_history(
    scope: tuple[Organization, Project, AgentSession],
    window: DistillationWindow,
) -> tuple[DistillationStage, DistillationStage]:
    organization, project, session = scope
    chunk = window.chunks.get(ordinal=0)
    primary_policy = _make_stage_policy(scope, 'primary')
    fallback_policy = _make_stage_policy(scope, 'fallback')
    target_key = stage_target_key(
        work_id=str(window.work_id),
        work_input_fingerprint=window.work.input_fingerprint,
        window_input_hash=window.input_hash,
        stage_kind=DistillationStageKind.EXTRACT,
        level=0,
        ordinal=chunk.ordinal,
        chunk_ordinal=chunk.ordinal,
        input_hash=chunk.input_hash,
        prompt_contract='distill_extract.v1',
    )
    primary_stage = DistillationStage.objects.create(
        organization=organization,
        project=project,
        team=session.team,
        window=window,
        chunk=chunk,
        stage_kind=DistillationStageKind.EXTRACT,
        level=0,
        ordinal=chunk.ordinal,
        target_key=target_key,
        stage_key=provider_stage_key(
            target_key=target_key,
            policy_id=str(primary_policy.id),
            policy_version=primary_policy.version,
            policy_role='primary',
        ),
        input_hash=chunk.input_hash,
        input_manifest=chunk.input_manifest,
        prompt_contract='distill_extract.v1',
        policy=primary_policy,
        policy_version=primary_policy.version,
        policy_role='primary',
        status=DistillationStageStatus.REQUIRED,
        attempt_count=1,
        last_failure_class='provider_timeout',
        last_failure_at=timezone.now(),
    )
    call = ProviderCallRecord.objects.create(
        organization=organization,
        project=project,
        team=session.team,
        policy=fallback_policy,
        secret=fallback_policy.secret,
        provider=fallback_policy.provider,
        model=fallback_policy.model,
        task_type=fallback_policy.task_type,
        policy_version=fallback_policy.version,
        request_id=f'distill-stage:{uuid.uuid4()}',
        redaction_state='redacted',
    )
    fallback_stage = DistillationStage.objects.create(
        organization=organization,
        project=project,
        team=session.team,
        window=window,
        chunk=chunk,
        stage_kind=DistillationStageKind.EXTRACT,
        level=0,
        ordinal=chunk.ordinal,
        target_key=target_key,
        stage_key=provider_stage_key(
            target_key=target_key,
            policy_id=str(fallback_policy.id),
            policy_version=fallback_policy.version,
            policy_role='fallback',
        ),
        input_hash=chunk.input_hash,
        input_manifest=chunk.input_manifest,
        prompt_contract='distill_extract.v1',
        policy=fallback_policy,
        policy_version=fallback_policy.version,
        policy_role='fallback',
        status=DistillationStageStatus.COMPLETE,
        attempt_count=1,
        accepted_provider_call=call,
        response_hash='a' * 64,
        response_size=1,
        output_snapshot={'memories': [], 'no_signal_observation_ids': []},
        output_hash='b' * 64,
        completed_at=timezone.now(),
    )
    return fallback_stage, primary_stage


def _candidate_input(candidate: MemoryCandidate, *, manifest: str) -> CandidateDecisionWorkInput:
    return CandidateDecisionWorkInput(
        candidate_id=candidate.id,
        candidate_content_hash=candidate.content_hash,
        organization_id=candidate.organization_id,
        project_id=candidate.project_id,
        team_id=candidate.team_id,
        evidence_manifest_hash=manifest,
        policy_version=1,
    )


@pytest.mark.django_db
def test_p3_is_exact_and_violated_for_required_latest_generation() -> None:
    scope = create_scope('p3-exact-required')
    _organization, _project, session = scope
    ended_session_work(scope, sequence=1)

    p3 = _exact_results(scope, timezone.now())['P3']

    assert p3.state == InvariantState.VIOLATED
    assert any(str(session.id) in sample for sample in p3.sample_ids)


@pytest.mark.django_db
def test_p3_is_exact_and_healthy_when_latest_generation_is_settled() -> None:
    scope = create_scope('p3-exact-settled')
    work = ended_session_work(scope, sequence=1)
    _settle_session_work(work)

    p3 = _exact_results(scope, timezone.now())['P3']

    assert p3.state == InvariantState.VIOLATED
    assert p3.reason == 'latest_distillation_window_incomplete'


@pytest.mark.django_db
def test_p3_exact_success_of_older_generation_never_covers_required_latest() -> None:
    scope = create_scope('p3-exact-no-cover')
    _organization, _project, session = scope
    older = ended_session_work(scope, sequence=1)
    _settle_session_work(older)
    AgentSession.objects.filter(id=session.id).update(
        status=SessionStatus.ACTIVE,
        ended_at=None,
        end_work_contract_version=0,
        observation_sequence_cursor=1,
    )
    ended_session_work(scope, sequence=2)

    p3 = _exact_results(scope, timezone.now())['P3']

    assert p3.state == InvariantState.VIOLATED
    assert any(str(session.id) in sample for sample in p3.sample_ids)


@pytest.mark.django_db
def test_p4_is_exact_and_violated_for_expired_lease() -> None:
    scope = create_scope('p4-exact-expired')
    work = ended_session_work(scope, sequence=1)
    now = timezone.now()
    _claim_session_work(work, now)

    p4 = _exact_results(scope, now + _EXACT_SESSION_LEASE + timedelta(seconds=60))['P4']

    assert p4.state == InvariantState.VIOLATED
    assert any(str(work.id) in sample for sample in p4.sample_ids)


@pytest.mark.django_db
def test_p4_is_exact_and_healthy_with_zero_expired_leases() -> None:
    scope = create_scope('p4-exact-healthy')
    ended_session_work(scope, sequence=1)

    p4 = _exact_results(scope, timezone.now())['P4']

    assert p4.state == InvariantState.HEALTHY


@pytest.mark.django_db
def test_p6_is_builder_aware_and_counts_only_missing_or_inactive_or_mismatched() -> None:
    from engram.memory import candidate_work_reconciler

    scope = create_scope('p6-builder-aware')
    organization, project, session = scope
    satisfied = MemoryCandidate.objects.create(
        organization=organization,
        project=project,
        team=session.team,
        title='p6 satisfied',
        body='p6 satisfied body',
        status=CandidateStatus.PROPOSED,
        content_hash='p6-satisfied-hash',
        confidence=Decimal('0.900'),
    )
    missing = MemoryCandidate.objects.create(
        organization=organization,
        project=project,
        team=session.team,
        title='p6 missing',
        body='p6 missing body',
        status=CandidateStatus.PROPOSED,
        content_hash='p6-missing-hash',
        confidence=Decimal('0.900'),
    )
    active_work = ended_session_work(scope, sequence=1)
    builder = StubBuilder(
        inputs={
            satisfied.id: _candidate_input(satisfied, manifest='manifest-satisfied'),
            missing.id: _candidate_input(missing, manifest='manifest-missing'),
        },
        works_by_manifest={'manifest-satisfied': active_work, 'manifest-missing': None},
    )
    candidate_work_reconciler.set_candidate_decision_work_builder(builder)
    try:
        p6 = _exact_results(scope, timezone.now())['P6']
    finally:
        candidate_work_reconciler.set_candidate_decision_work_builder(None)

    assert p6.state == InvariantState.MISSING_OBSERVABILITY
    assert p6.proxy_count == 1
    assert any(str(missing.id) in sample for sample in p6.sample_ids)
    assert all(str(satisfied.id) not in sample for sample in p6.sample_ids)


@pytest.mark.django_db
def test_p6_without_builder_counts_all_proposed_candidates() -> None:
    from engram.memory import candidate_work_reconciler

    scope = create_scope('p6-no-builder')
    organization, project, session = scope
    for suffix in ('a', 'b'):
        MemoryCandidate.objects.create(
            organization=organization,
            project=project,
            team=session.team,
            title=f'p6 {suffix}',
            body=f'p6 {suffix} body',
            status=CandidateStatus.PROPOSED,
            content_hash=f'p6-no-builder-{suffix}',
            confidence=Decimal('0.900'),
        )
    candidate_work_reconciler.set_candidate_decision_work_builder(None)

    p6 = _exact_results(scope, timezone.now())['P6']

    assert p6.state == InvariantState.MISSING_OBSERVABILITY
    assert p6.proxy_count == 2


@pytest.mark.django_db
def test_p13_remains_missing_observability_with_cp2_cp10_target() -> None:
    scope = create_scope('p13-partial')

    p13 = _exact_results(scope, timezone.now())['P13']

    assert p13.state == InvariantState.MISSING_OBSERVABILITY
    assert p13.reason == 'repair_run_relation_missing'
    assert p13.target_checkpoint == 'CP2/CP10'


@pytest.mark.django_db
def test_exact_invariants_ignore_foreign_scope() -> None:
    owned = create_scope('p34-owned')
    foreign = create_scope('p34-foreign')
    work = ended_session_work(owned, sequence=1)
    now = timezone.now()
    _claim_session_work(work, now)

    foreign_results = _exact_results(foreign, now + _EXACT_SESSION_LEASE + timedelta(seconds=60))

    assert foreign_results['P3'].state == InvariantState.HEALTHY
    assert foreign_results['P4'].state == InvariantState.HEALTHY


def _v0_ended_session_with_useful_observation(
    scope: tuple[Organization, Project, AgentSession],
    suffix: str,
    *,
    with_successful_run: bool = False,
) -> AgentSession:
    organization, project, base_session = scope
    session = AgentSession.objects.create(
        organization=organization,
        project=project,
        team=base_session.team,
        agent=base_session.agent,
        external_session_id=f'v0-residue-{suffix}',
        runtime=Runtime.CODEX,
        status=SessionStatus.ENDED,
        ended_at=timezone.now(),
        end_work_contract_version=0,
    )
    Observation.objects.create(
        organization=organization,
        project=project,
        team=base_session.team,
        agent=base_session.agent,
        session=session,
        observation_type='tool_use',
        title=f'v0 residue observation {suffix}',
        content_hash=f'v0-residue-obs-{session.id}',
        session_sequence=1,
        source_metadata={'event_type': 'post_tool_use'},
    )
    if with_successful_run:
        WorkflowRun.objects.create(
            organization=organization,
            project=project,
            team=base_session.team,
            run_type=WorkflowRunType.SESSION_DISTILLATION,
            status=WorkflowRunStatus.SUCCEEDED,
            input_snapshot={'session_id': str(session.id)},
        )

    return session


@pytest.mark.django_db
def test_p3_v0_residue_without_successful_run_is_missing_observability() -> None:
    scope = create_scope('p3-v0-residue')
    session = _v0_ended_session_with_useful_observation(scope, '1')

    p3 = _exact_results(scope, timezone.now())['P3']

    assert p3.state == InvariantState.MISSING_OBSERVABILITY
    assert p3.proxy_count == 1
    assert p3.sample_ids == (f'session:{session.id}',)


@pytest.mark.django_db
def test_p3_v0_residue_with_successful_run_stays_missing_at_zero_proxy() -> None:
    scope = create_scope('p3-v0-residue-covered')
    _v0_ended_session_with_useful_observation(scope, '1', with_successful_run=True)

    p3 = _exact_results(scope, timezone.now())['P3']

    assert p3.state == InvariantState.MISSING_OBSERVABILITY
    assert p3.proxy_count == 0
    assert p3.sample_ids == ()


@pytest.mark.django_db
def test_p3_v0_residue_with_clean_v1_cohort_stays_missing() -> None:
    scope = create_scope('p3-mixed-clean')
    _v0_ended_session_with_useful_observation(scope, '1')
    settled = ended_session_work(scope, sequence=1)
    _settle_session_work(settled)

    p3 = _exact_results(scope, timezone.now())['P3']

    assert p3.state == InvariantState.VIOLATED
    assert p3.reason == 'latest_distillation_window_incomplete'
    assert p3.sample_ids == (f'session:{settled.subject_id}',)


@pytest.mark.django_db
def test_p3_v1_violation_wins_over_v0_residue() -> None:
    scope = create_scope('p3-mixed-violated')
    _organization, _project, base_session = scope
    _v0_ended_session_with_useful_observation(scope, '1')
    ended_session_work(scope, sequence=1)

    p3 = _exact_results(scope, timezone.now())['P3']

    assert p3.state == InvariantState.VIOLATED
    assert any(str(base_session.id) in sample for sample in p3.sample_ids)


@pytest.mark.django_db
def test_p3_accepts_fallback_completion_after_failed_primary_history() -> None:
    scope = create_scope('p3-fallback-history')
    work = ended_session_work(scope, sequence=1)
    window = materialize_distillation_window(work)
    _make_stage_history(scope, window)
    _settle_session_work(work)

    p3 = _exact_results(scope, timezone.now())['P3']

    assert p3.state == InvariantState.HEALTHY
    assert p3.reason == 'latest_distillation_window_complete'


@pytest.mark.django_db
def test_p3_mixed_cp3_and_legacy_residue_stays_missing_after_proxy_success() -> None:
    scope = create_scope('p3-mixed-legacy-success')
    _v0_ended_session_with_useful_observation(scope, '1', with_successful_run=True)
    work = ended_session_work(scope, sequence=1)
    window = materialize_distillation_window(work)
    _make_stage_history(scope, window)
    _settle_session_work(work)

    p3 = _exact_results(scope, timezone.now())['P3']

    assert p3.state == InvariantState.MISSING_OBSERVABILITY
    assert p3.reason == 'legacy_distillation_window_unobservable'
    assert p3.proxy_count == 0
    assert p3.sample_ids == ()


@pytest.mark.django_db
def test_p5_accepts_multiple_candidate_sources_for_signal_coverage() -> None:
    scope = create_scope('p5-multi-source')
    work = ended_session_work(scope, sequence=1)
    window = materialize_distillation_window(work)
    deciding_stage, _primary_stage = _make_stage_history(scope, window)
    _settle_session_work(work)
    observation = Observation.objects.get(session=scope[2], session_sequence=1)
    digest = observation_content_digest(observation)
    DistillationObservationCoverage.objects.create(
        organization=scope[0],
        project=scope[1],
        team=scope[2].team,
        window=window,
        observation=observation,
        session_sequence=observation.session_sequence,
        observation_digest=digest,
        outcome=DistillationCoverageOutcome.SIGNAL,
        deciding_stage=deciding_stage,
    )
    for suffix in ('a', 'b'):
        candidate = MemoryCandidate.objects.create(
            organization=scope[0],
            project=scope[1],
            team=scope[2].team,
            title=f'p5 source {suffix}',
            body=f'p5 source body {suffix}',
            status=CandidateStatus.PROPOSED,
            content_hash=f'p5-source-{suffix}',
            confidence=Decimal('0.900'),
        )
        anchors = candidate_source_anchors(
            observation,
            observation_id=str(observation.id),
            observation_digest=digest,
        )
        MemoryCandidateSource.objects.create(
            organization=scope[0],
            project=scope[1],
            team=scope[2].team,
            candidate=candidate,
            window=window,
            observation=observation,
            stage=deciding_stage,
            anchors=anchors,
            anchors_hash=canonical_source_manifest(anchors),
        )

    p5 = _exact_results(scope, timezone.now())['P5']

    assert p5.state == InvariantState.HEALTHY
    assert p5.reason == 'completed_window_observations_disposed'


@pytest.mark.django_db
def test_completed_window_p5_query_rejects_each_coverage_anomaly() -> None:
    scope = create_scope('p5-anomalies')
    _organization, _project, _session = scope
    work = ended_session_work(scope, sequence=1)
    materialize_distillation_window(work)
    work.disposition = 'complete'
    work.resolution_reason = 'succeeded'
    work.resolved_at = timezone.now()
    work.execution_state = 'settled'
    work.save(update_fields=['disposition', 'resolution_reason', 'resolved_at', 'execution_state', 'updated_at'])

    p5 = _exact_results(scope, timezone.now())['P5']

    assert p5.state == InvariantState.VIOLATED
    assert p5.reason == 'completed_window_coverage_invalid'


@pytest.mark.django_db
def test_p7_version_one_promotion_chain_is_healthy() -> None:
    candidate, _source, (organization, project, session) = provenanced_candidate('p7-v1-healthy')
    result = transitions_module().PromoteMemoryCandidate().execute(transition_request(candidate))
    scope = ScopeFixture(organization, project, session.team, session.agent)

    p7 = _result_by_id(scope)['P7']

    assert result.memory_version.version == 1
    assert p7.state == InvariantState.HEALTHY
    assert p7.violation_count == 0


@pytest.mark.django_db
def test_import_provenance_fence_hash_and_p7_need_no_candidate_decision_work() -> None:
    provenance = importlib.import_module('engram.memory.import_provenance')
    candidate, source, (organization, project, session) = provenanced_candidate('import-p7')
    fields = {field.name for field in type(source)._meta.fields}
    assert {'source_kind', 'import_source'} <= fields
    import_source = ObservationSource.objects.create(
        organization=organization,
        project=project,
        observation=source.observation,
        source_type='claude_mem',
        source_id='claude-mem:import-p7:1',
    )
    source.source_kind = 'import'
    source.window = None
    source.stage = None
    source.import_source = import_source
    source.anchors = {
        'schema': 'import_candidate_source.v1',
        'observation_id': str(source.observation.id),
        'session_sequence': source.observation.session_sequence,
        'observation_digest': source.observation.content_hash,
        'source_type': 'claude_mem',
        'source_id': import_source.source_id,
        'source_store_id': 'import-p7-store',
        'event_type': 'claude_mem.observation',
        'raw_event_id': None,
    }
    source.anchors_hash = canonical_source_manifest(source.anchors)
    source.save(update_fields=['source_kind', 'window', 'stage', 'import_source', 'anchors', 'anchors_hash'])
    candidate.title = source.observation.title
    candidate.body = source.observation.body
    candidate.content_hash = provenance.import_candidate_content_hash(
        import_source.source_id,
        source.observation.content_hash,
    )
    candidate.decision_work_contract_version = 1
    candidate.save(update_fields=['title', 'body', 'content_hash', 'decision_work_contract_version'])

    expected_hash = ClaudeMemImporter()._content_hash(
        'memory-candidate',
        import_source.source_id,
        source.observation.content_hash,
    )
    assert candidate.content_hash == expected_hash
    manifest_entries, manifest_hash = provenance.candidate_evidence_manifest(candidate)
    assert manifest_entries
    assert manifest_hash == provenance.candidate_evidence_manifest(candidate)[1]

    base_request = transition_request(candidate)
    import_request = replace(
        base_request,
        candidate_fence=replace(base_request.candidate_fence, evidence_manifest_hash=manifest_hash),
    )
    MemoryCandidate.objects.filter(id=candidate.id).update(content_hash='f' * 64)
    with pytest.raises(ValueError, match='stale_decision'):
        transitions_module().PromoteMemoryCandidate().execute(import_request)
    assert not Memory.objects.filter(organization=organization).exists()
    MemoryCandidate.objects.filter(id=candidate.id).update(content_hash=expected_hash)
    candidate.refresh_from_db()
    result = transitions_module().PromoteMemoryCandidate().execute(import_request)
    p7 = _result_by_id(ScopeFixture(organization, project, session.team, session.agent))['P7']
    assert result.memory.transition_contract_version == 1
    assert p7.state == InvariantState.HEALTHY
    assert p7.violation_count == 0
    assert not WorkflowWork.objects.filter(
        work_type=WorkflowWorkType.CANDIDATE_DECISION,
        subject_id=candidate.id,
    ).exists()


@pytest.mark.django_db
def test_p7_version_one_malformed_transition_pointer_is_violated() -> None:
    candidate, _source, (organization, project, session) = provenanced_candidate('p7-v1-malformed')
    result = transitions_module().PromoteMemoryCandidate().execute(transition_request(candidate))
    scope = ScopeFixture(organization, project, session.team, session.agent)
    other_memory = _make_memory(scope, 'p7-v1-pointer-target')
    transition_model = __import__('engram.core.models', fromlist=['MemoryTransition']).MemoryTransition
    transition_model.objects.filter(id=result.transition.id).update(
        memory_id=other_memory.id,
        result_memory_id=other_memory.id,
    )

    p7 = _result_by_id(scope)['P7']

    assert p7.state == InvariantState.VIOLATED
    assert p7.violation_count is not None and p7.violation_count >= 1
    assert f'memory:{result.memory.id}' in p7.sample_ids


@pytest.mark.django_db
@pytest.mark.parametrize('corruption', ('document', 'provenance_source', 'provenance_hash', 'audit_projection'))
def test_p7_version_one_recomputes_projection_and_provenance_contracts(corruption: str) -> None:
    from engram.core.models import MemoryVersionSource

    candidate, _source, (organization, project, session) = provenanced_candidate(f'p7-v1-{corruption}')
    result = transitions_module().PromoteMemoryCandidate().execute(transition_request(candidate))
    scope = ScopeFixture(organization, project, session.team, session.agent)
    if corruption == 'document':
        RetrievalDocument.objects.filter(id=result.retrieval_document.id).update(full_text='tampered exact projection')
    elif corruption == 'provenance_source':
        MemoryVersionSource.objects.filter(memory_version_id=result.memory_version.id).update(
            source_content_hash='f' * 64
        )
    elif corruption == 'provenance_hash':
        type(result.transition).objects.filter(id=result.transition.id).update(provenance_hash='f' * 64)
    else:
        audit = result.transition.audit_event
        metadata = dict(audit.metadata)
        metadata['exact_projection_hash'] = 'f' * 64
        type(audit).objects.filter(id=audit.id).update(metadata=metadata)

    p7 = _result_by_id(scope)['P7']

    assert p7.state == InvariantState.VIOLATED
    assert f'memory:{result.memory.id}' in p7.sample_ids


@pytest.mark.django_db
def test_p7_version_zero_rows_remain_missing_observability() -> None:
    scope = _create_scope('p7-v0-observability')
    memory = _make_memory(scope, 'p7-v0', current_version=1)
    version = _make_version(memory, 'p7-v0')
    _make_document(memory, version)

    p7 = _result_by_id(scope)['P7']

    assert p7.state == InvariantState.MISSING_OBSERVABILITY


@pytest.mark.django_db
def test_p8_version_one_transition_lineage_fixture_is_healthy() -> None:
    candidate, _source, (organization, project, session) = provenanced_candidate('p8-v1-healthy')
    result = transitions_module().PromoteMemoryCandidate().execute(transition_request(candidate))
    scope = ScopeFixture(organization, project, session.team, session.agent)

    p8 = _result_by_id(scope)['P8']

    assert result.memory.transition_contract_version == 1
    assert p8.state == InvariantState.HEALTHY
    assert p8.violation_count == 0


@pytest.mark.django_db
def test_p8_rejects_cross_scope_result_version_corruption() -> None:
    candidate, _source, (organization, project, session) = provenanced_candidate('p8-v1-target-corrupt')
    result = transitions_module().PromoteMemoryCandidate().execute(transition_request(candidate))
    foreign_candidate, _foreign_source, (foreign_org, foreign_project, foreign_session) = provenanced_candidate(
        'p8-v1-foreign-result'
    )
    foreign_result = transitions_module().PromoteMemoryCandidate().execute(transition_request(foreign_candidate))
    MemoryTransition.objects.filter(id=result.transition.id).update(result_version_id=foreign_result.memory_version.id)
    scope = ScopeFixture(organization, project, session.team, session.agent)

    p8 = _result_by_id(scope)['P8']

    assert p8.state == InvariantState.VIOLATED
    assert p8.violation_count is not None and p8.violation_count >= 1
    assert f'memory:{result.memory.id}' in p8.sample_ids
    assert foreign_org.id != organization.id
    assert foreign_project.id != project.id
    assert foreign_session.project_id == foreign_project.id


@pytest.mark.django_db
def test_p8_rejects_foreign_transition_pointing_at_owned_memory() -> None:
    candidate, _source, (organization, project, session) = provenanced_candidate('p8-v1-owned')
    owned_result = transitions_module().PromoteMemoryCandidate().execute(transition_request(candidate))
    foreign_candidate, _foreign_source, (foreign_org, foreign_project, foreign_session) = provenanced_candidate(
        'p8-v1-foreign-only'
    )
    foreign_result = transitions_module().PromoteMemoryCandidate().execute(transition_request(foreign_candidate))
    MemoryTransition.objects.filter(id=foreign_result.transition.id).update(result_memory_id=owned_result.memory.id)
    scope = ScopeFixture(organization, project, session.team, session.agent)

    p8 = _result_by_id(scope)['P8']

    assert p8.state == InvariantState.VIOLATED
    assert p8.violation_count is not None and p8.violation_count >= 1
    assert f'memory:{owned_result.memory.id}' in p8.sample_ids
    assert foreign_org.id != organization.id
    assert foreign_project.id != project.id
    assert foreign_session.project_id == foreign_project.id


@pytest.mark.django_db
def test_p8_ignores_unrelated_foreign_scope_transition() -> None:
    candidate, _source, (organization, project, session) = provenanced_candidate('p8-v1-owned-only')
    transitions_module().PromoteMemoryCandidate().execute(transition_request(candidate))
    foreign_candidate, _foreign_source, (foreign_org, foreign_project, foreign_session) = provenanced_candidate(
        'p8-v1-foreign-unrelated'
    )
    transitions_module().PromoteMemoryCandidate().execute(transition_request(foreign_candidate))
    scope = ScopeFixture(organization, project, session.team, session.agent)

    p8 = _result_by_id(scope)['P8']

    assert p8.state == InvariantState.HEALTHY
    assert p8.violation_count == 0
    assert foreign_org.id != organization.id
    assert foreign_project.id != project.id
    assert foreign_session.project_id == foreign_project.id


@pytest.mark.django_db
def test_p9_open_conflict_fixture_is_healthy_and_protected() -> None:
    candidate, conflict = open_single_conflict('p9-open-healthy')
    scope = ScopeFixture(
        conflict.organization,
        conflict.project,
        conflict.team,
        candidate.source_observation.session.agent,
    )

    p9 = _result_by_id(scope)['P9']

    assert conflict.resolved_transition_id is None
    assert conflict.opened_transition.semantic_link_id == conflict.semantic_link_id
    assert conflict.memory.current_transition_id != conflict.opened_transition_id
    assert p9.state == InvariantState.HEALTHY
    assert p9.violation_count == 0


@pytest.mark.django_db
@pytest.mark.parametrize('resolution', ('publish_candidate', 'merge_candidate', 'supersede_memory', 'reject_candidate'))
def test_p8_p9_real_resolution_outcomes_are_healthy(resolution: str) -> None:
    candidate, conflict = open_single_conflict(f'p9-resolved-{resolution}')
    transitions = transitions_module()
    selected_fence = transitions.build_memory_fence(conflict.memory)
    result = transitions.ResolveMemoryConflict().execute(
        transitions.ResolveMemoryConflictInput(
            request=transition_request_for(
                candidate,
                key=f'p9-resolve:{candidate.id}:{resolution}',
            ),
            candidate_fence=candidate_fence_for(candidate),
            conflict_ids=(conflict.id,),
            conflict_memory_fences=(selected_fence,),
            resolution=resolution,
            selected_memory_fence=selected_fence if resolution in ('merge_candidate', 'supersede_memory') else None,
            title=f'Resolved {resolution}',
            body=f'Resolved body {resolution}',
        ),
    )
    scope = ScopeFixture(
        conflict.organization,
        conflict.project,
        conflict.team,
        candidate.source_observation.session.agent,
    )
    conflict.refresh_from_db()

    results = _result_by_id(scope)
    p8 = results['P8']
    p9 = results['P9']

    assert conflict.resolved_transition_id == result.transition.id
    assert conflict.resolution == resolution
    assert p8.state == InvariantState.HEALTHY
    assert p8.violation_count == 0
    assert p9.state == InvariantState.HEALTHY
    assert p9.violation_count == 0

    MemoryTransition.objects.filter(id=result.transition.id).update(transition_type=MemoryTransitionType.PROMOTE)
    p9_corrupt = _result_by_id(scope)['P9']
    assert p9_corrupt.state == InvariantState.VIOLATED
    assert p9_corrupt.violation_count is not None and p9_corrupt.violation_count >= 1


@pytest.mark.django_db
def test_p9_rejects_foreign_selected_memory_version() -> None:
    candidate, conflict = open_single_conflict('p9-foreign-selected')
    foreign_candidate, foreign_conflict = open_single_conflict('p9-foreign-version')
    scope = ScopeFixture(
        conflict.organization,
        conflict.project,
        conflict.team,
        candidate.source_observation.session.agent,
    )
    MemoryConflict.objects.filter(id=conflict.id).update(memory_version_id=foreign_conflict.memory_version_id)

    p9 = _result_by_id(scope)['P9']

    assert p9.state == InvariantState.VIOLATED
    assert p9.violation_count is not None and p9.violation_count >= 1
    assert f'conflict:{conflict.id}' in p9.sample_ids
    assert foreign_candidate.project_id != scope.project.id


@pytest.mark.django_db
def test_p8_stays_healthy_after_real_candidate_merge() -> None:
    candidate, source, (organization, project, session) = provenanced_candidate('p8-candidate-merge')
    transitions = transitions_module()
    promoted = transitions.PromoteMemoryCandidate().execute(transition_request(candidate))
    merge_candidate, _merge_source = candidate_in_scope(
        candidate,
        source,
        title='Candidate merge evidence',
        body='Candidate merge evidence body',
    )

    result = transitions.MergeMemoryCandidate().execute(
        transitions.MergeMemoryCandidateInput(
            request=transition_request_for(merge_candidate, key=f'p8-merge:{merge_candidate.id}'),
            candidate_fence=candidate_fence_for(merge_candidate),
            memory_fence=transitions.build_memory_fence(promoted.memory),
            title='Merged candidate memory',
            body='Merged candidate memory body',
        ),
    )
    scope = ScopeFixture(organization, project, session.team, session.agent)

    p8 = _result_by_id(scope)['P8']

    assert result.transition.transition_type == MemoryTransitionType.MERGE
    assert p8.state == InvariantState.HEALTHY
    assert p8.violation_count == 0


@pytest.mark.django_db
def test_p8_stays_healthy_after_later_revise_refute_restore_history() -> None:
    candidate, source, (organization, project, session) = provenanced_candidate('p8-later-history')
    transitions = transitions_module()
    promoted = transitions.PromoteMemoryCandidate().execute(transition_request(candidate))
    merge_candidate, _merge_source = candidate_in_scope(
        candidate,
        source,
        title='History merge evidence',
        body='History merge evidence body',
    )
    merged = transitions.MergeMemoryCandidate().execute(
        transitions.MergeMemoryCandidateInput(
            request=transition_request_for(merge_candidate, key=f'p8-history-merge:{merge_candidate.id}'),
            candidate_fence=candidate_fence_for(merge_candidate),
            memory_fence=transitions.build_memory_fence(promoted.memory),
            title='History merged memory',
            body='History merged memory body',
        ),
    )
    revised = transitions.ReviseMemory().execute(
        transitions.ReviseMemoryInput(
            request=transition_request_for(candidate, key=f'p8-history-revise:{candidate.id}'),
            memory_fence=transitions.build_memory_fence(merged.memory),
            title='History revised memory',
            body='History revised memory body',
        ),
    )
    refuted = transitions.RefuteMemory().execute(
        transitions.MemoryStateInput(
            request=transition_request_for(candidate, key=f'p8-history-refute:{candidate.id}'),
            memory_fence=transitions.build_memory_fence(revised.memory),
        ),
    )
    transitions.RestoreMemory().execute(
        transitions.MemoryStateInput(
            request=transition_request_for(candidate, key=f'p8-history-restore:{candidate.id}'),
            memory_fence=transitions.build_memory_fence(refuted.memory),
        ),
    )
    scope = ScopeFixture(organization, project, session.team, session.agent)

    p8 = _result_by_id(scope)['P8']

    assert p8.state == InvariantState.HEALTHY
    assert p8.violation_count == 0


@pytest.mark.django_db
def test_p8_multi_source_provenance_order_is_healthy() -> None:
    candidate, source, (organization, project, session) = provenanced_candidate('p8-multi-source-order')
    scope = ScopeFixture(organization, project, session.team, session.agent)
    observation = _make_observation(scope, session, 'p8-multi-source-second')
    digest = observation_content_digest(observation)
    anchors = candidate_source_anchors(
        observation,
        observation_id=str(observation.id),
        session_sequence=observation.session_sequence,
        observation_digest=digest,
    )
    MemoryCandidateSource.objects.create(
        organization=organization,
        project=project,
        team=session.team,
        candidate=candidate,
        window=source.window,
        observation=observation,
        stage=source.stage,
        anchors=anchors,
        anchors_hash=canonical_source_manifest(anchors),
    )
    transitions = transitions_module()
    transitions.PromoteMemoryCandidate().execute(transition_request(candidate))

    p8 = _result_by_id(scope)['P8']

    assert p8.state == InvariantState.HEALTHY
    assert p8.violation_count == 0


@pytest.mark.django_db
def test_p9_complete_conflict_set_resolution_is_healthy() -> None:
    base_candidate, _second_candidate, first, second = promoted_pair('p9-complete-set')
    source = MemoryCandidateSource.objects.get(candidate=base_candidate)
    organization = base_candidate.organization
    project = base_candidate.project
    session = base_candidate.source_observation.session
    transitions = transitions_module()
    candidate, _candidate_source = candidate_in_scope(
        base_candidate,
        source,
        title='Complete set conflict candidate',
        body='Complete set conflict candidate body',
    )
    for index, memory_result in enumerate((first, second), start=1):
        transitions.OpenMemoryConflict().execute(
            transitions.OpenMemoryConflictInput(
                request=transition_request_for(candidate, key=f'p9-complete-open:{candidate.id}:{index}'),
                candidate_fence=candidate_fence_for(candidate),
                memory_fence=transitions.build_memory_fence(memory_result.memory),
                evidence_hash=str(index) * 64,
                redacted_reason='complete conflict set',
            ),
        )
    conflicts = tuple(MemoryConflict.objects.filter(candidate=candidate).order_by('id'))
    result = transitions.ResolveMemoryConflict().execute(
        transitions.ResolveMemoryConflictInput(
            request=transition_request_for(candidate, key=f'p9-complete-resolve:{candidate.id}'),
            candidate_fence=candidate_fence_for(candidate),
            conflict_ids=tuple(conflict.id for conflict in conflicts),
            conflict_memory_fences=tuple(transitions.build_memory_fence(conflict.memory) for conflict in conflicts),
            resolution=MemoryConflictResolution.REJECT_CANDIDATE,
        ),
    )
    scope = ScopeFixture(organization, project, session.team, session.agent)

    results = _result_by_id(scope)
    p8 = results['P8']
    p9 = results['P9']

    assert result.transition.transition_type == MemoryTransitionType.CONFLICT_RESOLVE
    assert p8.state == InvariantState.HEALTHY
    assert p8.violation_count == 0
    assert p9.state == InvariantState.HEALTHY
    assert p9.violation_count == 0
