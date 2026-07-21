from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from engram.core.models import (
    CandidateStatus,
    DistillationCoverageOutcome,
    DistillationObservationCoverage,
    DistillationStage,
    DistillationStageKind,
    DistillationStageStatus,
    DistillationWindow,
    Memory,
    MemoryCandidate,
    MemoryCandidateSource,
    Observation,
    VisibilityScope,
    WorkflowRunOrigin,
    WorkflowWork,
    WorkflowWorkDisposition,
    WorkflowWorkExecutionState,
)
from engram.memory.candidate_decision_work import ensure_candidate_decision_work_locked, evidence_manifest
from engram.memory.distillation_provenance import (
    CandidatePlan,
    FinalizationPlan,
    ProvenanceContractError,
    build_finalization_plan,
    session_candidate_content_hash,
)
from engram.memory.distillation_provider_stage import (
    STAGE_BLOCKED,
    STAGE_COMPLETED,
    STAGE_CONTINUATION,
    STAGE_RETRY,
    execute_distillation_stage,
    resolve_extraction_stage,
    stage_target_key,
)
from engram.memory.distillation_provider_stage import (
    PROVIDER_OUTPUT_TRUNCATED,
    resolve_reduction_policy,
)
from engram.memory.distillation_provider_stage import (
    stage_key as provider_stage_key,
)
from engram.memory.distillation_reduction import (
    ReductionContractError,
    ReductionTruncationExhausted,
    compute_reduction_generation,
    derive_final_reduction_drafts,
    derive_first_pending_reduction_target,
    execute_reduction_stage,
    output_budget_tokens,
    provider_stage_target,
    resolve_reduction_stage,
)
from engram.memory.distillation_window import (
    continue_distillation_work,
    materialize_distillation_window,
    max_provider_calls_per_attempt,
    next_distillation_stage,
)
from engram.memory.services import (
    MemoryWorkerError,
)
from engram.memory.transitions import (
    AttachPromotedCandidateSource,
    AttachPromotedCandidateSourceInput,
    CandidateFence,
    TransitionRequest,
    TransitionScope,
    build_memory_fence,
)
from engram.memory.work_dispatch import queue_work_attempt
from engram.memory.work_execution import (
    WorkClaim,
    execution_configuration_fingerprint,
    finish_work_claim,
    lock_work_fence,
)
from engram.memory.work_failures import CONFIGURATION, INVALID_INPUT, ClassifiedWorkFailure
from engram.memory.workflow_work import canonical_json_bytes, observation_content_digest
from engram.model_policy.errors import ModelPolicyError
from engram.model_policy.services import effective_completion_cap


@dataclass(frozen=True, slots=True)
class FinalizationResult:
    candidates: tuple[MemoryCandidate, ...]
    decision_work_ids: tuple[uuid.UUID, ...]


class DistillationStageError(Exception):
    def __init__(self, failure: ClassifiedWorkFailure) -> None:
        self.failure = failure
        super().__init__(failure.redacted_detail or failure.code)


def _finalization_error(message: str) -> MemoryWorkerError:
    return MemoryWorkerError(message, code='work_fingerprint_mismatch')


def _sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _same_identity(left: object, right: object) -> bool:
    if left is None or right is None:
        return left is right

    return str(left) == str(right)


def _verify_finalization_scope(window: DistillationWindow, plan: FinalizationPlan) -> None:
    expected = {
        'organization_id': window.organization_id,
        'project_id': window.project_id,
        'team_id': window.team_id,
        'session_id': window.session_id,
    }
    if any(not _same_identity(plan.scope.get(key), value) for key, value in expected.items()):
        raise _finalization_error('finalization plan scope does not match the window')
    if plan.window_input_hash != window.input_hash:
        raise _finalization_error('finalization plan window hash does not match')

    return


