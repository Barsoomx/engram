from __future__ import annotations

import hashlib
import uuid
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from django.db.models import CharField, Count, Exists, F, Max, OuterRef, Prefetch, Q, QuerySet, Value
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast, Coalesce, Concat
from django.utils import timezone

from engram.core.models import (
    AgentSession,
    AuditEvent,
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
    MemoryVersionSource,
    Observation,
    ObservationSource,
    Project,
    RawEventEnvelope,
    RetrievalDocument,
    Runtime,
    SessionStatus,
    VisibilityScope,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowRunType,
    WorkflowSubjectType,
    WorkflowWork,
    WorkflowWorkDisposition,
    WorkflowWorkExecutionState,
    WorkflowWorkType,
)
from engram.memory import candidate_work_reconciler
from engram.memory.conflict_links import CONFLICT_CANDIDATE_TARGET_PREFIX
from engram.memory.distillation_provenance import (
    ProvenanceContractError,
    candidate_source_anchors,
    canonical_source_manifest,
)
from engram.memory.distillation_provider_stage import stage_target_key
from engram.memory.import_provenance import is_validated_import_candidate
from engram.memory.projections import build_exact_memory_projection
from engram.memory.session_work_reconciler import inspect_session_work
from engram.memory.transitions import canonical_memory_version_sources, memory_version_provenance_hash
from engram.memory.work_execution import fingerprint_matches
from engram.memory.workflow_work import canonical_json_bytes, observation_content_digest, work_input_fingerprint

_SAMPLE_LIMIT = 20
_POST_CUTOVER_BATCH_SIZE = 100
_LIFECYCLE_OBSERVATION_TYPES = ('session_start', 'session_end')
_REVIEW_MEMORY_STATUSES = (MemoryStatus.CONFLICT, MemoryStatus.REFUTED)
_REVIEW_MEMORY_CONFIDENCE_THRESHOLD = Decimal('0.300')


class InvariantState(StrEnum):
    HEALTHY = 'healthy'
    VIOLATED = 'violated'
    MISSING_OBSERVABILITY = 'missing_observability'


class InvariantId(StrEnum):
    P1 = 'P1'
    P2 = 'P2'
    P3 = 'P3'
    P4 = 'P4'
    P5 = 'P5'
    P6 = 'P6'
    P7 = 'P7'
    P8 = 'P8'
    P9 = 'P9'
    P10 = 'P10'
    P11 = 'P11'
    P12 = 'P12'
    P13 = 'P13'
    P14 = 'P14'
    P15 = 'P15'


@dataclass(frozen=True, slots=True)
class InvariantResult:
    invariant_id: InvariantId
    state: InvariantState
    reason: str
    violation_count: int | None = None
    proxy_count: int | None = None
    sample_ids: tuple[str, ...] = ()
    missing_evidence: str | None = None
    target_checkpoint: str | None = None


_MISSING_CATALOG = {
    InvariantId.P2: (
        'logical_work_intent_relation_missing',
        'durable logical-work-intent relation tied to the source transition',
        'CP1',
    ),
    InvariantId.P3: (
        'legacy_distillation_window_unobservable',
        'exact latest and completed input watermarks for legacy sessions',
        'CP2/CP3',
    ),
    InvariantId.P4: (
        'work_lease_and_reclaim_evidence_missing',
        'lease expiry, owner, heartbeat, and reclaim evidence',
        'CP2',
    ),
    InvariantId.P5: (
        'legacy_observation_coverage_unobservable',
        'completed CP3 observation coverage and source relations for legacy cohorts',
        'CP3',
    ),
    InvariantId.P6: (
        'candidate_decision_work_relation_missing',
        'candidate-to-active-decision-work and canonical conflict relation',
        'CP2/CP3/CP5',
    ),
    InvariantId.P7: (
        'promotion_provenance_audit_relation_missing',
        'relational promotion provenance and transition audit identity',
        'CP4',
    ),
    InvariantId.P8: (
        'memory_transition_history_relation_missing',
        'immutable transition history and authoritative current pointer',
        'CP4',
    ),
    InvariantId.P9: (
        'durable_conflict_evidence_relation_missing',
        'conflict evidence surviving cleanup and restart',
        'CP4/CP5',
    ),
    InvariantId.P10: (
        'replay_evidence_fields_missing',
        'replay fingerprint, byte hash, authorization, and budget evidence',
        'CP6',
    ),
    InvariantId.P11: (
        'temporal_eligibility_evidence_missing',
        'retrieval-time temporal eligibility evidence',
        'CP8',
    ),
    InvariantId.P13: (
        'repair_run_relation_missing',
        'repair identity, progress, idempotency, and dry-run explanation',
        'CP2/CP10',
    ),
    InvariantId.P14: (
        'operation_scope_resolution_evidence_missing',
        'operation-to-resolved organization/project/team evidence',
        'CP1+',
    ),
    InvariantId.P15: (
        'repository_impact_coverage_relation_missing',
        'memory revision and impact-coverage revision relation',
        'CP8',
    ),
}


def evaluate_invariants(
    *,
    organization_id: uuid.UUID,
    project_id: uuid.UUID,
    as_of: datetime | None = None,
) -> tuple[InvariantResult, ...]:
    project = Project.objects.only('id', 'organization_id').get(
        id=project_id,
        organization_id=organization_id,
    )
    effective_as_of = as_of or timezone.now()

    if timezone.is_naive(effective_as_of):
        raise ValueError('as_of must be timezone-aware')

    return (
        _evaluate_p1(project),
        _missing(InvariantId.P2),
        _evaluate_p3(project, effective_as_of),
        _evaluate_p4(project, effective_as_of),
        _evaluate_p5(project),
        _evaluate_p6(project, effective_as_of),
        _evaluate_p7(project),
        _evaluate_p8(project),
        _evaluate_p9(project),
        _missing(InvariantId.P10),
        _missing(InvariantId.P11),
        _evaluate_p12(project),
        _missing(InvariantId.P13),
        _missing(InvariantId.P14),
        _missing(InvariantId.P15),
    )


def evaluate_post_cutover_p1_p2(
    *,
    organization_id: uuid.UUID,
    project_id: uuid.UUID,
) -> tuple[InvariantResult, InvariantResult]:
    project = Project.objects.only('id', 'organization_id').get(
        id=project_id,
        organization_id=organization_id,
    )

    return _evaluate_post_cutover_p1(project), _evaluate_post_cutover_p2(project)


def _missing(
    invariant_id: InvariantId,
    *,
    violation_count: int | None = None,
    proxy_count: int | None = None,
    sample_ids: tuple[str, ...] = (),
) -> InvariantResult:
    reason, missing_evidence, target_checkpoint = _MISSING_CATALOG[invariant_id]

    return InvariantResult(
        invariant_id=invariant_id,
        state=InvariantState.MISSING_OBSERVABILITY,
        reason=reason,
        violation_count=violation_count,
        proxy_count=proxy_count,
        sample_ids=sample_ids,
        missing_evidence=missing_evidence,
        target_checkpoint=target_checkpoint,
    )


def _evaluate_post_cutover_p1(project: Project) -> InvariantResult:
    typed_raw_events = RawEventEnvelope.objects.filter(
        organization_id=project.organization_id,
        project_id=project.id,
        normalization_contract_version=1,
    ).annotate(
        total_source_count=Count('observation_sources', distinct=True),
        valid_source_count=Count(
            'observation_sources',
            filter=Q(
                observation_sources__organization_id=project.organization_id,
                observation_sources__project_id=project.id,
                observation_sources__raw_event_id=F('id'),
                observation_sources__observation__organization_id=project.organization_id,
                observation_sources__observation__project_id=project.id,
                observation_sources__observation__session_id=F('session_id'),
                session__organization_id=project.organization_id,
                session__project_id=project.id,
            )
            & (
                Q(observation_sources__observation__team_id=F('team_id'))
                | Q(
                    observation_sources__observation__team_id__isnull=True,
                    team_id__isnull=True,
                )
            )
            & (Q(session__team_id=F('team_id')) | Q(session__team_id__isnull=True, team_id__isnull=True)),
            distinct=True,
        ),
    )
    valid_observation = Q(
        normalization_disposition='observation',
        normalization_reason__isnull=True,
        total_source_count=1,
        valid_source_count=1,
    )
    valid_no_op = Q(
        normalization_disposition='no_op',
        normalization_reason='evidence_only',
        total_source_count=0,
        session__organization_id=project.organization_id,
        session__project_id=project.id,
    ) & (Q(session__team_id=F('team_id')) | Q(session__team_id__isnull=True, team_id__isnull=True))
    invalid_raw_events = typed_raw_events.exclude(valid_observation | valid_no_op)
    violation_count = invalid_raw_events.count()

    if violation_count:
        return InvariantResult(
            invariant_id=InvariantId.P1,
            state=InvariantState.VIOLATED,
            reason='raw_event_normalization_cardinality_invalid',
            violation_count=violation_count,
            sample_ids=_query_samples(invalid_raw_events, 'raw_event'),
            target_checkpoint='CP1',
        )

    return InvariantResult(
        invariant_id=InvariantId.P1,
        state=InvariantState.HEALTHY,
        reason='scoped_raw_events_normalized',
        violation_count=0,
        target_checkpoint='CP1',
    )


def _valid_hook_work_policy(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {'schema', 'realtime_candidates_enabled', 'legacy_policy_fallback'}
        and value['schema'] == 'hook_work_policy/v1'
        and type(value['realtime_candidates_enabled']) is bool
        and value['legacy_policy_fallback'] is False
    )


