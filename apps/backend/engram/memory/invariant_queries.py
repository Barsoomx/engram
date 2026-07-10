from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from django.db.models import CharField, Count, Exists, OuterRef, Q, QuerySet, Value
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
    Project,
    RawEventEnvelope,
    RetrievalDocument,
    SessionStatus,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowRunType,
)
from engram.memory.conflict_links import CONFLICT_CANDIDATE_TARGET_PREFIX

_SAMPLE_LIMIT = 20
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