def _verify_window_manifest(  # noqa: C901
    window: DistillationWindow,
    work: WorkflowWork,
) -> tuple[dict[str, Mapping[str, object]], dict[uuid.UUID, object]]:
    chunks = list(window.chunks.select_for_update().order_by('ordinal'))
    if not chunks or [chunk.ordinal for chunk in chunks] != list(range(len(chunks))):
        raise _finalization_error('distillation chunks are incomplete or unordered')
    entries: list[Mapping[str, object]] = []
    chunks_by_id: dict[uuid.UUID, object] = {}
    seen_ids: set[str] = set()
    seen_sequences: set[int] = set()
    for chunk in chunks:
        manifest = chunk.input_manifest
        if not isinstance(manifest, dict) or set(manifest) != {
            'schema',
            'window_input_hash',
            'ordinal',
            'observations',
        }:
            raise _finalization_error('distillation chunk manifest is malformed')
        if (
            manifest['schema'] != 'distillation_chunk_manifest.v1'
            or manifest['window_input_hash'] != window.input_hash
            or manifest['ordinal'] != chunk.ordinal
            or _sha256(manifest) != chunk.input_hash
        ):
            raise _finalization_error('distillation chunk hash does not match its manifest')
        observations = manifest['observations']
        if not isinstance(observations, list) or len(observations) != chunk.observation_count:
            raise _finalization_error('distillation chunk observation count is invalid')
        for entry in observations:
            if not isinstance(entry, dict) or set(entry) != {
                'observation_id',
                'session_sequence',
                'content_digest',
            }:
                raise _finalization_error('distillation observation manifest entry is malformed')
            observation_id = entry['observation_id']
            sequence = entry['session_sequence']
            digest = entry['content_digest']
            if (
                not isinstance(observation_id, str)
                or type(sequence) is not int
                or sequence <= 0
                or not isinstance(digest, str)
                or len(digest) != 64
                or observation_id in seen_ids
                or sequence in seen_sequences
            ):
                raise _finalization_error('distillation observation manifest identity is invalid')
            seen_ids.add(observation_id)
            seen_sequences.add(sequence)
            entries.append(entry)
        if (
            observations[0]['session_sequence'] != chunk.first_sequence
            or observations[-1]['session_sequence'] != chunk.last_sequence
        ):
            raise _finalization_error('distillation chunk sequence bounds are invalid')
        chunks_by_id[chunk.id] = chunk
    if len(entries) != window.observation_count:
        raise _finalization_error('distillation window observation count is invalid')
    window_manifest = {
        'schema': 'distillation_window_manifest.v1',
        'work_id': str(work.id),
        'work_input_fingerprint': work.input_fingerprint,
        'lower_sequence_exclusive': window.lower_sequence_exclusive,
        'upper_sequence_inclusive': window.upper_sequence_inclusive,
        'observations': entries,
    }
    if _sha256(window_manifest) != window.input_hash:
        raise _finalization_error('distillation window hash does not match its manifest')

    return {entry['observation_id']: entry for entry in entries}, chunks_by_id