def _valid_hook_event_type(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _canonical_hook_observation(
    *,
    project: Project,
    raw_event: RawEventEnvelope,
    sources: list[ObservationSource],
) -> Observation | None:
    if len(sources) != 1:
        return None

    source = sources[0]
    observation = source.observation
    if (
        source.organization_id != project.organization_id
        or source.project_id != project.id
        or source.raw_event_id != raw_event.id
        or source.source_type != 'hook_event'
        or source.source_id != raw_event.client_event_id
        or observation.organization_id != project.organization_id
        or observation.project_id != project.id
        or observation.session_id != raw_event.session_id
        or observation.team_id != raw_event.team_id
        or raw_event.session.organization_id != project.organization_id
        or raw_event.session.project_id != project.id
        or raw_event.session.team_id != raw_event.team_id
    ):
        return None

    observation_metadata = observation.source_metadata
    source_metadata = source.metadata
    trusted_event_type = observation_metadata.get('event_type') if isinstance(observation_metadata, dict) else None
    source_event_type = source_metadata.get('event_type') if isinstance(source_metadata, dict) else None
    if (
        not _valid_hook_event_type(raw_event.event_type)
        or not _valid_hook_event_type(trusted_event_type)
        or not _valid_hook_event_type(source_event_type)
        or trusted_event_type != raw_event.event_type
        or source_event_type != raw_event.event_type
    ):
        return None

    return observation


def _record_bounded_sample(sample_ids: set[str], sample_id: str) -> None:
    sample_ids.add(sample_id)
    if len(sample_ids) > _SAMPLE_LIMIT:
        sample_ids.intersection_update(sorted(sample_ids, key=_sample_sort_key)[:_SAMPLE_LIMIT])


def _batched_raw_events(queryset: QuerySet[RawEventEnvelope]) -> Iterator[list[RawEventEnvelope]]:
    batch: list[RawEventEnvelope] = []
    for raw_event in queryset.iterator(chunk_size=_POST_CUTOVER_BATCH_SIZE):
        batch.append(raw_event)
        if len(batch) == _POST_CUTOVER_BATCH_SIZE:
            yield batch
            batch = []
    if batch:
        yield batch


def _observation_work_index(
    *,
    project: Project,
    observations: list[Observation],
) -> dict[tuple[uuid.UUID, uuid.UUID | None], list[WorkflowWork]]:
    if not observations:
        return {}

    works = WorkflowWork.objects.filter(
        organization_id=project.organization_id,
        project_id=project.id,
        work_type=WorkflowWorkType.OBSERVATION_PROCESSING,
        subject_type=WorkflowSubjectType.OBSERVATION,
        subject_id__in=[observation.id for observation in observations],
        contract_version=1,
        occurrence_key='',
    ).order_by('id')
    index: dict[tuple[uuid.UUID, uuid.UUID | None], list[WorkflowWork]] = {}
    for work in works:
        index.setdefault((work.subject_id, work.team_id), []).append(work)

    return index


def _stored_observation_work_matches(
    *,
    work: WorkflowWork,
    observation: Observation,
    policy: dict[str, object],
) -> bool:
    snapshot = work.input_snapshot
    if not isinstance(snapshot, dict):
        return False

    try:
        stored_fingerprint = work_input_fingerprint(
            work_type=WorkflowWorkType.OBSERVATION_PROCESSING,
            subject_type=WorkflowSubjectType.OBSERVATION,
            subject_id=observation.id,
            contract_version=1,
            occurrence_key='',
            input_snapshot=snapshot,
        )
    except ValueError:
        return False

    if stored_fingerprint != work.input_fingerprint:
        return False
    if (
        snapshot.get('schema') != 'observation_processing_input/v1'
        or snapshot.get('observation_id') != str(observation.id)
        or snapshot.get('observation_digest') != observation_content_digest(observation)
    ):
        return False

    expected_snapshot = {
        'schema': 'observation_processing_input/v1',
        'observation_id': str(observation.id),
        'observation_digest': observation_content_digest(observation),
        'policy': policy,
    }
    try:
        expected_fingerprint = work_input_fingerprint(
            work_type=WorkflowWorkType.OBSERVATION_PROCESSING,
            subject_type=WorkflowSubjectType.OBSERVATION,
            subject_id=observation.id,
            contract_version=1,
            occurrence_key='',
            input_snapshot=expected_snapshot,
        )
    except ValueError:
        return False

    return stored_fingerprint == expected_fingerprint


def _evaluate_post_cutover_p2(project: Project) -> InvariantResult:
    policy_raw_events = (
        RawEventEnvelope.objects.filter(
            organization_id=project.organization_id,
            project_id=project.id,
            normalization_contract_version=1,
            source_adapter__in=(Runtime.CODEX, Runtime.CLAUDE_CODE, Runtime.UNKNOWN),
        )
        .select_related('session')
        .prefetch_related(
            Prefetch(
                'observation_sources',
                queryset=ObservationSource.objects.select_related('observation'),
            ),
        )
        .order_by('id')
    )
    violation_count = 0
    sample_ids: set[str] = set()

    for batch in _batched_raw_events(policy_raw_events):
        work_inputs: list[tuple[RawEventEnvelope, Observation, dict[str, object]]] = []
        for raw_event in batch:
            metadata = raw_event.metadata
            policy = metadata.get('work_policy_v1') if isinstance(metadata, dict) else None
            if not _valid_hook_work_policy(policy):
                violation_count += 1
                _record_bounded_sample(sample_ids, f'raw_event:{raw_event.id}')
                continue

            observation = _canonical_hook_observation(
                project=project,
                raw_event=raw_event,
                sources=list(raw_event.observation_sources.all()),
            )
            if observation is None:
                violation_count += 1
                _record_bounded_sample(sample_ids, f'raw_event:{raw_event.id}')
                continue

            work_inputs.append((raw_event, observation, policy))

        work_index = _observation_work_index(
            project=project,
            observations=[observation for _raw_event, observation, _policy in work_inputs],
        )
        for raw_event, observation, policy in work_inputs:
            is_lifecycle = raw_event.event_type in _LIFECYCLE_OBSERVATION_TYPES
            realtime_enabled = policy['realtime_candidates_enabled']
            if not realtime_enabled or is_lifecycle:
                continue

            matching_work = work_index.get((observation.id, observation.team_id), ())
            work_is_valid = any(
                _stored_observation_work_matches(
                    work=work,
                    observation=observation,
                    policy=policy,
                )
                for work in matching_work
            )
            if not work_is_valid:
                violation_count += 1
                _record_bounded_sample(sample_ids, f'raw_event:{raw_event.id}')

    if not violation_count:
        return InvariantResult(
            invariant_id=InvariantId.P2,
            state=InvariantState.HEALTHY,
            reason='post_cutover_work_intent_present',
            violation_count=0,
            target_checkpoint='CP1',
        )

    return InvariantResult(
        invariant_id=InvariantId.P2,
        state=InvariantState.VIOLATED,
        reason='post_cutover_work_intent_relation_invalid',
        violation_count=violation_count,
        sample_ids=tuple(sorted(sample_ids, key=_sample_sort_key)),
        target_checkpoint='CP1',
    )


def _query_samples(queryset: QuerySet, prefix: str) -> tuple[str, ...]:
    return tuple(
        f'{prefix}:{entity_id}' for entity_id in queryset.order_by('id').values_list('id', flat=True)[:_SAMPLE_LIMIT]
    )


def _sample_sort_key(sample_id: str) -> tuple[int, str]:
    prefix, entity_id = sample_id.split(':', maxsplit=1)

    return uuid.UUID(entity_id).int, prefix


def _merge_samples(*sample_groups: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted(set().union(*sample_groups), key=_sample_sort_key)[:_SAMPLE_LIMIT])


def _evaluate_p1(project: Project) -> InvariantResult:
    invalid_raw_events = (
        RawEventEnvelope.objects.filter(
            organization_id=project.organization_id,
            project_id=project.id,
        )
        .annotate(
            total_source_count=Count('observation_sources', distinct=True),
            valid_source_count=Count(
                'observation_sources',
                filter=Q(
                    observation_sources__organization_id=project.organization_id,
                    observation_sources__project_id=project.id,
                    observation_sources__observation__organization_id=project.organization_id,
                    observation_sources__observation__project_id=project.id,
                ),
                distinct=True,
            ),
        )
        .exclude(total_source_count=1, valid_source_count=1)
    )
    violation_count = invalid_raw_events.count()

    if violation_count:
        return InvariantResult(
            invariant_id=InvariantId.P1,
            state=InvariantState.VIOLATED,
            reason='raw_event_normalization_cardinality_invalid',
            violation_count=violation_count,
            sample_ids=_query_samples(invalid_raw_events, 'raw_event'),
            target_checkpoint='CP1',
        )

    return InvariantResult(
        invariant_id=InvariantId.P1,
        state=InvariantState.HEALTHY,
        reason='scoped_raw_events_normalized',
        violation_count=0,
        target_checkpoint='CP1',
    )


def _pre_cutover_residue_sessions(project: Project) -> QuerySet:
    non_lifecycle_observations = Observation.objects.filter(
        organization_id=project.organization_id,
        project_id=project.id,
        session_id=OuterRef('id'),
    ).exclude(observation_type__in=_LIFECYCLE_OBSERVATION_TYPES)

    return (
        AgentSession.objects.filter(
            organization_id=project.organization_id,
            project_id=project.id,
            status=SessionStatus.ENDED,
        )
        .exclude(end_work_contract_version=1)
        .annotate(has_non_lifecycle_observation=Exists(non_lifecycle_observations))
        .filter(has_non_lifecycle_observation=True)
    )


def _evaluate_p3(project: Project, as_of: datetime) -> InvariantResult:
    cp3_sessions = (
        AgentSession.objects.filter(
            organization_id=project.organization_id,
            project_id=project.id,
            status=SessionStatus.ENDED,
            end_work_contract_version=1,
        )
        .annotate(
            latest_useful_sequence=Max(
                'observations__session_sequence',
                filter=Q(observations__organization_id=project.organization_id)
                & Q(observations__project_id=project.id)
                & (
                    Q(observations__source_metadata__event_type__isnull=True)
                    | ~Q(observations__source_metadata__event_type__in=_LIFECYCLE_OBSERVATION_TYPES)
                ),
            ),
        )
        .filter(latest_useful_sequence__gt=0)
        .order_by('id')
    )
    if cp3_sessions.exists():
        violation_ids: set[str] = set()
        violation_count = 0
        for session in cp3_sessions.iterator(chunk_size=_POST_CUTOVER_BATCH_SIZE):
            upper = session.latest_useful_sequence
            work = WorkflowWork.objects.filter(
                organization_id=project.organization_id,
                project_id=project.id,
                team_id=session.team_id,
                work_type=WorkflowWorkType.SESSION_DISTILLATION,
                subject_type=WorkflowSubjectType.AGENT_SESSION,
                subject_id=session.id,
                contract_version=1,
            ).order_by('created_at', 'id')
            exact_work = next(
                (
                    candidate
                    for candidate in work
                    if isinstance(candidate.input_snapshot, dict)
                    and candidate.input_snapshot.get('lower_sequence_exclusive') == 0
                    and candidate.input_snapshot.get('upper_sequence_inclusive') == upper
                    and fingerprint_matches(candidate)
                ),
                None,
            )
            complete = exact_work is not None and _cp3_work_complete(exact_work, project)
            if not complete:
                violation_count += 1
                _record_bounded_sample(violation_ids, f'session:{session.id}')

        if violation_count:
            return InvariantResult(
                invariant_id=InvariantId.P3,
                state=InvariantState.VIOLATED,
                reason='latest_distillation_window_incomplete',
                violation_count=violation_count,
                sample_ids=tuple(sorted(violation_ids, key=_sample_sort_key)),
                target_checkpoint='CP3',
            )

        legacy = _pre_cutover_residue_sessions(project)
        successful_runs = (
            WorkflowRun.objects.filter(
                organization_id=project.organization_id,
                project_id=project.id,
                run_type=WorkflowRunType.SESSION_DISTILLATION,
                status=WorkflowRunStatus.SUCCEEDED,
            )
            .annotate(session_id_text=KeyTextTransform('session_id', 'input_snapshot'))
            .filter(session_id_text=Cast(OuterRef('id'), output_field=CharField()))
        )
        unproven_legacy = legacy.annotate(has_successful_run=Exists(successful_runs)).filter(
            has_successful_run=False,
        )
        if legacy.exists():
            return InvariantResult(
                invariant_id=InvariantId.P3,
                state=InvariantState.MISSING_OBSERVABILITY,
                reason='legacy_distillation_window_unobservable',
                proxy_count=unproven_legacy.count(),
                sample_ids=_query_samples(unproven_legacy, 'session'),
                missing_evidence='exact latest and completed input watermarks for legacy sessions',
                target_checkpoint='CP2/CP3',
            )

        return InvariantResult(
            invariant_id=InvariantId.P3,
            state=InvariantState.HEALTHY,
            reason='latest_distillation_window_complete',
            violation_count=0,
            target_checkpoint='CP3',
        )

    inspection = inspect_session_work(
        organization_id=project.organization_id,
        project_id=project.id,
        as_of=as_of,
    )
    if inspection.findings:
        sample_ids = tuple(
            sorted(
                {f'session:{finding.entity_id}' for finding in inspection.findings},
                key=_sample_sort_key,
            )[:_SAMPLE_LIMIT]
        )

        return InvariantResult(
            invariant_id=InvariantId.P3,
            state=InvariantState.VIOLATED,
            reason='ended_session_current_generation_work_inexact',
            violation_count=len(inspection.findings),
            sample_ids=sample_ids,
            target_checkpoint='CP2',
        )

    residue = _pre_cutover_residue_sessions(project)
    if not residue.exists():
        return InvariantResult(
            invariant_id=InvariantId.P3,
            state=InvariantState.HEALTHY,
            reason='ended_session_current_generation_work_exact',
            violation_count=0,
            target_checkpoint='CP2',
        )

    successful_runs = (
        WorkflowRun.objects.filter(
            organization_id=project.organization_id,
            project_id=project.id,
            run_type=WorkflowRunType.SESSION_DISTILLATION,
            status=WorkflowRunStatus.SUCCEEDED,
        )
        .annotate(session_id_text=KeyTextTransform('session_id', 'input_snapshot'))
        .filter(session_id_text=Cast(OuterRef('id'), output_field=CharField()))
    )
    unproven = residue.annotate(has_successful_run=Exists(successful_runs)).filter(has_successful_run=False)

    return InvariantResult(
        invariant_id=InvariantId.P3,
        state=InvariantState.MISSING_OBSERVABILITY,
        reason='legacy_distillation_window_unobservable',
        proxy_count=unproven.count(),
        sample_ids=_query_samples(unproven, 'session'),
        missing_evidence='exact latest and completed input watermarks for legacy sessions',
        target_checkpoint='CP2/CP3',
    )


def _cp3_work_complete(work: WorkflowWork, project: Project) -> bool:
    if (
        work.disposition != WorkflowWorkDisposition.COMPLETE
        or work.execution_state != WorkflowWorkExecutionState.SETTLED
    ):
        return False

    try:
        window = DistillationWindow.objects.get(
            work_id=work.id,
            organization_id=project.organization_id,
            project_id=project.id,
            session_id=work.subject_id,
        )
    except DistillationWindow.DoesNotExist:
        return False

    stages = list(
        DistillationStage.objects.filter(
            window_id=window.id,
            organization_id=project.organization_id,
            project_id=project.id,
        ).only('id', 'status', 'stage_kind', 'chunk_id')
    )
    stages_by_target: dict[str, list[DistillationStage]] = defaultdict(list)
    for stage in stages:
        stages_by_target[stage.target_key].append(stage)
    accepted_stages: list[DistillationStage] = []
    for target_stages in stages_by_target.values():
        accepted = [stage for stage in target_stages if stage.status == DistillationStageStatus.COMPLETE]
        if len(accepted) != 1:
            return False
        accepted_stages.append(accepted[0])
    if not stages:
        return False

    chunk_ids = set(window.chunks.values_list('id', flat=True))
    extract_chunk_ids = {
        stage.chunk_id
        for stage in accepted_stages
        if stage.stage_kind == DistillationStageKind.EXTRACT and stage.chunk_id is not None
    }
    return chunk_ids <= extract_chunk_ids


def _evaluate_p5(project: Project) -> InvariantResult:
    windows = (
        DistillationWindow.objects.filter(
            organization_id=project.organization_id,
            project_id=project.id,
            work__organization_id=project.organization_id,
            work__project_id=project.id,
            work__disposition=WorkflowWorkDisposition.COMPLETE,
        )
        .select_related('work')
        .order_by('id')
    )
    if not windows.exists():
        legacy = _pre_cutover_residue_sessions(project)
        legacy_count = legacy.count()
        return InvariantResult(
            invariant_id=InvariantId.P5,
            state=InvariantState.MISSING_OBSERVABILITY,
            reason='legacy_observation_coverage_unobservable',
            proxy_count=legacy_count or None,
            sample_ids=_query_samples(legacy, 'session'),
            missing_evidence='completed CP3 observation coverage and source relations',
            target_checkpoint='CP3',
        )

    violation_count = 0
    sample_ids: set[str] = set()
    for window in windows.iterator(chunk_size=_POST_CUTOVER_BATCH_SIZE):
        findings = _p5_window_findings(window)
        violation_count += len(findings)
        for prefix, entity_id in findings:
            _record_bounded_sample(sample_ids, f'{prefix}:{entity_id}')

    if violation_count:
        return InvariantResult(
            invariant_id=InvariantId.P5,
            state=InvariantState.VIOLATED,
            reason='completed_window_coverage_invalid',
            violation_count=violation_count,
            sample_ids=tuple(sorted(sample_ids, key=_sample_sort_key)),
            target_checkpoint='CP3',
        )

    legacy = _pre_cutover_residue_sessions(project)
    if legacy.exists():
        return InvariantResult(
            invariant_id=InvariantId.P5,
            state=InvariantState.MISSING_OBSERVABILITY,
            reason='legacy_observation_coverage_unobservable',
            proxy_count=legacy.count(),
            sample_ids=_query_samples(legacy, 'session'),
            missing_evidence='completed CP3 observation coverage and source relations',
            target_checkpoint='CP3',
        )

    return InvariantResult(
        invariant_id=InvariantId.P5,
        state=InvariantState.HEALTHY,
        reason='completed_window_observations_disposed',
        violation_count=0,
        target_checkpoint='CP3',
    )


def _p5_window_findings(window: DistillationWindow) -> list[tuple[str, uuid.UUID]]:  # noqa: C901
    findings: list[tuple[str, uuid.UUID]] = []
    if not _cp3_work_complete(window.work, window.project):
        findings.append(('window', window.id))

    chunks = list(
        window.chunks.filter(
            organization_id=window.organization_id,
            project_id=window.project_id,
        ).order_by('ordinal', 'id')
    )
    expected: dict[uuid.UUID, tuple[int, str]] = {}
    expected_sequences: set[int] = set()
    for chunk in chunks:
        observations = chunk.input_manifest.get('observations') if isinstance(chunk.input_manifest, dict) else None
        if not isinstance(observations, list):
            findings.append(('chunk', chunk.id))
            continue
        if (
            chunk.input_manifest.get('schema') != 'distillation_chunk_manifest.v1'
            or chunk.input_manifest.get('window_input_hash') != window.input_hash
            or chunk.input_manifest.get('ordinal') != chunk.ordinal
            or chunk.observation_count != len(observations)
            or hashlib.sha256(canonical_json_bytes(chunk.input_manifest)).hexdigest() != chunk.input_hash
        ):
            findings.append(('chunk', chunk.id))
        if observations:
            sequences = [entry.get('session_sequence') for entry in observations if isinstance(entry, dict)]
            if sequences and (chunk.first_sequence != min(sequences) or chunk.last_sequence != max(sequences)):
                findings.append(('chunk', chunk.id))
        for entry in observations:
            if not isinstance(entry, dict):
                findings.append(('chunk', chunk.id))
                continue
            try:
                observation_id = uuid.UUID(str(entry['observation_id']))
                sequence = entry['session_sequence']
                digest = entry['content_digest']
            except (KeyError, TypeError, ValueError):
                findings.append(('chunk', chunk.id))
                continue
            if type(sequence) is not int or sequence <= 0 or not isinstance(digest, str):
                findings.append(('chunk', chunk.id))
                continue
            if observation_id in expected or sequence in expected_sequences:
                findings.append(('chunk', chunk.id))
            expected[observation_id] = (sequence, digest)
            expected_sequences.add(sequence)
    if window.observation_count != len(expected):
        findings.append(('window', window.id))

    stages = list(
        DistillationStage.objects.filter(window_id=window.id)
        .select_related('chunk')
        .order_by('stage_kind', 'level', 'ordinal', 'id')
    )
    stage_by_id: dict[uuid.UUID, DistillationStage] = {}
    stage_keys: set[str] = set()
    stages_by_target: dict[str, list[DistillationStage]] = defaultdict(list)
    for stage in stages:
        stages_by_target[stage.target_key].append(stage)
    extract_chunks: set[uuid.UUID] = set()
    for stage in stages:
        if (
            stage.organization_id != window.organization_id
            or stage.project_id != window.project_id
            or stage.team_id != window.team_id
            or stage.window_id != window.id
            or stage.stage_key in stage_keys
        ):
            findings.append(('stage', stage.id))
        stage_keys.add(stage.stage_key)
        expected_target_key = stage_target_key(
            work_id=str(window.work_id),
            work_input_fingerprint=window.work.input_fingerprint,
            window_input_hash=window.input_hash,
            stage_kind=stage.stage_kind,
            level=stage.level,
            ordinal=stage.ordinal,
            chunk_ordinal=stage.chunk.ordinal if stage.chunk_id is not None else None,
            input_hash=stage.input_hash,
            prompt_contract=stage.prompt_contract,
        )
        if stage.target_key != expected_target_key:
            findings.append(('stage', stage.id))
        if stage.status != DistillationStageStatus.COMPLETE:
            continue
        accepted_for_target = [
            candidate
            for candidate in stages_by_target[stage.target_key]
            if candidate.status == DistillationStageStatus.COMPLETE
        ]
        if len(accepted_for_target) != 1:
            findings.append(('window', window.id))
            continue
        stage_by_id[stage.id] = stage
        if stage.stage_kind == DistillationStageKind.EXTRACT:
            if stage.chunk_id is None or stage.chunk.window_id != window.id or stage.level != 0:
                findings.append(('stage', stage.id))
            else:
                extract_chunks.add(stage.chunk_id)
                if stage.input_hash != stage.chunk.input_hash or stage.input_manifest != stage.chunk.input_manifest:
                    findings.append(('stage', stage.id))
        elif stage.chunk_id is not None or stage.level <= 0:
            findings.append(('stage', stage.id))
    if (
        not stages_by_target
        or len(stage_by_id) != len(stages_by_target)
        or extract_chunks != {chunk.id for chunk in chunks}
    ):
        findings.append(('window', window.id))

    coverage = list(
        DistillationObservationCoverage.objects.filter(window_id=window.id)
        .select_related('observation', 'deciding_stage')
        .order_by('session_sequence', 'id')
    )
    coverage_by_observation: dict[uuid.UUID, list[DistillationObservationCoverage]] = defaultdict(list)
    coverage_by_sequence: dict[int, list[DistillationObservationCoverage]] = defaultdict(list)
    for row in coverage:
        coverage_by_observation[row.observation_id].append(row)
        coverage_by_sequence[row.session_sequence].append(row)
        stage = row.deciding_stage
        valid_scope = (
            row.organization_id == window.organization_id
            and row.project_id == window.project_id
            and row.team_id == window.team_id
            and row.observation.organization_id == window.organization_id
            and row.observation.project_id == window.project_id
            and row.observation.session_id == window.session_id
            and stage.id in stage_by_id
            and stage.organization_id == window.organization_id
            and stage.project_id == window.project_id
            and stage.team_id == window.team_id
            and stage.status == DistillationStageStatus.COMPLETE
        )
        expected_value = expected.get(row.observation_id)
        if not valid_scope or expected_value != (row.session_sequence, row.observation_digest):
            findings.append(('coverage', row.id))
        if row.outcome not in (DistillationCoverageOutcome.SIGNAL, DistillationCoverageOutcome.NO_SIGNAL):
            findings.append(('coverage', row.id))
    for observation_id, value in expected.items():
        rows = coverage_by_observation.get(observation_id, [])
        if len(rows) != 1:
            findings.append(('window', window.id))
        if len(coverage_by_sequence.get(value[0], [])) != 1:
            findings.append(('window', window.id))

    sources = list(
        MemoryCandidateSource.objects.filter(window_id=window.id)
        .select_related('candidate', 'observation', 'stage')
        .order_by('id')
    )
    sources_by_observation: dict[uuid.UUID, list[MemoryCandidateSource]] = defaultdict(list)
    for source in sources:
        sources_by_observation[source.observation_id].append(source)
        stage = source.stage
        if (
            source.organization_id != window.organization_id
            or source.project_id != window.project_id
            or source.team_id != window.team_id
            or source.candidate.organization_id != window.organization_id
            or source.candidate.project_id != window.project_id
            or source.observation.organization_id != window.organization_id
            or source.observation.project_id != window.project_id
            or source.observation.session_id != window.session_id
            or stage.id not in stage_by_id
            or stage.organization_id != window.organization_id
            or stage.project_id != window.project_id
            or stage.team_id != window.team_id
            or stage.status != DistillationStageStatus.COMPLETE
        ):
            findings.append(('candidate_source', source.id))
        else:
            try:
                expected_anchors_hash = canonical_source_manifest(
                    candidate_source_anchors(
                        source.observation,
                        observation_id=str(source.observation_id),
                        observation_digest=observation_content_digest(source.observation),
                    )
                )
            except (ProvenanceContractError, TypeError, ValueError):
                findings.append(('candidate_source', source.id))
            else:
                if source.anchors_hash != expected_anchors_hash:
                    findings.append(('candidate_source', source.id))
    for observation_id, rows in coverage_by_observation.items():
        outcome = rows[0].outcome
        source_rows = sources_by_observation.get(observation_id, [])
        if outcome == DistillationCoverageOutcome.SIGNAL and not source_rows:
            findings.append(('window', window.id))
        if outcome == DistillationCoverageOutcome.NO_SIGNAL and source_rows:
            findings.append(('window', window.id))

    return findings


def _evaluate_p4(project: Project, as_of: datetime) -> InvariantResult:
    expired_leases = WorkflowWork.objects.filter(
        organization_id=project.organization_id,
        project_id=project.id,
        execution_state=WorkflowWorkExecutionState.LEASED,
        lease_expires_at__lt=as_of,
    )
    violation_count = expired_leases.count()
    if not violation_count:
        return InvariantResult(
            invariant_id=InvariantId.P4,
            state=InvariantState.HEALTHY,
            reason='no_expired_work_leases',
            violation_count=0,
            target_checkpoint='CP2',
        )

    return InvariantResult(
        invariant_id=InvariantId.P4,
        state=InvariantState.VIOLATED,
        reason='expired_work_lease_unreclaimed',
        violation_count=violation_count,
        sample_ids=_query_samples(expired_leases, 'workflow_work'),
        target_checkpoint='CP2',
    )


def _evaluate_p6(project: Project, as_of: datetime) -> InvariantResult:
    proposed_candidates = MemoryCandidate.objects.filter(
        organization_id=project.organization_id,
        project_id=project.id,
        status=CandidateStatus.PROPOSED,
    )
    if candidate_work_reconciler.get_candidate_decision_work_builder() is None:
        return _missing(
            InvariantId.P6,
            proxy_count=proposed_candidates.count(),
            sample_ids=_query_samples(proposed_candidates, 'candidate'),
        )

    findings = candidate_work_reconciler.inspect_candidate_work(
        organization_id=project.organization_id,
        project_id=project.id,
        as_of=as_of,
    )
    sample_ids = tuple(
        sorted(
            {f'candidate:{finding.entity_id}' for finding in findings},
            key=_sample_sort_key,
        )[:_SAMPLE_LIMIT]
    )

    return _missing(
        InvariantId.P6,
        proxy_count=len(findings),
        sample_ids=sample_ids,
    )


def _p7_memory_projection_querysets(
    organization_id: uuid.UUID,
    project_id: uuid.UUID,
) -> tuple[QuerySet, QuerySet, QuerySet]:
    current_versions = MemoryVersion.objects.filter(
        organization_id=organization_id,
        project_id=project_id,
        memory__organization_id=organization_id,
        memory__project_id=project_id,
        memory_id=OuterRef('id'),
        version=OuterRef('current_version'),
    )
    matching_current_bodies = current_versions.filter(body=OuterRef('body'))
    consistent_documents = (
        RetrievalDocument.objects.filter(
            organization_id=organization_id,
            project_id=project_id,
            memory__organization_id=organization_id,
            memory__project_id=project_id,
            memory_version__organization_id=organization_id,
            memory_version__project_id=project_id,
            memory_id=OuterRef('id'),
            memory_version__memory_id=OuterRef('id'),
            memory_version__version=OuterRef('current_version'),
            visibility_scope=OuterRef('visibility_scope'),
            stale=OuterRef('stale'),
            refuted=OuterRef('refuted'),
        )
        .annotate(
            team_key=Coalesce(
                Cast('team_id', output_field=CharField()),
                Value(''),
            ),
        )
        .filter(team_key=OuterRef('team_key'))
    )
    memories = (
        Memory.objects.filter(
            organization_id=organization_id,
            project_id=project_id,
        )
        .annotate(
            team_key=Coalesce(
                Cast('team_id', output_field=CharField()),
                Value(''),
            ),
        )
        .annotate(
            has_current_version=Exists(current_versions),
            has_matching_current_body=Exists(matching_current_bodies),
            has_consistent_document=Exists(consistent_documents),
        )
    )
    missing_current_versions = memories.filter(has_current_version=False)
    mismatched_current_bodies = memories.filter(
        has_current_version=True,
        has_matching_current_body=False,
    )
    missing_or_inconsistent_documents = memories.filter(
        has_current_version=True,
        has_consistent_document=False,
    )

    return missing_current_versions, mismatched_current_bodies, missing_or_inconsistent_documents


def projection_inconsistency_memory_ids(
    *,
    organization_id: uuid.UUID,
    project_id: uuid.UUID,
) -> tuple[uuid.UUID, ...]:
    missing_current_versions, mismatched_current_bodies, missing_or_inconsistent_documents = (
        _p7_memory_projection_querysets(organization_id, project_id)
    )
    memory_ids: set[uuid.UUID] = set()
    for queryset in (missing_current_versions, mismatched_current_bodies, missing_or_inconsistent_documents):
        memory_ids.update(queryset.values_list('id', flat=True))

    return tuple(sorted(memory_ids, key=lambda memory_id: memory_id.int))


def _p7_v1_candidate_violations(project: Project) -> list[str]:
    violations: list[str] = []
    promoted_candidates = MemoryCandidate.objects.filter(
        organization_id=project.organization_id,
        project_id=project.id,
        status=CandidateStatus.PROMOTED,
    ).only('id', 'promoted_memory_id', 'decision_work_contract_version')
    for candidate in promoted_candidates:
        candidate_sample = f'candidate:{candidate.id}'
        if candidate.decision_work_contract_version == 0:
            continue
        if candidate.promoted_memory_id is None:
            violations.append(candidate_sample)
            continue
        if not Memory.objects.filter(
            id=candidate.promoted_memory_id,
            organization_id=project.organization_id,
            project_id=project.id,
        ).exists():
            violations.append(candidate_sample)
            continue
        transitions = MemoryTransition.objects.filter(
            organization_id=project.organization_id,
            project_id=project.id,
            candidate_id=candidate.id,
            transition_type__in=(
                MemoryTransitionType.PROMOTE,
                MemoryTransitionType.CONFLICT_RESOLVE,
                MemoryTransitionType.MERGE,
                MemoryTransitionType.REVISE,
                MemoryTransitionType.SUPERSEDE,
            ),
        )
        if transitions.count() != 1:
            violations.append(candidate_sample)
            continue
        transition = transitions.order_by('-created_at', '-id').first()
        decision_work_ok = WorkflowWork.objects.filter(
            organization_id=project.organization_id,
            project_id=project.id,
            work_type='candidate_decision',
            subject_id=candidate.id,
            disposition__in=('complete', 'no_op'),
            resolved_at__isnull=False,
        ).exists()
        import_only = False
        import_candidate_transition_types: tuple[str, ...] | None = None
        if candidate.decision_work_contract_version == 1:
            import_sources = list(
                MemoryCandidateSource.objects.select_related('observation', 'import_source').filter(
                    candidate_id=candidate.id
                )
            )
            import_only = bool(import_sources) and all(source.source_kind == 'import' for source in import_sources)
            if import_only and not is_validated_import_candidate(candidate, sources=import_sources):
                violations.append(candidate_sample)
                continue
        if import_only:
            import_candidate_transition_types = tuple(
                MemoryTransition.objects.filter(
                    organization_id=project.organization_id,
                    project_id=project.id,
                    candidate_id=candidate.id,
                )
                .order_by('id')
                .values_list('transition_type', flat=True)
            )
            import_transition_ok = (
                transition is not None
                and transition.transition_type == MemoryTransitionType.PROMOTE
                and import_candidate_transition_types == (MemoryTransitionType.PROMOTE,)
            )
            decision_work_absent = not WorkflowWork.objects.filter(
                organization_id=project.organization_id,
                project_id=project.id,
                work_type=WorkflowWorkType.CANDIDATE_DECISION,
                subject_id=candidate.id,
            ).exists()
            transition_shape_ok = import_transition_ok and decision_work_absent
        else:
            transition_shape_ok = transition is not None and (
                transition.transition_type != MemoryTransitionType.PROMOTE or decision_work_ok
            )
        if (
            transition is None
            or transition.result_memory_id != candidate.promoted_memory_id
            or transition.candidate_id != candidate.id
            or not transition_shape_ok
        ):
            violations.append(candidate_sample)

    return violations


def _p7_v1_pointer_is_valid(memory: Memory, transition: MemoryTransition, version: MemoryVersion) -> bool:
    memory_side = transition.memory_id == memory.id and transition.to_version_id == version.id
    result_side = transition.result_memory_id == memory.id and transition.result_version_id == version.id
    source_memory = transition.memory
    result_memory = transition.result_memory
    result_version = transition.result_version
    return all(
        (
            transition.organization_id == memory.organization_id,
            transition.project_id == memory.project_id,
            transition.team_id == memory.team_id,
            source_memory.organization_id == memory.organization_id,
            source_memory.project_id == memory.project_id,
            source_memory.team_id == memory.team_id,
            version.organization_id == memory.organization_id,
            version.project_id == memory.project_id,
            version.memory_id == memory.id,
            result_memory.organization_id == memory.organization_id,
            result_memory.project_id == memory.project_id,
            result_memory.team_id == memory.team_id,
            result_version.organization_id == memory.organization_id,
            result_version.project_id == memory.project_id,
            result_version.memory_id == result_memory.id,
            memory_side or result_side,
            version.body == memory.body,
        ),
    )


def _p7_v1_document_is_valid(
    memory: Memory,
    transition: MemoryTransition,
    version: MemoryVersion,
    document: RetrievalDocument | None,
    sources: list[MemoryVersionSource],
) -> bool:
    if document is None:
        return False

    projection = build_exact_memory_projection(
        memory=memory,
        version=version,
        transition_id=transition.id,
        sources=sources,
    )
    values = projection.document_values
    embedding_present = bool(
        document.embedding_reference
        or document.embedding_vector
        or document.embedding_projection_hash
        or document.embedding_projected_at
    )
    embedding_ok = (
        (
            document.embedding_projection_hash == document.exact_projection_hash
            and bool(document.embedding_reference)
            and bool(document.embedding_vector)
            and document.embedding_projected_at is not None
        )
        if embedding_present
        else (
            document.embedding_reference == ''
            and document.embedding_vector == []
            and document.embedding_projection_hash == ''
            and document.embedding_projected_at is None
        )
    )

    return all(
        (
            document.projection_contract_version == 1,
            document.exact_projection_hash == projection.exact_projection_hash,
            document.memory_id == memory.id,
            document.memory_version_id == version.id,
            document.organization_id == memory.organization_id,
            document.project_id == memory.project_id,
            document.team_id == memory.team_id,
            document.id in (transition.exact_document_id, transition.result_exact_document_id),
            document.visibility_scope == memory.visibility_scope,
            document.stale == memory.stale,
            document.refuted == memory.refuted,
            document.source_observation_ids == values['source_observation_ids'],
            document.file_paths == values['file_paths'],
            document.symbols == values['symbols'],
            document.exact_terms == values['exact_terms'],
            document.full_text == values['full_text'],
            document.metadata == {'projection': values},
            embedding_ok,
        ),
    )


def _p7_v1_audit_is_valid(memory: Memory, transition: MemoryTransition, document: RetrievalDocument) -> bool:
    if not transition.audit_event_id:
        return False

    return AuditEvent.objects.filter(
        id=transition.audit_event_id,
        event_type='MemoryTransitionCommitted',
        organization_id=memory.organization_id,
        project_id=memory.project_id,
        metadata__schema='memory_transition/v1',
        metadata__transition_id=str(transition.id),
        metadata__exact_document_id=str(document.id),
        metadata__exact_projection_hash=document.exact_projection_hash,
        metadata__provenance_hash=transition.provenance_hash,
    ).exists()


def _p7_v1_work_is_valid(memory: Memory, transition: MemoryTransition, document: RetrievalDocument | None) -> bool:
    if document is None:
        return False
    embedding_complete = (
        bool(document.embedding_reference)
        and bool(document.embedding_vector)
        and bool(document.embedding_projection_hash)
        and document.embedding_projection_hash == document.exact_projection_hash
        and document.embedding_projected_at is not None
    )
    if embedding_complete:
        return True
    if transition.embedding_work_id is None:
        return False
    expected_snapshot = {
        'schema': 'memory_embedding/v1',
        'retrieval_document_id': str(document.id),
        'memory_id': str(memory.id),
        'memory_version_id': str(document.memory_version_id),
        'exact_projection_hash': document.exact_projection_hash,
    }
    return WorkflowWork.objects.filter(
        id=transition.embedding_work_id,
        organization_id=memory.organization_id,
        project_id=memory.project_id,
        work_type='memory_embedding',
        subject_type='retrieval_document',
        subject_id=document.id,
        team_id=memory.team_id,
        input_snapshot=expected_snapshot,
    ).exists()


def _p7_source_memory_scope_is_valid(memory: Memory, source: Memory) -> bool:
    if memory.kind != 'digest':
        return source.team_id == memory.team_id
    if memory.visibility_scope == VisibilityScope.PROJECT:
        return source.visibility_scope == VisibilityScope.PROJECT
    return memory.visibility_scope == VisibilityScope.TEAM and (
        source.visibility_scope == VisibilityScope.PROJECT
        or (source.visibility_scope == VisibilityScope.TEAM and source.team_id == memory.team_id)
    )


def _p7_v1_sources_are_valid(memory: Memory, version: MemoryVersion, sources: list[MemoryVersionSource]) -> bool:
    if not sources:
        return False
    for source in sources:
        if (
            source.organization_id != memory.organization_id
            or source.project_id != memory.project_id
            or source.team_id != memory.team_id
            or source.memory_version_id != version.id
        ):
            return False
        if source.candidate_source_id is not None:
            candidate_source = source.candidate_source
            if (
                candidate_source.organization_id != memory.organization_id
                or candidate_source.project_id != memory.project_id
                or candidate_source.team_id != memory.team_id
                or source.source_content_hash != candidate_source.anchors_hash
            ):
                return False
        elif source.source_memory_version_id is not None:
            source_memory_version = source.source_memory_version
            source_memory = source_memory_version.memory
            if (
                source_memory_version.organization_id != memory.organization_id
                or source_memory_version.project_id != memory.project_id
                or source_memory.organization_id != memory.organization_id
                or source_memory.project_id != memory.project_id
                or not _p7_source_memory_scope_is_valid(memory, source_memory)
                or source.source_content_hash != source_memory_version.content_hash
            ):
                return False
        else:
            return False
    return True


def _p7_v1_memory_is_coherent(memory: Memory) -> bool:
    if memory.transition_contract_version != 1 or not memory.current_transition_id:
        return False

    transition = MemoryTransition.objects.filter(id=memory.current_transition_id).first()
    version = MemoryVersion.objects.filter(memory_id=memory.id, version=memory.current_version).first()
    if transition is None or version is None:
        return False

    valid_pointer = _p7_v1_pointer_is_valid(memory, transition, version)
    sources = canonical_memory_version_sources(
        list(
            MemoryVersionSource.objects.filter(memory_version_id=version.id)
            .select_related('candidate_source', 'source_memory_version')
            .order_by('id')
        ),
    )
    source_ok = _p7_v1_sources_are_valid(memory, version, sources)
    provenance_ok = transition.provenance_hash == memory_version_provenance_hash(sources)
    pointer_document_id = (
        transition.result_exact_document_id
        if transition.result_memory_id == memory.id and transition.result_version_id == version.id
        else transition.exact_document_id
    )
    document = RetrievalDocument.objects.filter(id=pointer_document_id, memory_version_id=version.id).first()
    document_ok = _p7_v1_document_is_valid(memory, transition, version, document, sources)
    audit_ok = document is not None and _p7_v1_audit_is_valid(memory, transition, document)
    inactive = bool(
        memory.stale
        or memory.refuted
        or memory.status in (MemoryStatus.ARCHIVED, MemoryStatus.REFUTED)
        or (document is not None and (document.stale or document.refuted))
    )
    work_ok = inactive or _p7_v1_work_is_valid(memory, transition, document)

    return valid_pointer and source_ok and provenance_ok and document_ok and audit_ok and work_ok


def _p7_v1_memory_violations(
    memories: list[Memory],
) -> list[str]:
    violations: list[str] = []
    for memory in memories:
        if memory.transition_contract_version == 0:
            continue
        if not _p7_v1_memory_is_coherent(memory):
            violations.append(f'memory:{memory.id}')

    return violations


def _p7_v1_legacy_result(project: Project) -> InvariantResult:
    scoped_promoted_memory = Memory.objects.filter(
        organization_id=project.organization_id,
        project_id=project.id,
        id=OuterRef('promoted_memory_id'),
    )
    legacy_orphans = (
        MemoryCandidate.objects.filter(
            organization_id=project.organization_id,
            project_id=project.id,
            status=CandidateStatus.PROMOTED,
        )
        .annotate(has_scoped_promoted_memory=Exists(scoped_promoted_memory))
        .filter(
            has_scoped_promoted_memory=False,
        )
    )
    missing_versions, mismatched_bodies, bad_documents = _p7_memory_projection_querysets(
        organization_id=project.organization_id,
        project_id=project.id,
    )
    legacy_violations = (
        legacy_orphans.count() + missing_versions.count() + mismatched_bodies.count() + bad_documents.count()
    )
    if legacy_violations:
        legacy_samples = _merge_samples(
            _query_samples(legacy_orphans, 'candidate'),
            _query_samples(missing_versions, 'memory'),
            _query_samples(mismatched_bodies, 'memory'),
            _query_samples(bad_documents, 'memory'),
        )
        return InvariantResult(
            invariant_id=InvariantId.P7,
            state=InvariantState.VIOLATED,
            reason='promotion_chain_inconsistent',
            violation_count=legacy_violations,
            sample_ids=legacy_samples,
            missing_evidence='relational promotion provenance and transition audit identity',
            target_checkpoint='CP4',
        )

    return InvariantResult(
        invariant_id=InvariantId.P7,
        state=InvariantState.MISSING_OBSERVABILITY,
        reason='legacy_transition_observability_missing',
        violation_count=0,
        missing_evidence='immutable transition history and authoritative current pointer',
        target_checkpoint='CP4',
    )


def _evaluate_p7_v1(project: Project) -> InvariantResult:
    memories = list(
        Memory.objects.filter(
            organization_id=project.organization_id,
            project_id=project.id,
        ).only('id', 'transition_contract_version', 'current_transition_id', 'current_version'),
    )
    legacy_count = sum(1 for memory in memories if memory.transition_contract_version == 0)
    has_legacy_promoted_candidate = MemoryCandidate.objects.filter(
        organization_id=project.organization_id,
        project_id=project.id,
        status=CandidateStatus.PROMOTED,
        decision_work_contract_version=0,
    ).exists()
    violations = _p7_v1_candidate_violations(project)
    violations.extend(_p7_v1_memory_violations(memories))
    if not violations and (legacy_count or has_legacy_promoted_candidate):
        return _p7_v1_legacy_result(project)
    if not violations and not memories:
        return _missing(InvariantId.P7, violation_count=0)
    if not violations:
        return InvariantResult(
            invariant_id=InvariantId.P7,
            state=InvariantState.HEALTHY,
            reason='promotion_chain_coherent',
            violation_count=0,
            target_checkpoint='CP4',
        )
    sample_ids = tuple(sorted(set(violations))[:_SAMPLE_LIMIT])
    return InvariantResult(
        invariant_id=InvariantId.P7,
        state=InvariantState.VIOLATED,
        reason='promotion_chain_inconsistent',
        violation_count=len(violations),
        sample_ids=sample_ids,
        missing_evidence='relational promotion provenance and transition audit identity',
        target_checkpoint='CP4',
    )


def _evaluate_p7(project: Project) -> InvariantResult:
    return _evaluate_p7_v1(project)


@dataclass(frozen=True, slots=True)
class _P8TransitionShape:
    changed_roles: tuple[str, ...]


_P8_STATE_TYPES = {
    MemoryTransitionType.MARK_STALE,
    MemoryTransitionType.REFUTE,
    MemoryTransitionType.RESTORE,
    MemoryTransitionType.ARCHIVE,
}
_P8_RESOLUTIONS = {
    MemoryConflictResolution.PUBLISH_CANDIDATE,
    MemoryConflictResolution.MERGE_CANDIDATE,
    MemoryConflictResolution.SUPERSEDE_MEMORY,
    MemoryConflictResolution.REJECT_CANDIDATE,
}
_P8_HEX_FIELDS = ('request_fingerprint', 'provenance_hash')


def _p8_scope_matches(obj: object, project: Project, team_id: uuid.UUID | None = None) -> bool:
    return (
        getattr(obj, 'organization_id', None) == project.organization_id
        and getattr(obj, 'project_id', None) == project.id
        and (team_id is None or getattr(obj, 'team_id', None) == team_id)
    )


def _p8_sources_for(version: MemoryVersion) -> list[MemoryVersionSource]:
    return canonical_memory_version_sources(
        list(
            MemoryVersionSource.objects.filter(memory_version_id=version.id)
            .select_related('candidate_source', 'source_memory_version', 'source_memory_version__memory')
            .order_by('id')
        ),
    )


def _p8_hash_is_valid(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and value == value.lower()
        and all(character in '0123456789abcdef' for character in value)
    )


def _p8_transition_shape(transition: MemoryTransition) -> _P8TransitionShape | None:  # noqa: C901
    same_memory = transition.memory_id == transition.result_memory_id
    same_result_version = transition.to_version_id == transition.result_version_id
    has_from = transition.from_version_id is not None
    candidate = transition.candidate_id is not None
    transition_type = transition.transition_type

    if transition_type == MemoryTransitionType.PROMOTE:
        valid = candidate and same_memory and not has_from and same_result_version
        return _P8TransitionShape(('result',)) if valid else None
    if transition_type == MemoryTransitionType.ATTACH_SOURCE:
        valid = candidate and same_memory and has_from and transition.from_version_id == transition.to_version_id
        valid = valid and same_result_version
        return _P8TransitionShape(('result',)) if valid else None
    if transition_type == MemoryTransitionType.PUBLISH_DIGEST:
        valid = not candidate and same_memory and not has_from and same_result_version
        return _P8TransitionShape(('result',)) if valid else None
    if transition_type == MemoryTransitionType.REVISE:
        valid = (
            same_memory and has_from and transition.from_version_id != transition.to_version_id and same_result_version
        )
        return _P8TransitionShape(('result',)) if valid else None
    if transition_type == MemoryTransitionType.MERGE:
        if candidate:
            valid = (
                same_memory
                and has_from
                and transition.from_version_id != transition.to_version_id
                and same_result_version
            )
            return _P8TransitionShape(('result',)) if valid else None
        valid = not same_memory and has_from and transition.from_version_id == transition.to_version_id
        return _P8TransitionShape(('affected', 'result')) if valid else None
    if transition_type == MemoryTransitionType.SUPERSEDE:
        valid = not same_memory and has_from and transition.from_version_id == transition.to_version_id
        return (
            _P8TransitionShape(('affected', 'result'))
            if valid and candidate
            else _P8TransitionShape(('affected',))
            if valid
            else None
        )
    if transition_type in _P8_STATE_TYPES:
        valid = (
            not candidate
            and same_memory
            and has_from
            and transition.from_version_id == transition.to_version_id
            and same_result_version
        )
        return _P8TransitionShape(('result',)) if valid else None
    if transition_type == MemoryTransitionType.CONFLICT_OPEN:
        valid = (
            candidate
            and same_memory
            and has_from
            and transition.from_version_id == transition.to_version_id
            and same_result_version
        )
        return _P8TransitionShape(()) if valid else None
    if transition_type == MemoryTransitionType.CONFLICT_RESOLVE:
        metadata = transition.audit_event.metadata if transition.audit_event is not None else {}
        resolution = metadata.get('resolution') if isinstance(metadata, dict) else None
        if not candidate or resolution not in _P8_RESOLUTIONS or not has_from:
            return None
        if resolution == MemoryConflictResolution.REJECT_CANDIDATE:
            valid = same_memory and transition.from_version_id == transition.to_version_id and same_result_version
            return _P8TransitionShape(()) if valid else None
        if resolution == MemoryConflictResolution.PUBLISH_CANDIDATE:
            valid = not same_memory and transition.from_version_id == transition.to_version_id
            return _P8TransitionShape(('result',)) if valid else None
        if resolution == MemoryConflictResolution.MERGE_CANDIDATE:
            valid = same_memory and transition.from_version_id != transition.to_version_id and same_result_version
            return _P8TransitionShape(('result',)) if valid else None
        valid = not same_memory and transition.from_version_id == transition.to_version_id
        return _P8TransitionShape(('affected', 'result')) if valid else None
    return None


def _p8_candidate_shape_valid(transition: MemoryTransition, project: Project) -> bool:
    candidate_required = transition.transition_type in {
        MemoryTransitionType.PROMOTE,
        MemoryTransitionType.ATTACH_SOURCE,
        MemoryTransitionType.CONFLICT_OPEN,
        MemoryTransitionType.CONFLICT_RESOLVE,
    }
    candidate_forbidden = transition.transition_type in {
        MemoryTransitionType.PUBLISH_DIGEST,
        *tuple(_P8_STATE_TYPES),
    }
    if candidate_required and transition.candidate_id is None:
        return False
    if candidate_forbidden and transition.candidate_id is not None:
        return False
    if transition.candidate is None:
        return not candidate_required
    return _p8_scope_matches(transition.candidate, project) and transition.candidate.team_id == transition.team_id


def _p8_document_is_valid(
    *,
    memory: Memory,
    version: MemoryVersion,
    document: RetrievalDocument | None,
    transition_id: uuid.UUID,
    sources: list[MemoryVersionSource],
) -> bool:
    if document is None or not _p7_v1_sources_are_valid(memory, version, sources):
        return False
    try:
        projection = build_exact_memory_projection(
            memory=memory,
            version=version,
            transition_id=transition_id,
            sources=sources,
        )
    except (TypeError, ValueError):
        return False
    values = projection.document_values
    return all(
        (
            document.projection_contract_version == 1,
            document.organization_id == memory.organization_id,
            document.project_id == memory.project_id,
            document.team_id == memory.team_id,
            document.memory_id == memory.id,
            document.memory_version_id == version.id,
            document.exact_projection_hash == projection.exact_projection_hash,
            document.visibility_scope == memory.visibility_scope,
            document.stale == memory.stale,
            document.refuted == memory.refuted,
            document.source_observation_ids == values['source_observation_ids'],
            document.file_paths == values['file_paths'],
            document.symbols == values['symbols'],
            document.exact_terms == values['exact_terms'],
            document.full_text == values['full_text'],
            document.metadata == {'projection': values},
        ),
    )


def _p8_document_identity_is_valid(
    *,
    memory: Memory,
    version: MemoryVersion,
    document: RetrievalDocument | None,
    project: Project,
) -> bool:
    return bool(
        document is not None
        and _p8_scope_matches(document, project)
        and document.team_id == memory.team_id
        and document.memory_id == memory.id
        and document.memory_version_id == version.id
        and version.memory_id == memory.id
        and version.version > 0
        and _p8_hash_is_valid(version.content_hash)
        and document.projection_contract_version == 1
        and _p8_hash_is_valid(document.exact_projection_hash)
    )


def _p8_audit_is_valid(
    transition: MemoryTransition,
    *,
    project: Project,
    memory: Memory,
    document: RetrievalDocument | None,
    role: str,
    current: bool = False,
) -> bool:
    audit = transition.audit_event
    if audit is None or not _p8_scope_matches(audit, project):
        return False
    metadata = audit.metadata if isinstance(audit.metadata, dict) else {}
    if not _p8_audit_core_is_valid(audit, metadata, transition, project, memory):
        return False
    if not _p8_audit_relations_are_valid(metadata, transition):
        return False
    return _p8_audit_document_is_valid(metadata, transition, document, role, current=current)


def _p8_audit_core_is_valid(
    audit: AuditEvent,
    metadata: dict[str, object],
    transition: MemoryTransition,
    project: Project,
    memory: Memory,
) -> bool:
    expected_scope = {
        'organization_id': str(project.organization_id),
        'project_id': str(project.id),
        'team_id': str(transition.team_id) if transition.team_id else None,
    }
    return not (
        audit.event_type != 'MemoryTransitionCommitted'
        or audit.target_type != 'memory'
        or audit.target_id != str(transition.memory_id)
        or audit.result != 'recorded'
        or transition.audit_event_id != audit.id
        or metadata.get('schema') != 'memory_transition/v1'
        or metadata.get('transition_id') != str(transition.id)
        or metadata.get('transition_type') != transition.transition_type
        or metadata.get('scope_filters') != expected_scope
        or metadata.get('request_fingerprint') != transition.request_fingerprint
        or metadata.get('provenance_hash') != transition.provenance_hash
        or metadata.get('memory_id') != str(transition.memory_id)
        or any(not _p8_hash_is_valid(getattr(transition, field)) for field in _P8_HEX_FIELDS)
        or not _p8_hash_is_valid(metadata.get('request_fingerprint'))
        or not _p8_hash_is_valid(metadata.get('provenance_hash'))
        or audit.organization_id != memory.organization_id
        or audit.project_id != memory.project_id
        or audit.team_id != transition.team_id
        or audit.team_id != memory.team_id
    )


def _p8_audit_relations_are_valid(metadata: dict[str, object], transition: MemoryTransition) -> bool:
    optional_fields = (
        ('candidate_id', transition.candidate_id),
        ('semantic_link_id', transition.semantic_link_id),
        ('from_version_id', transition.from_version_id),
        ('work_id', transition.embedding_work_id),
    )
    relations_valid = all(
        (key not in metadata if expected is None else metadata.get(key) == str(expected))
        for key, expected in optional_fields
    )
    if not relations_valid:
        return False
    if transition.transition_type in (MemoryTransitionType.PROMOTE, MemoryTransitionType.ATTACH_SOURCE):
        return metadata.get('version_id') == str(transition.to_version_id)
    return all(
        metadata.get(key) == str(expected)
        for key, expected in (
            ('to_version_id', transition.to_version_id),
            ('result_memory_id', transition.result_memory_id),
            ('result_version_id', transition.result_version_id),
        )
    )


def _p8_audit_document_is_valid(
    metadata: dict[str, object],
    transition: MemoryTransition,
    document: RetrievalDocument | None,
    role: str,
    *,
    current: bool = False,
) -> bool:
    if document is None or not _p8_hash_is_valid(document.exact_projection_hash):
        return False
    if role == 'exact':
        exact_id_key, exact_hash_key = 'exact_document_id', 'exact_projection_hash'
        version_key = 'version_id' if metadata.get('version_id') else 'to_version_id'
        expected_version = transition.to_version_id
    else:
        if transition.transition_type in (MemoryTransitionType.PROMOTE, MemoryTransitionType.ATTACH_SOURCE):
            exact_id_key, exact_hash_key = 'exact_document_id', 'exact_projection_hash'
        else:
            exact_id_key, exact_hash_key = 'result_exact_document_id', 'result_exact_projection_hash'
        version_key = 'version_id' if metadata.get('version_id') else 'result_version_id'
        expected_version = transition.result_version_id
    if metadata.get(exact_id_key) != str(document.id) or metadata.get(version_key) != str(expected_version):
        return False
    if not _p8_hash_is_valid(metadata.get(exact_hash_key)):
        return False
    if current and metadata.get(exact_hash_key) != document.exact_projection_hash:
        return False
    if transition.transition_type not in (MemoryTransitionType.PROMOTE, MemoryTransitionType.ATTACH_SOURCE):
        for key, expected in (
            ('to_version_id', transition.to_version_id),
            ('result_memory_id', transition.result_memory_id),
            ('result_version_id', transition.result_version_id),
        ):
            if metadata.get(key) != str(expected):
                return False
    return True


def _p8_link_is_valid(
    transition: MemoryTransition,
    project: Project,
) -> bool:
    link = transition.semantic_link
    resolution = None
    if transition.audit_event is not None and isinstance(transition.audit_event.metadata, dict):
        resolution = transition.audit_event.metadata.get('resolution')
    required_type = None
    if transition.transition_type == MemoryTransitionType.CONFLICT_OPEN:
        required_type = LinkType.CONFLICTS_WITH
    elif transition.transition_type == MemoryTransitionType.MERGE and transition.candidate_id is None:
        required_type = LinkType.NARROWED_BY
    elif transition.transition_type == MemoryTransitionType.SUPERSEDE:
        required_type = LinkType.SUPERSEDED_BY
    elif (
        transition.transition_type == MemoryTransitionType.CONFLICT_RESOLVE
        and resolution == MemoryConflictResolution.SUPERSEDE_MEMORY
    ):
        required_type = LinkType.SUPERSEDED_BY
    if required_type is None:
        return link is None
    if link is None or not _p8_scope_matches(link, project) or link.memory_id != transition.memory_id:
        return False
    if link.link_type != required_type:
        return False
    if required_type == LinkType.CONFLICTS_WITH:
        return (
            transition.candidate_id is not None
            and link.target == f'{CONFLICT_CANDIDATE_TARGET_PREFIX}{transition.candidate_id}'
        )
    return link.target == str(transition.result_memory_id)


def _p8_transition_sources_valid(
    transition: MemoryTransition,
    memory: Memory,
    result_memory: Memory,
    to_version: MemoryVersion,
    result_version: MemoryVersion,
    project: Project,
) -> tuple[list[MemoryVersionSource], list[MemoryVersionSource]] | None:
    if (
        to_version.memory_id != memory.id
        or not _p8_scope_matches(to_version, project)
        or not _p8_scope_matches(result_version, project)
        or result_version.memory_id != result_memory.id
    ):
        return None
    to_sources = _p8_sources_for(to_version)
    result_sources = _p8_sources_for(result_version)
    if not to_sources or not result_sources:
        return None
    if (
        not _p8_source_rows_are_valid(memory, to_version, to_sources, project)
        or not _p8_source_rows_are_valid(result_memory, result_version, result_sources, project)
        or not _p8_hash_is_valid(transition.provenance_hash)
    ):
        return None
    return to_sources, result_sources


def _p8_source_rows_are_valid(
    memory: Memory,
    version: MemoryVersion,
    sources: list[MemoryVersionSource],
    project: Project,
) -> bool:
    if (
        not _p8_scope_matches(version, project)
        or version.memory_id != memory.id
        or not sources
        or not _p8_hash_is_valid(version.content_hash)
    ):
        return False
    for source in sources:
        if (
            not _p8_scope_matches(source, project, memory.team_id)
            or source.memory_version_id != version.id
            or not _p8_hash_is_valid(source.source_content_hash)
            or (source.candidate_source_id is None) == (source.source_memory_version_id is None)
        ):
            return False
        if source.candidate_source_id is not None:
            candidate_source = source.candidate_source
            if (
                candidate_source is None
                or not _p8_scope_matches(candidate_source, project, memory.team_id)
                or not _p8_hash_is_valid(candidate_source.anchors_hash)
                or source.source_content_hash != candidate_source.anchors_hash
            ):
                return False
        else:
            source_version = source.source_memory_version
            if (
                source_version is None
                or source_version.id == version.id
                or not _p8_scope_matches(source_version, project)
                or source_version.memory.team_id != memory.team_id
                or not _p8_hash_is_valid(source_version.content_hash)
                or source.source_content_hash != source_version.content_hash
            ):
                return False
    return True


def _p8_transition_projections_valid(
    transition: MemoryTransition,
    project: Project,
    memory: Memory,
    result_memory: Memory,
    to_version: MemoryVersion,
    result_version: MemoryVersion,
    to_sources: list[MemoryVersionSource],
    result_sources: list[MemoryVersionSource],
) -> bool:
    shape = _p8_transition_shape(transition)
    if shape is None or not _p8_link_is_valid(transition, project):
        return False
    if not _p8_audit_is_valid(
        transition, project=project, memory=memory, document=transition.exact_document, role='exact'
    ):
        return False
    if not _p8_audit_is_valid(
        transition, project=project, memory=result_memory, document=transition.result_exact_document, role='result'
    ):
        return False
    return _p8_document_identity_is_valid(
        memory=memory,
        version=to_version,
        document=transition.exact_document,
        project=project,
    ) and _p8_document_identity_is_valid(
        memory=result_memory,
        version=result_version,
        document=transition.result_exact_document,
        project=project,
    )


def _p8_transition_violation(
    transition: MemoryTransition,
    project: Project,
    owned_memory_ids: set[uuid.UUID],
    owned_version_ids: set[uuid.UUID],
) -> bool:
    memory = transition.memory
    result_memory = transition.result_memory
    from_version = transition.from_version
    to_version = transition.to_version
    result_version = transition.result_version
    targeted_owned = bool({transition.memory_id, transition.result_memory_id} & owned_memory_ids)
    targeted_owned = targeted_owned or bool(
        {transition.from_version_id, transition.to_version_id, transition.result_version_id} & owned_version_ids,
    )
    if not targeted_owned:
        return False
    if not _p8_scope_matches(transition, project):
        return True
    if (
        memory is None
        or result_memory is None
        or not _p8_scope_matches(memory, project)
        or not _p8_scope_matches(result_memory, project)
    ):
        return True
    if transition.team_id != memory.team_id or transition.team_id != result_memory.team_id:
        return True
    if not _p8_candidate_shape_valid(transition, project):
        return True
    shape = _p8_transition_shape(transition)
    if shape is None:
        return True
    if from_version is not None and (
        from_version.memory_id != memory.id or not _p8_scope_matches(from_version, project)
    ):
        return True
    source_rows = _p8_transition_sources_valid(
        transition,
        memory,
        result_memory,
        to_version,
        result_version,
        project,
    )
    if source_rows is None:
        return True
    to_sources, result_sources = source_rows
    if not _p8_transition_projections_valid(
        transition,
        project,
        memory,
        result_memory,
        to_version,
        result_version,
        to_sources,
        result_sources,
    ):
        return True
    return False


def _p8_latest_pointer_violation(
    transition: MemoryTransition,
    project: Project,
    shape: _P8TransitionShape,
    latest_by_memory: dict[uuid.UUID, MemoryTransition],
) -> bool:
    result_is_latest = (
        'result' in shape.changed_roles and latest_by_memory.get(transition.result_memory_id) is transition
    )
    if result_is_latest:
        result_sources = _p8_sources_for(transition.result_version)
        if transition.provenance_hash != memory_version_provenance_hash(result_sources):
            return True
    for role in shape.changed_roles:
        memory_id = transition.memory_id if role == 'affected' else transition.result_memory_id
        if latest_by_memory.get(memory_id) is not transition:
            continue
        memory = transition.memory if role == 'affected' else transition.result_memory
        version = transition.to_version if role == 'affected' else transition.result_version
        document = transition.exact_document if role == 'affected' else transition.result_exact_document
        sources = _p8_sources_for(version)
        if (
            memory.transition_contract_version != 1
            or memory.current_transition_id != transition.id
            or memory.current_version != version.version
            or not _p8_document_is_valid(
                memory=memory,
                version=version,
                document=document,
                transition_id=transition.id,
                sources=sources,
            )
            or not _p8_audit_is_valid(
                transition,
                project=project,
                memory=memory,
                document=document,
                role='exact' if role == 'affected' else 'result',
                current=True,
            )
        ):
            return True
    return False


def _evaluate_p8(project: Project) -> InvariantResult:  # noqa: C901
    owned_memories = list(Memory.objects.filter(organization_id=project.organization_id, project_id=project.id))
    owned_memory_ids = {memory.id for memory in owned_memories if memory.transition_contract_version == 1}
    owned_versions = set(MemoryVersion.objects.filter(memory_id__in=owned_memory_ids).values_list('id', flat=True))
    relevant = (
        MemoryTransition.objects.filter(
            Q(organization_id=project.organization_id, project_id=project.id)
            | Q(memory_id__in=owned_memory_ids)
            | Q(result_memory_id__in=owned_memory_ids)
            | Q(from_version_id__in=owned_versions)
            | Q(to_version_id__in=owned_versions)
            | Q(result_version_id__in=owned_versions),
        )
        .select_related(
            'memory',
            'result_memory',
            'from_version',
            'to_version',
            'result_version',
            'exact_document',
            'result_exact_document',
            'semantic_link',
            'audit_event',
            'candidate',
        )
        .order_by('created_at', 'id')
    )
    transitions = list(relevant)
    violations: list[str] = []
    latest_by_memory: dict[uuid.UUID, MemoryTransition] = {}
    shapes: dict[uuid.UUID, _P8TransitionShape | None] = {}
    for transition in transitions:
        shape = _p8_transition_shape(transition)
        shapes[transition.id] = shape
        if shape is not None:
            for role in shape.changed_roles:
                memory_id = transition.memory_id if role == 'affected' else transition.result_memory_id
                if memory_id not in owned_memory_ids:
                    continue
                current = latest_by_memory.get(memory_id)
                if current is None or (transition.created_at, transition.id.int) > (current.created_at, current.id.int):
                    latest_by_memory[memory_id] = transition
    for transition in transitions:
        shape = shapes[transition.id]
        transition_violation = _p8_transition_violation(transition, project, owned_memory_ids, owned_versions)
        if not transition_violation and shape is not None:
            transition_violation = _p8_latest_pointer_violation(transition, project, shape, latest_by_memory)
        if not transition_violation:
            continue
        targets = {transition.memory_id, transition.result_memory_id} & owned_memory_ids
        if not targets:
            targets = set(
                MemoryVersion.objects.filter(
                    id__in={transition.from_version_id, transition.to_version_id, transition.result_version_id},
                    memory_id__in=owned_memory_ids,
                ).values_list('memory_id', flat=True),
            )
        violations.extend(f'memory:{target_id}' for target_id in sorted(targets, key=lambda value: value.int)[:1])
    legacy = any(memory.transition_contract_version == 0 for memory in owned_memories)
    if violations:
        return InvariantResult(
            invariant_id=InvariantId.P8,
            state=InvariantState.VIOLATED,
            reason='memory_transition_history_invalid',
            violation_count=len(violations),
            sample_ids=tuple(sorted(set(violations), key=_sample_sort_key)[:_SAMPLE_LIMIT]),
            target_checkpoint='CP4',
        )
    if legacy or not owned_memory_ids:
        return _missing(InvariantId.P8)
    return InvariantResult(
        invariant_id=InvariantId.P8,
        state=InvariantState.HEALTHY,
        reason='memory_transition_history_coherent',
        violation_count=0,
        target_checkpoint='CP4',
    )


def _p9_conflict_check(
    conflict: MemoryConflict,
    project: Project,
) -> tuple[str, uuid.UUID | None, bool]:
    sample = f'conflict:{conflict.id}'
    if (
        not _p8_scope_matches(conflict, project)
        or not _p8_scope_matches(conflict.candidate, project)
        or not _p8_scope_matches(conflict.memory, project)
        or not _p8_scope_matches(conflict.memory_version, project)
        or conflict.candidate.team_id != conflict.team_id
        or conflict.memory.team_id != conflict.team_id
        or conflict.memory_version.memory_id != conflict.memory_id
        or conflict.memory.transition_contract_version != 1
        or not _p8_hash_is_valid(conflict.evidence_hash)
    ):
        return sample, None, True
    target = f'{CONFLICT_CANDIDATE_TARGET_PREFIX}{conflict.candidate_id}'
    candidate_links = list(
        MemoryLink.objects.filter(
            link_type=LinkType.CONFLICTS_WITH,
            memory_id=conflict.memory_id,
            target=target,
        ).order_by('id'),
    )
    same_links = [link for link in candidate_links if _p8_scope_matches(link, project)]
    if len(same_links) != 1 or conflict.semantic_link_id != same_links[0].id:
        return sample, None, True
    link = same_links[0]
    opened = conflict.opened_transition
    open_transitions = MemoryTransition.objects.filter(
        organization_id=project.organization_id,
        project_id=project.id,
        transition_type=MemoryTransitionType.CONFLICT_OPEN,
        semantic_link_id=link.id,
    )
    if (
        opened is None
        or open_transitions.count() != 1
        or opened.id != open_transitions.values_list('id', flat=True).first()
        or opened.team_id != conflict.team_id
        or opened.candidate_id != conflict.candidate_id
        or opened.memory_id != conflict.memory_id
        or opened.result_memory_id != conflict.memory_id
        or opened.from_version_id != conflict.memory_version_id
        or opened.to_version_id != conflict.memory_version_id
        or opened.result_version_id != conflict.memory_version_id
    ):
        return sample, link.id, True
    if conflict.resolved_transition_id is None:
        return (
            sample,
            link.id,
            bool(
                conflict.resolved_at is not None
                or conflict.resolution != ''
                or conflict.candidate.status != CandidateStatus.PROPOSED
            ),
        )
    return (
        sample,
        link.id,
        bool(
            conflict.resolved_transition is None
            or conflict.resolution not in MemoryConflictResolution.values
            or conflict.resolved_at is None
        ),
    )


def _p9_resolution_audit_is_valid(
    transition: MemoryTransition,
    project: Project,
    conflicts: list[MemoryConflict],
    resolution: str,
) -> bool:
    audit = transition.audit_event
    if audit is None or not _p8_scope_matches(audit, project, transition.team_id):
        return False
    metadata = audit.metadata if isinstance(audit.metadata, dict) else {}
    conflict_ids = ','.join(str(conflict.id) for conflict in sorted(conflicts, key=lambda row: str(row.id)))
    return (
        _p8_audit_core_is_valid(audit, metadata, transition, project, transition.memory)
        and _p8_audit_relations_are_valid(metadata, transition)
        and metadata.get('conflict_ids') == conflict_ids
        and metadata.get('resolution') == resolution
    )


def _p9_resolution_outcome_is_valid(  # noqa: C901
    conflicts: list[MemoryConflict],
    transition: MemoryTransition,
    resolution: str,
    project: Project,
) -> bool:
    candidate = conflicts[0].candidate
    by_memory = {conflict.memory_id: conflict for conflict in conflicts}
    first = min(conflicts, key=lambda conflict: str(conflict.id))
    result_version = transition.result_version
    if result_version.memory_id != transition.result_memory_id:
        return False

    rejected = resolution == MemoryConflictResolution.REJECT_CANDIDATE
    if rejected:
        if candidate.status != CandidateStatus.REJECTED or candidate.promoted_memory_id is not None:
            return False
    elif candidate.status != CandidateStatus.PROMOTED or candidate.promoted_memory_id != transition.result_memory_id:
        return False

    if resolution in (
        MemoryConflictResolution.REJECT_CANDIDATE,
        MemoryConflictResolution.PUBLISH_CANDIDATE,
    ):
        if (
            transition.memory_id != first.memory_id
            or transition.from_version_id != first.memory_version_id
            or transition.to_version_id != first.memory_version_id
        ):
            return False

    if resolution == MemoryConflictResolution.REJECT_CANDIDATE:
        return (
            transition.result_memory_id == first.memory_id
            and transition.result_version_id == first.memory_version_id
            and transition.semantic_link_id is None
        )
    if resolution == MemoryConflictResolution.PUBLISH_CANDIDATE:
        return (
            transition.result_memory_id not in by_memory
            and result_version.version == 1
            and transition.semantic_link_id is None
        )

    selected = by_memory.get(transition.memory_id)
    if selected is None or transition.from_version_id != selected.memory_version_id:
        return False
    if resolution == MemoryConflictResolution.MERGE_CANDIDATE:
        return (
            transition.result_memory_id == selected.memory_id
            and transition.to_version_id == transition.result_version_id
            and transition.result_version_id != selected.memory_version_id
            and result_version.version == selected.memory_version.version + 1
            and transition.semantic_link_id is None
        )
    if resolution != MemoryConflictResolution.SUPERSEDE_MEMORY:
        return False

    link = transition.semantic_link
    return bool(
        transition.to_version_id == selected.memory_version_id
        and transition.result_memory_id not in by_memory
        and result_version.version == 1
        and link is not None
        and _p8_scope_matches(link, project)
        and link.link_type == LinkType.SUPERSEDED_BY
        and link.memory_id == selected.memory_id
        and link.target == str(transition.result_memory_id)
    )


def _p9_conflict_group_is_valid(conflicts: list[MemoryConflict], project: Project) -> bool:
    candidate = conflicts[0].candidate
    conflict_ids = {conflict.id for conflict in conflicts}
    persisted_ids = set(
        MemoryConflict.objects.filter(candidate_id=candidate.id).values_list('id', flat=True),
    )
    if persisted_ids != conflict_ids:
        return False

    resolved_ids = {conflict.resolved_transition_id for conflict in conflicts}
    if resolved_ids == {None}:
        return candidate.status == CandidateStatus.PROPOSED and candidate.promoted_memory_id is None
    if None in resolved_ids or len(resolved_ids) != 1:
        return False

    resolutions = {conflict.resolution for conflict in conflicts}
    if len(resolutions) != 1:
        return False
    resolution = resolutions.pop()
    if resolution not in MemoryConflictResolution.values:
        return False

    transition = conflicts[0].resolved_transition
    if (
        transition is None
        or any(conflict.resolved_transition_id != transition.id for conflict in conflicts)
        or not _p8_scope_matches(transition, project, candidate.team_id)
        or transition.transition_type != MemoryTransitionType.CONFLICT_RESOLVE
        or transition.candidate_id != candidate.id
        or transition.team_id != candidate.team_id
    ):
        return False
    scoped_resolutions = set(
        MemoryTransition.objects.filter(
            organization_id=project.organization_id,
            project_id=project.id,
            transition_type=MemoryTransitionType.CONFLICT_RESOLVE,
            candidate_id=candidate.id,
        ).values_list('id', flat=True),
    )
    resolved_conflict_ids = set(
        MemoryConflict.objects.filter(resolved_transition_id=transition.id).values_list('id', flat=True),
    )
    return (
        scoped_resolutions == {transition.id}
        and resolved_conflict_ids == conflict_ids
        and _p9_resolution_audit_is_valid(transition, project, conflicts, resolution)
        and _p9_resolution_outcome_is_valid(conflicts, transition, resolution, project)
    )


def _p9_conflict_is_targeted(
    conflict: MemoryConflict,
    project: Project,
    owned_candidate_ids: set[uuid.UUID],
    owned_memory_ids: set[uuid.UUID],
    owned_version_ids: set[uuid.UUID],
) -> bool:
    return (
        _p8_scope_matches(conflict, project)
        or conflict.candidate_id in owned_candidate_ids
        or conflict.memory_id in owned_memory_ids
        or conflict.memory_version_id in owned_version_ids
    )


def _p9_link_violations(
    links: QuerySet,
    owned_memory_ids: set[uuid.UUID],
    project: Project,
    checked_link_ids: set[uuid.UUID],
) -> set[str]:
    violations: set[str] = set()
    for link in links:
        if link.memory_id not in owned_memory_ids or not _p8_scope_matches(link, project):
            if link.memory_id in owned_memory_ids:
                violations.add(f'memory:{link.memory_id}')
            continue
        if link.id not in checked_link_ids and link.memory.transition_contract_version == 1:
            violations.add(f'memory:{link.memory_id}')
    return violations


def _evaluate_p9(project: Project) -> InvariantResult:
    all_memories = list(Memory.objects.filter(organization_id=project.organization_id, project_id=project.id))
    owned_candidate_ids = set(
        MemoryCandidate.objects.filter(
            organization_id=project.organization_id,
            project_id=project.id,
        ).values_list('id', flat=True),
    )
    owned_memory_ids = {memory.id for memory in all_memories}
    v1_memory_ids = {memory.id for memory in all_memories if memory.transition_contract_version == 1}
    owned_version_ids = set(MemoryVersion.objects.filter(memory_id__in=v1_memory_ids).values_list('id', flat=True))
    links = (
        MemoryLink.objects.filter(
            Q(organization_id=project.organization_id, project_id=project.id) | Q(memory_id__in=owned_memory_ids),
            link_type=LinkType.CONFLICTS_WITH,
        )
        .select_related('memory')
        .order_by('id')
    )
    link_ids = set(links.values_list('id', flat=True))
    conflicts = (
        MemoryConflict.objects.filter(
            Q(organization_id=project.organization_id, project_id=project.id)
            | Q(candidate_id__in=owned_candidate_ids)
            | Q(memory_id__in=owned_memory_ids)
            | Q(memory_version_id__in=owned_version_ids)
            | Q(semantic_link_id__in=link_ids),
        )
        .select_related(
            'candidate',
            'memory',
            'memory_version',
            'semantic_link',
            'opened_transition',
            'resolved_transition',
            'resolved_transition__audit_event',
            'resolved_transition__memory',
            'resolved_transition__result_version',
            'resolved_transition__semantic_link',
        )
        .order_by('id')
    )
    violations: set[str] = set()
    checked_link_ids: set[uuid.UUID] = set()
    targeted: list[MemoryConflict] = []
    for conflict in conflicts:
        if not _p9_conflict_is_targeted(
            conflict,
            project,
            owned_candidate_ids,
            owned_memory_ids,
            owned_version_ids,
        ):
            continue
        targeted.append(conflict)
        sample, link_id, invalid = _p9_conflict_check(conflict, project)
        if link_id is not None:
            checked_link_ids.add(link_id)
        if invalid:
            violations.add(sample)
    conflicts_by_candidate: dict[uuid.UUID, list[MemoryConflict]] = defaultdict(list)
    for conflict in targeted:
        conflicts_by_candidate[conflict.candidate_id].append(conflict)
    for conflict_group in conflicts_by_candidate.values():
        if not _p9_conflict_group_is_valid(conflict_group, project):
            violations.update(f'conflict:{conflict.id}' for conflict in conflict_group)
    violations.update(_p9_link_violations(links, owned_memory_ids, project, checked_link_ids))
    if violations:
        return InvariantResult(
            invariant_id=InvariantId.P9,
            state=InvariantState.VIOLATED,
            reason='durable_conflict_evidence_invalid',
            violation_count=len(violations),
            sample_ids=tuple(sorted(violations, key=_sample_sort_key)[:_SAMPLE_LIMIT]),
            target_checkpoint='CP4/CP5',
        )
    has_v1_memory = Memory.objects.filter(
        organization_id=project.organization_id,
        project_id=project.id,
        transition_contract_version=1,
    ).exists()
    if not has_v1_memory:
        return _missing(InvariantId.P9)
    return InvariantResult(
        invariant_id=InvariantId.P9,
        state=InvariantState.HEALTHY,
        reason='durable_conflict_evidence_coherent',
        violation_count=0,
        target_checkpoint='CP4/CP5',
    )


def _reviewable_memory_filter() -> Q:
    return (
        Q(status__in=_REVIEW_MEMORY_STATUSES)
        | Q(
            status=MemoryStatus.APPROVED,
            confidence__lte=_REVIEW_MEMORY_CONFIDENCE_THRESHOLD,
        )
        | Q(status=MemoryStatus.APPROVED, refuted=True)
    )


def _evaluate_p12(project: Project) -> InvariantResult:
    scoped_conflict_links = MemoryLink.objects.filter(
        organization_id=project.organization_id,
        project_id=project.id,
        memory__organization_id=project.organization_id,
        memory__project_id=project.id,
        link_type=LinkType.CONFLICTS_WITH,
        target=Concat(
            Value(CONFLICT_CANDIDATE_TARGET_PREFIX),
            Cast(OuterRef('id'), output_field=CharField()),
        ),
    )
    ordinary_candidates = (
        MemoryCandidate.objects.filter(
            organization_id=project.organization_id,
            project_id=project.id,
            status=CandidateStatus.PROPOSED,
        )
        .annotate(has_conflict_link=Exists(scoped_conflict_links))
        .filter(has_conflict_link=False)
    )
    non_conflict_reviewable_memories = (
        Memory.objects.filter(
            organization_id=project.organization_id,
            project_id=project.id,
        )
        .filter(_reviewable_memory_filter())
        .exclude(status=MemoryStatus.CONFLICT)
    )
    violation_count = ordinary_candidates.count() + non_conflict_reviewable_memories.count()

    if not violation_count:
        return InvariantResult(
            invariant_id=InvariantId.P12,
            state=InvariantState.HEALTHY,
            reason='human_inbox_conflicts_only',
            violation_count=0,
            target_checkpoint='CP5',
        )

    return InvariantResult(
        invariant_id=InvariantId.P12,
        state=InvariantState.VIOLATED,
        reason='non_conflict_item_in_human_inbox',
        violation_count=violation_count,
        sample_ids=_merge_samples(
            _query_samples(ordinary_candidates, 'candidate'),
            _query_samples(non_conflict_reviewable_memories, 'memory'),
        ),
        target_checkpoint='CP5',
    )
