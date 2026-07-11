from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from django.db.models import CharField, Count, Exists, F, OuterRef, Prefetch, Q, QuerySet, Value
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast, Coalesce, Concat
from django.utils import timezone

from engram.core.models import (
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
    Project,
    RawEventEnvelope,
    RetrievalDocument,
    Runtime,
    SessionStatus,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowRunType,
    WorkflowSubjectType,
    WorkflowWork,
    WorkflowWorkType,
)
from engram.memory.conflict_links import CONFLICT_CANDIDATE_TARGET_PREFIX
from engram.memory.workflow_work import observation_content_digest, work_input_fingerprint

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
        'latest_input_watermark_missing',
        'exact latest and completed input watermarks',
        'CP2/CP3',
    ),
    InvariantId.P4: (
        'work_lease_and_reclaim_evidence_missing',
        'lease expiry, owner, heartbeat, and reclaim evidence',
        'CP2',
    ),
    InvariantId.P5: (
        'observation_coverage_relation_missing',
        'observation-to-window disposition coverage relation',
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
        _evaluate_p3(project),
        _evaluate_p4(project, effective_as_of),
        _missing(InvariantId.P5),
        _evaluate_p6(project),
        _evaluate_p7(project),
        _missing(InvariantId.P8),
        _missing(InvariantId.P9),
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

    stored_policy = snapshot.get('policy')
    if stored_policy != policy:
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


def _evaluate_p3(project: Project) -> InvariantResult:
    non_lifecycle_observations = Observation.objects.filter(
        organization_id=project.organization_id,
        project_id=project.id,
        session_id=OuterRef('id'),
    ).exclude(observation_type__in=_LIFECYCLE_OBSERVATION_TYPES)
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
    sessions = (
        AgentSession.objects.filter(
            organization_id=project.organization_id,
            project_id=project.id,
            status=SessionStatus.ENDED,
        )
        .annotate(
            has_non_lifecycle_observation=Exists(non_lifecycle_observations),
            has_successful_run=Exists(successful_runs),
        )
        .filter(has_non_lifecycle_observation=True, has_successful_run=False)
    )

    return _missing(
        InvariantId.P3,
        proxy_count=sessions.count(),
        sample_ids=_query_samples(sessions, 'session'),
    )


def _evaluate_p4(project: Project, as_of: datetime) -> InvariantResult:
    stale_runs = (
        WorkflowRun.objects.filter(
            organization_id=project.organization_id,
            project_id=project.id,
            status=WorkflowRunStatus.RUNNING,
        )
        .annotate(effective_started_at=Coalesce('started_at', 'created_at'))
        .filter(effective_started_at__lt=as_of - timedelta(minutes=30))
    )

    return _missing(
        InvariantId.P4,
        proxy_count=stale_runs.count(),
        sample_ids=_query_samples(stale_runs, 'workflow_run'),
    )


def _evaluate_p6(project: Project) -> InvariantResult:
    proposed_candidates = MemoryCandidate.objects.filter(
        organization_id=project.organization_id,
        project_id=project.id,
        status=CandidateStatus.PROPOSED,
    )

    return _missing(
        InvariantId.P6,
        proxy_count=proposed_candidates.count(),
        sample_ids=_query_samples(proposed_candidates, 'candidate'),
    )


def _evaluate_p7(project: Project) -> InvariantResult:
    scoped_promoted_memory = Memory.objects.filter(
        organization_id=project.organization_id,
        project_id=project.id,
        id=OuterRef('promoted_memory_id'),
    )
    candidates_without_memory = (
        MemoryCandidate.objects.filter(
            organization_id=project.organization_id,
            project_id=project.id,
            status=CandidateStatus.PROMOTED,
        )
        .annotate(has_scoped_promoted_memory=Exists(scoped_promoted_memory))
        .filter(has_scoped_promoted_memory=False)
    )
    current_versions = MemoryVersion.objects.filter(
        organization_id=project.organization_id,
        project_id=project.id,
        memory__organization_id=project.organization_id,
        memory__project_id=project.id,
        memory_id=OuterRef('id'),
        version=OuterRef('current_version'),
    )
    matching_current_bodies = current_versions.filter(body=OuterRef('body'))
    consistent_documents = (
        RetrievalDocument.objects.filter(
            organization_id=project.organization_id,
            project_id=project.id,
            memory__organization_id=project.organization_id,
            memory__project_id=project.id,
            memory_version__organization_id=project.organization_id,
            memory_version__project_id=project.id,
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
            organization_id=project.organization_id,
            project_id=project.id,
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
    violation_count = (
        candidates_without_memory.count()
        + missing_current_versions.count()
        + mismatched_current_bodies.count()
        + missing_or_inconsistent_documents.count()
    )

    if not violation_count:
        return _missing(InvariantId.P7, violation_count=0)

    sample_ids = _merge_samples(
        _query_samples(candidates_without_memory, 'candidate'),
        _query_samples(missing_current_versions, 'memory'),
        _query_samples(mismatched_current_bodies, 'memory'),
        _query_samples(missing_or_inconsistent_documents, 'memory'),
    )

    return InvariantResult(
        invariant_id=InvariantId.P7,
        state=InvariantState.VIOLATED,
        reason='promotion_chain_inconsistent',
        violation_count=violation_count,
        sample_ids=sample_ids,
        missing_evidence='relational promotion provenance and transition audit identity',
        target_checkpoint='CP4',
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