def _verify_complete_stages(  # noqa: C901
    window: DistillationWindow,
    work: WorkflowWork,
    chunks_by_id: Mapping[uuid.UUID, object],
) -> dict[str, DistillationStage]:
    stages = list(
        DistillationStage.objects.select_for_update(of=('self',))
        .filter(window=window, status=DistillationStageStatus.COMPLETE)
        .select_related('chunk', 'policy')
        .order_by('stage_kind', 'level', 'ordinal', 'id')
    )
    complete_by_key: dict[str, DistillationStage] = {}
    extracted_chunks: set[uuid.UUID] = set()
    for stage in stages:
        if (
            stage.organization_id != window.organization_id
            or stage.project_id != window.project_id
            or stage.team_id != window.team_id
            or stage.output_snapshot is None
            or _sha256(stage.output_snapshot) != stage.output_hash
        ):
            raise _finalization_error('completed distillation stage is outside the exact window scope')
        if stage.stage_kind == DistillationStageKind.EXTRACT:
            chunk = chunks_by_id.get(stage.chunk_id)
            if chunk is None or stage.level != 0 or stage.ordinal != chunk.ordinal:
                raise _finalization_error('completed extraction stage coordinate is invalid')
            if stage.input_hash != chunk.input_hash or stage.input_manifest != chunk.input_manifest:
                raise _finalization_error('completed extraction stage input does not match its chunk')
            extracted_chunks.add(chunk.id)
        elif stage.stage_kind == DistillationStageKind.REDUCE:
            manifest = stage.input_manifest
            if (
                stage.chunk_id is not None
                or stage.level <= 0
                or not isinstance(manifest, dict)
                or set(manifest) != {'schema', 'level', 'ordinal', 'refs'}
                or manifest['schema'] != 'distillation_reduce_manifest.v1'
                or manifest['level'] != stage.level
                or manifest['ordinal'] != stage.ordinal
                or not isinstance(manifest['refs'], list)
                or _sha256({'schema': manifest['schema'], 'refs': manifest['refs']}) != stage.input_hash
            ):
                raise _finalization_error('completed reduction stage input is invalid')
        else:
            raise _finalization_error('completed distillation stage kind is invalid')
        expected_target_key = stage_target_key(
            work_id=str(work.id),
            work_input_fingerprint=work.input_fingerprint,
            window_input_hash=window.input_hash,
            stage_kind=stage.stage_kind,
            level=stage.level,
            ordinal=stage.ordinal,
            chunk_ordinal=stage.chunk.ordinal if stage.chunk_id is not None else None,
            input_hash=stage.input_hash,
            prompt_contract=stage.prompt_contract,
        )
        expected_stage_key = provider_stage_key(
            target_key=expected_target_key,
            policy_id=str(stage.policy_id),
            policy_version=stage.policy_version,
            policy_role=stage.policy_role,
        )
        if stage.target_key != expected_target_key or stage.stage_key != expected_stage_key:
            raise _finalization_error('completed distillation stage identity is invalid')
        if stage.stage_key in complete_by_key:
            raise _finalization_error('completed distillation stage key is duplicated')
        complete_by_key[stage.stage_key] = stage
    if extracted_chunks != set(chunks_by_id):
        raise _finalization_error('not every extraction chunk has an accepted stage')

    return complete_by_key


def _verify_finalization_plan(  # noqa: C901
    window: DistillationWindow,
    work: WorkflowWork,
    plan: FinalizationPlan,
) -> tuple[dict[str, Observation], dict[str, DistillationStage]]:
    _verify_finalization_scope(window, plan)
    manifest_by_id, chunks_by_id = _verify_window_manifest(window, work)
    complete_stages = _verify_complete_stages(window, work, chunks_by_id)
    observations = {
        str(observation.id): observation
        for observation in Observation.objects.select_for_update().filter(
            id__in=manifest_by_id,
            organization_id=window.organization_id,
            project_id=window.project_id,
            team_id=window.team_id,
            session_id=window.session_id,
        )
    }
    if set(observations) != set(manifest_by_id):
        raise _finalization_error('window observation is missing or outside scope')
    for observation_id, entry in manifest_by_id.items():
        observation = observations[observation_id]
        if (
            observation.session_sequence != entry['session_sequence']
            or observation_content_digest(observation) != entry['content_digest']
        ):
            raise _finalization_error('window observation no longer matches its frozen digest')
    coverage_by_id = {coverage.observation_id: coverage for coverage in plan.coverage}
    if len(coverage_by_id) != len(plan.coverage) or set(coverage_by_id) != set(manifest_by_id):
        raise _finalization_error('finalization coverage is not exact')
    signal_source_stages: dict[str, set[str]] = {}
    for candidate in plan.candidates:
        expected_hash = session_candidate_content_hash(window.session_id, candidate.title, candidate.body)
        if candidate.content_hash not in (None, expected_hash):
            raise _finalization_error('candidate content identity does not match the session contract')
        if candidate.deciding_stage_key not in complete_stages:
            raise _finalization_error('candidate deciding stage is not complete')
        for source in candidate.sources:
            entry = manifest_by_id.get(source.observation_id)
            if (
                entry is None
                or source.session_sequence != entry['session_sequence']
                or source.observation_digest != entry['content_digest']
                or source.lineage_stage_key not in complete_stages
                or _sha256(dict(source.anchors)) != source.anchors_hash
            ):
                raise _finalization_error('candidate source does not match the frozen window')
            signal_source_stages.setdefault(source.observation_id, set()).add(source.lineage_stage_key)
    for observation_id, coverage in coverage_by_id.items():
        entry = manifest_by_id[observation_id]
        if (
            coverage.session_sequence != entry['session_sequence']
            or coverage.observation_digest != entry['content_digest']
            or coverage.deciding_stage_key not in complete_stages
        ):
            raise _finalization_error('coverage row does not match the frozen window')
        source_stages = signal_source_stages.get(observation_id, set())
        has_source = bool(source_stages)
        if coverage.outcome == DistillationCoverageOutcome.SIGNAL and not has_source:
            raise _finalization_error('signal coverage requires candidate provenance')
        if coverage.outcome == DistillationCoverageOutcome.SIGNAL and coverage.deciding_stage_key not in source_stages:
            raise _finalization_error('signal coverage deciding stage does not match candidate provenance')
        if coverage.outcome == DistillationCoverageOutcome.NO_SIGNAL and has_source:
            raise _finalization_error('no-signal coverage cannot have candidate provenance')
        if coverage.outcome not in (DistillationCoverageOutcome.SIGNAL, DistillationCoverageOutcome.NO_SIGNAL):
            raise _finalization_error('coverage outcome is invalid')
    if plan.has_signal != bool(signal_source_stages) or plan.intent != (
        'signal' if signal_source_stages else 'no_signal'
    ):
        raise _finalization_error('finalization outcome does not match candidate provenance')

    return observations, complete_stages


def _candidate_for_plan(
    window: DistillationWindow,
    plan: CandidatePlan,
    observations: Mapping[str, Observation],
    existing: dict[str, MemoryCandidate],
) -> tuple[MemoryCandidate, bool]:
    content_hash = plan.content_hash or session_candidate_content_hash(window.session_id, plan.title, plan.body)
    candidate = existing.get(content_hash)
    created = False
    if candidate is None:
        first_source = observations[plan.sources[0].observation_id]
        try:
            with transaction.atomic():
                candidate = MemoryCandidate.objects.create(
                    organization_id=window.organization_id,
                    project_id=window.project_id,
                    team_id=window.team_id,
                    source_observation=first_source,
                    title=plan.title,
                    body=plan.body,
                    status=CandidateStatus.PROPOSED,
                    visibility_scope=VisibilityScope.PROJECT,
                    evidence=[],
                    content_hash=content_hash,
                    confidence=plan.confidence,
                    kind=plan.kind,
                )
            created = True
        except IntegrityError:
            candidate = MemoryCandidate.objects.select_for_update().get(
                organization_id=window.organization_id,
                project_id=window.project_id,
                content_hash=content_hash,
            )
    if (
        candidate.organization_id != window.organization_id
        or candidate.project_id != window.project_id
        or candidate.team_id != window.team_id
        or candidate.title != plan.title
        or candidate.body != plan.body
    ):
        raise _finalization_error('existing candidate does not match the finalization plan')
    existing[content_hash] = candidate

    return candidate, created


def _append_compatibility_evidence(
    candidate: MemoryCandidate,
    window: DistillationWindow,
    plan: CandidatePlan,
) -> None:
    if not isinstance(candidate.evidence, list):
        raise _finalization_error('candidate compatibility evidence is invalid')
    if any(isinstance(item, dict) and item.get('window_id') == str(window.id) for item in candidate.evidence):
        return
    summary = {
        'schema': 'candidate_source_summary.v1',
        'session_id': str(window.session_id),
        'window_id': str(window.id),
        'supporting_observation_ids': [source.observation_id for source in plan.sources],
        'stage_keys': sorted({source.lineage_stage_key for source in plan.sources}),
    }
    candidate.evidence = [*candidate.evidence, summary]
    candidate.save(update_fields=['evidence', 'updated_at'])

    return


def _attach_promoted_candidate_source(
    candidate: MemoryCandidate,
    source: MemoryCandidateSource,
    window: DistillationWindow,
) -> object | None:
    if candidate.status != CandidateStatus.PROMOTED or candidate.promoted_memory_id is None:
        return None
    promoted_memory = Memory.objects.get(id=candidate.promoted_memory_id)
    if promoted_memory.transition_contract_version != 1:
        return None
    _entries, manifest_hash = evidence_manifest(candidate)
    idempotency_key = f'candidate-source:{source.id}:attach:v1'
    return AttachPromotedCandidateSource().execute(
        AttachPromotedCandidateSourceInput(
            request=TransitionRequest(
                scope=TransitionScope(
                    organization_id=candidate.organization_id,
                    project_id=candidate.project_id,
                    team_id=candidate.team_id,
                ),
                idempotency_key=idempotency_key,
                actor_type='memory_worker',
                actor_id='memory-worker',
                capability='memories:write',
                request_id=idempotency_key,
                correlation_id=f'distillation-window:{window.id}',
                reason='attach promoted candidate source',
                origin='memory-worker',
            ),
            candidate_fence=CandidateFence(
                candidate_id=candidate.id,
                candidate_content_hash=candidate.content_hash,
                evidence_manifest_hash=manifest_hash,
            ),
            memory_fence=build_memory_fence(promoted_memory),
            candidate_source_id=source.id,
        ),
    )


def finalize_distillation(  # noqa: C901
    *,
    window: DistillationWindow,
    claim: WorkClaim,
    plan: FinalizationPlan,
    now: datetime,
    fault_injector: Callable[[str], None] | None = None,
) -> FinalizationResult:
    def inject(point: str) -> None:
        if fault_injector is not None:
            fault_injector(point)

        return

    with transaction.atomic():
        locked_work, _root_run = lock_work_fence(claim=claim, now=now)
        if locked_work.disposition != WorkflowWorkDisposition.REQUIRED:
            raise _finalization_error('distillation root work is not required')
        locked_window = DistillationWindow.objects.select_for_update().get(id=window.id)
        if locked_window.work_id != locked_work.id:
            raise _finalization_error('distillation window does not belong to the claimed root')
        observations, complete_stages = _verify_finalization_plan(locked_window, locked_work, plan)
        content_hashes = [
            candidate.content_hash
            or session_candidate_content_hash(locked_window.session_id, candidate.title, candidate.body)
            for candidate in plan.candidates
        ]
        existing_candidates = {
            candidate.content_hash: candidate
            for candidate in MemoryCandidate.objects.select_for_update()
            .filter(
                organization_id=locked_window.organization_id,
                project_id=locked_window.project_id,
                content_hash__in=content_hashes,
            )
            .order_by('id')
        }
        candidates: list[MemoryCandidate] = []
        by_draft_id: dict[str, MemoryCandidate] = {}
        for candidate_plan in plan.candidates:
            candidate, _created = _candidate_for_plan(
                locked_window,
                candidate_plan,
                observations,
                existing_candidates,
            )
            candidates.append(candidate)
            by_draft_id[candidate_plan.final_draft_id] = candidate
        inject('candidate')
        for candidate_plan in plan.candidates:
            candidate = by_draft_id[candidate_plan.final_draft_id]
            for source in candidate_plan.sources:
                stage = complete_stages[source.lineage_stage_key]
                values = {
                    'organization_id': locked_window.organization_id,
                    'project_id': locked_window.project_id,
                    'team_id': locked_window.team_id,
                    'window': locked_window,
                    'observation': observations[source.observation_id],
                    'stage': stage,
                    'anchors': dict(source.anchors),
                    'anchors_hash': source.anchors_hash,
                }
                persisted, created = MemoryCandidateSource.objects.get_or_create(
                    candidate=candidate,
                    window=locked_window,
                    observation=observations[source.observation_id],
                    defaults=values,
                )
                if not created and any(
                    getattr(persisted, field) != value
                    for field, value in (
                        ('organization_id', values['organization_id']),
                        ('project_id', values['project_id']),
                        ('team_id', values['team_id']),
                        ('stage_id', stage.id),
                        ('anchors', values['anchors']),
                        ('anchors_hash', values['anchors_hash']),
                    )
                ):
                    raise _finalization_error('existing candidate source does not match finalization')
                if created:
                    _attach_promoted_candidate_source(candidate, persisted, locked_window)
            _append_compatibility_evidence(candidate, locked_window, candidate_plan)
        inject('source')
        for coverage in plan.coverage:
            stage = complete_stages[coverage.deciding_stage_key]
            values = {
                'organization_id': locked_window.organization_id,
                'project_id': locked_window.project_id,
                'team_id': locked_window.team_id,
                'session_sequence': coverage.session_sequence,
                'observation_digest': coverage.observation_digest,
                'outcome': coverage.outcome,
                'deciding_stage': stage,
            }
            persisted, created = DistillationObservationCoverage.objects.get_or_create(
                window=locked_window,
                observation=observations[coverage.observation_id],
                defaults=values,
            )
            if not created and any(
                getattr(persisted, field) != value
                for field, value in (
                    ('organization_id', values['organization_id']),
                    ('project_id', values['project_id']),
                    ('team_id', values['team_id']),
                    ('session_sequence', values['session_sequence']),
                    ('observation_digest', values['observation_digest']),
                    ('outcome', values['outcome']),
                    ('deciding_stage_id', stage.id),
                )
            ):
                raise _finalization_error('existing coverage does not match finalization')
        inject('coverage')
        decision_works: list[tuple[WorkflowWork, bool]] = []
        for candidate in candidates:
            decision_works.append(ensure_candidate_decision_work_locked(candidate))
        inject('work')
        for decision_work, created in decision_works:
            if created:
                queue_work_attempt(
                    work_id=decision_work.id,
                    now=now,
                    origin=WorkflowRunOrigin.AUTOMATIC,
                )
        inject('package')
        finish_work_claim(
            claim=claim,
            now=now,
            completion='product_succeeded' if plan.has_signal else 'product_no_signal',
        )
        for candidate in candidates:
            if candidate.decision_work_contract_version != 1:
                candidate.decision_work_contract_version = 1
                candidate.save(update_fields=['decision_work_contract_version', 'updated_at'])
        settled = WorkflowWork.objects.get(id=locked_work.id)
        expected_reason = 'succeeded' if plan.has_signal else 'no_signal'
        if (
            settled.disposition != WorkflowWorkDisposition.COMPLETE
            or settled.execution_state != WorkflowWorkExecutionState.SETTLED
            or settled.resolution_reason != expected_reason
        ):
            raise _finalization_error('root work completion does not match finalization')
        inject('root')

    return FinalizationResult(
        candidates=tuple(candidates),
        decision_work_ids=tuple(work.id for work, _created in decision_works),
    )


_LEASE_SAFE_MARGIN = timedelta(seconds=30)


def _configuration_failure(work: WorkflowWork, error: Exception) -> DistillationStageError:
    return DistillationStageError(
        ClassifiedWorkFailure(
            failure_class=CONFIGURATION,
            code='distillation_configuration_invalid',
            redacted_detail=str(error)[:1024],
            configuration_fingerprint=execution_configuration_fingerprint(work),
        )
    )


def _invalid_distillation_failure(code: str, detail: str) -> DistillationStageError:
    return DistillationStageError(
        ClassifiedWorkFailure(
            failure_class=INVALID_INPUT,
            code=code,
            redacted_detail=detail[:1024],
        )
    )


def _accepted_stage_rows(
    window: DistillationWindow, stage_kind: str, *, prompt_contract: str | None = None
) -> list[DistillationStage]:
    filters: dict[str, object] = {
        'window': window,
        'stage_kind': stage_kind,
        'status': DistillationStageStatus.COMPLETE,
    }
    if prompt_contract is not None:
        filters['prompt_contract'] = prompt_contract

    return list(
        DistillationStage.objects.filter(**filters)
        .select_related('chunk', 'window')
        .order_by('level', 'ordinal', 'stage_key')
    )


def _provenance_observations(window: DistillationWindow) -> tuple[dict[str, object], ...]:
    manifest_entries = [
        entry for chunk in window.chunks.order_by('ordinal') for entry in chunk.input_manifest['observations']
    ]
    observations = {
        str(observation.id): observation
        for observation in Observation.objects.filter(
            id__in=[entry['observation_id'] for entry in manifest_entries],
            organization_id=window.organization_id,
            project_id=window.project_id,
            team_id=window.team_id,
            session_id=window.session_id,
        )
    }
    if set(observations) != {entry['observation_id'] for entry in manifest_entries}:
        raise _invalid_distillation_failure(
            'distillation_observation_scope_invalid',
            'frozen distillation observation is missing or outside scope',
        )

    return tuple(
        {
            'id': entry['observation_id'],
            'observation_id': entry['observation_id'],
            'session_sequence': entry['session_sequence'],
            'observation_digest': entry['content_digest'],
            'content_digest': entry['content_digest'],
            'organization_id': window.organization_id,
            'project_id': window.project_id,
            'team_id': window.team_id,
            'session_id': window.session_id,
            'source_metadata': observations[entry['observation_id']].source_metadata,
            'files_read': observations[entry['observation_id']].files_read,
            'files_modified': observations[entry['observation_id']].files_modified,
        }
        for entry in manifest_entries
    )


def _attempt_now(initial: datetime) -> datetime:
    return max(initial, timezone.now())


def _can_start_provider_call(claim: WorkClaim, *, now: datetime, started: int, budget: int) -> bool:
    return started < budget and now + _LEASE_SAFE_MARGIN < claim.lease_expires_at


def _continue_complete_distillation(
    work: WorkflowWork,
    claim: WorkClaim,
    *,
    now: datetime,
) -> str:
    continue_distillation_work(work=work, claim=claim, now=now)

    return STAGE_CONTINUATION


def _consume_stage_result(
    result: object,
    *,
    fault_injector: Callable[[str], None] | None,
) -> tuple[str, int]:
    status = result.status
    started = result.started_provider_calls
    if status == STAGE_COMPLETED:
        if started and fault_injector is not None:
            fault_injector('stage_completed')

        return status, started
    if status == STAGE_CONTINUATION:
        return status, started
    if status in (STAGE_RETRY, STAGE_BLOCKED) and result.failure is not None:
        raise DistillationStageError(result.failure)

    raise _invalid_distillation_failure(
        'distillation_stage_result_invalid',
        'provider stage returned an invalid operational result',
    )


def run_complete_distillation_attempt(  # noqa: C901
    *,
    work: WorkflowWork,
    claim: WorkClaim,
    now: datetime,
    fault_injector: Callable[[str], None] | None = None,
) -> str:
    if claim.work_id != work.id:
        raise _invalid_distillation_failure(
            'distillation_claim_scope_invalid',
            'distillation claim does not belong to the root work',
        )
    try:
        window = materialize_distillation_window(work)
        provider_budget = max_provider_calls_per_attempt()
    except ValueError as error:
        raise _configuration_failure(work, error) from error

    started_provider_calls = 0
    while True:
        current_now = _attempt_now(now)
        pending_chunk = next_distillation_stage(window)
        if pending_chunk is not None:
            if not _can_start_provider_call(
                claim,
                now=current_now,
                started=started_provider_calls,
                budget=provider_budget,
            ):
                return _continue_complete_distillation(work, claim, now=current_now)
            stage = resolve_extraction_stage(chunk=pending_chunk, claim=claim, now=current_now)
            result = execute_distillation_stage(
                stage,
                claim,
                now=_attempt_now(now),
                max_provider_calls=provider_budget - started_provider_calls,
            )
            status, started = _consume_stage_result(result, fault_injector=fault_injector)
            started_provider_calls += started
            if status == STAGE_CONTINUATION:
                return _continue_complete_distillation(work, claim, now=_attempt_now(now))

            continue

        extraction_stages = _accepted_stage_rows(window, DistillationStageKind.EXTRACT)
        reduction_stages = _accepted_stage_rows(
            window, DistillationStageKind.REDUCE, prompt_contract='distill_reduce.v2'
        )
        try:
            reduce_policy = resolve_reduction_policy(window)
        except ModelPolicyError as error:
            raise _configuration_failure(work, error) from error
        output_budget = output_budget_tokens(effective_completion_cap(reduce_policy, 'distill_reduce.v2'))
        truncated_levels = list(
            DistillationStage.objects.filter(
                window=window,
                stage_kind=DistillationStageKind.REDUCE,
                status=DistillationStageStatus.REQUIRED,
                prompt_contract='distill_reduce.v2',
                last_failure_class=PROVIDER_OUTPUT_TRUNCATED,
            ).values_list('level', flat=True)
        )
        try:
            generation = compute_reduction_generation(truncated_levels)
            pending_reduction = derive_first_pending_reduction_target(
                extraction_stages,
                reduction_stages,
                reduction_target_floor=window.reduction_target,
                output_budget_tokens=output_budget,
                generation=generation,
            )
        except ReductionTruncationExhausted as error:
            raise _invalid_distillation_failure(
                'distillation_reduction_truncation_exhausted',
                str(error),
            ) from error
        except ReductionContractError as error:
            raise _invalid_distillation_failure(
                'distillation_reduction_plan_invalid',
                str(error),
            ) from error
        if pending_reduction is not None:
            if not _can_start_provider_call(
                claim,
                now=current_now,
                started=started_provider_calls,
                budget=provider_budget,
            ):
                return _continue_complete_distillation(work, claim, now=current_now)
            target = provider_stage_target(window, pending_reduction)
            stage = resolve_reduction_stage(target, claim, now=current_now)
            result = execute_reduction_stage(
                stage,
                claim,
                now=_attempt_now(now),
                max_provider_calls=provider_budget - started_provider_calls,
            )
            status, started = _consume_stage_result(result, fault_injector=fault_injector)
            started_provider_calls += started
            if status == STAGE_CONTINUATION:
                return _continue_complete_distillation(work, claim, now=_attempt_now(now))

            continue

        try:
            final_drafts = derive_final_reduction_drafts(
                extraction_stages,
                reduction_stages,
                reduction_target_floor=window.reduction_target,
                output_budget_tokens=output_budget,
                generation=generation,
            )
            leaf_count = sum(len(stage.output_snapshot['memories']) for stage in extraction_stages)
            if leaf_count and not final_drafts:
                raise ReductionContractError('completed reduction graph has no final drafts')
            plan = build_finalization_plan(
                window=window,
                final_drafts=final_drafts,
                observations=_provenance_observations(window),
                extraction_stages=extraction_stages,
                reduction_stages=reduction_stages,
            )
        except (KeyError, ProvenanceContractError, ReductionContractError) as error:
            raise _invalid_distillation_failure(
                'distillation_finalization_plan_invalid',
                str(error),
            ) from error
        if _attempt_now(now) + _LEASE_SAFE_MARGIN >= claim.lease_expires_at:
            return _continue_complete_distillation(work, claim, now=_attempt_now(now))
        finalize_distillation(
            window=window,
            claim=claim,
            plan=plan,
            now=_attempt_now(now),
        )

        return STAGE_COMPLETED
