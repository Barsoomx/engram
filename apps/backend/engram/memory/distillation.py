from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

import structlog
from django.db import IntegrityError, transaction
from django.utils import timezone

from engram.core.models import (
    AgentSession,
    AuditEvent,
    AuditResult,
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
    WorkflowRun,
    WorkflowRunOrigin,
    WorkflowRunStatus,
    WorkflowRunType,
    WorkflowWork,
    WorkflowWorkDisposition,
    WorkflowWorkExecutionState,
    clamp_memory_kind,
)
from engram.memory.candidate_decision_work import ensure_candidate_decision_work_locked
from engram.memory.candidate_parsing import (
    SynthesizedCandidate,
    _clamp_confidence,
    parse_synthesized_candidates,
    strip_json_fence,
)
from engram.memory.curation import CurateMemoryCandidate, CurateMemoryCandidateInput
from engram.memory.distillation_provenance import (
    CandidatePlan,
    FinalizationPlan,
    ProvenanceContractError,
    build_finalization_plan,
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
    stage_key as provider_stage_key,
)
from engram.memory.distillation_reduction import (
    ReductionContractError,
    derive_final_reduction_drafts,
    derive_first_pending_reduction_target,
    execute_reduction_stage,
    provider_stage_target,
    resolve_reduction_stage,
)
from engram.memory.distillation_window import (
    continue_distillation_work,
    materialize_distillation_window,
    max_provider_calls_per_attempt,
    next_distillation_stage,
    render_observation_block,
)
from engram.memory.observation_work import useful_observation_q
from engram.memory.services import (
    MemoryWorkerError,
    call_with_fallback,
    is_auto_promotable,
    redact_error,
    redact_value,
    resolve_auto_approve_threshold,
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
from engram.model_policy.models import ModelPolicy
from engram.model_policy.services import (
    AnthropicMessagesGateway,
    FakeProviderGateway,
    ModelPolicyError,
    OpenAICompatibleGateway,
    ProviderCallInput,
    ProviderCallResult,
    ProviderSecretError,
    ResolvedModelPolicy,
    ResolveModelPolicy,
    ResolveModelPolicyInput,
    get_provider_gateway,
    resolve_context_window_tokens,
)

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class DistillSessionInput:
    session_id: uuid.UUID
    request_id: str = ''
    correlation_id: str = ''
    auto_approve_threshold: Decimal | None = None
    run_id: str = ''
    upper_sequence_inclusive: int | None = None


@dataclass(frozen=True)
class _ReducedCandidate:
    title: str
    body: str
    confidence: Decimal
    source_ids: tuple[int, ...]
    kind: str = ''


@dataclass(frozen=True)
class DistillSessionResult:
    session: AgentSession
    auto_promoted: tuple[Memory, ...]
    queued_for_review: tuple[MemoryCandidate, ...]
    provider_call_ids: tuple[str, ...] = ()
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class FinalizationResult:
    candidates: tuple[MemoryCandidate, ...]
    decision_work_ids: tuple[uuid.UUID, ...]


class DistillationStageError(Exception):
    def __init__(self, failure: ClassifiedWorkFailure) -> None:
        self.failure = failure
        super().__init__(failure.redacted_detail or failure.code)


def session_distillation_system_prompt() -> str:
    return (
        'You are a session distillation engine for software engineering sessions.\n'
        'Given structured observations from one agent session, synthesize durable, '
        'runtime-neutral engineering memories.\n'
        '\n'
        'Rules:\n'
        '- Output a single JSON object only, with exactly one key "memories".\n'
        '- "memories" is an array of objects with the keys '
        '"title", "body", "confidence", "supporting_observation_ids", and optionally "kind".\n'
        '- If the session contains no durable engineering signal, output {"memories": []}.\n'
        '- "confidence" is a number between 0 and 1: 0.9 or higher for verified facts with direct '
        'evidence (a fix confirmed by tests, an observed error with its cause), 0.6-0.8 for plausible '
        'conclusions consistent with the observations, 0.3-0.5 for unverified hypotheses, below 0.3 '
        'for speculation.\n'
        '- "supporting_observation_ids" lists the observation ids the memory is derived from.\n'
        '- "kind" is optional: one of "decision", "convention", "gotcha", "architecture", "incident" '
        'when the memory clearly fits one of those categories, omitted otherwise.\n'
        '- Consolidate related observations into a small number of high-signal memories.\n'
        '- Preserve exact identifiers verbatim: file paths, function names, class names, '
        'CLI commands, error strings, ticket identifiers, URLs, and config keys.\n'
        '- Drop session chatter, acknowledgements, timestamps, and credential-shaped values.\n'
        '- Do not invent facts not present in the input.\n'
        '- Do not name any AI assistant, tool, or product by brand.\n'
        '\n'
        'Good memory: {"title": "Retry queue drops messages on Redis restart", '
        '"body": "The consumer in worker/queue.py acknowledges messages before processing; '
        'a Redis restart during processing loses them. Fixed by acknowledging after processing.", '
        '"confidence": 0.9, "supporting_observation_ids": ["<id>"]}\n'
        'Bad memory (never produce): {"title": "Worked on the queue", '
        '"body": "Investigated some queue issues and made progress.", '
        '"confidence": 0.4, "supporting_observation_ids": []} '
        '- vague, no identifiers, not durable.'
    )


def session_reduce_system_prompt() -> str:
    return (
        'You are consolidating draft engineering memories synthesized from one agent session.\n'
        'Given a JSON array of draft memories, each with an integer "id", merge near-duplicate or '
        'overlapping drafts into a smaller set of high-signal memories.\n'
        '\n'
        'Rules:\n'
        '- Output a single JSON object only, with exactly one key "memories".\n'
        '- "memories" is an array of objects with the keys '
        '"title", "body", "confidence", "source_ids", and optionally "kind".\n'
        '- "source_ids" lists the integer ids of the input drafts merged into this memory.\n'
        '- "confidence" is a number between 0 and 1 reflecting how durable and reliable the memory is.\n'
        '- "kind" is optional: one of "decision", "convention", "gotcha", "architecture", "incident" '
        'when the merged memory clearly fits one of those categories, omitted otherwise.\n'
        '- Preserve exact identifiers verbatim: file paths, function names, class names, '
        'CLI commands, error strings, ticket identifiers, URLs, and config keys.\n'
        '- Do not invent facts not present in the input drafts.\n'
        '- Do not name any AI assistant, tool, or product by brand.'
    )


_observation_block = render_observation_block


def session_distillation_prompt(observations: list[Observation], cap: int) -> str:
    return '\n\n'.join(_observation_block(observation, cap) for observation in observations)


def _distill_chunk_char_budget(policy: ModelPolicy) -> int:
    env_value = os.environ.get('ENGRAM_DISTILL_CHUNK_CHAR_BUDGET')
    if env_value is not None:
        return int(env_value)

    ceiling = int(os.environ.get('ENGRAM_DISTILL_CHUNK_CHAR_CEILING', '120000'))
    tokens = resolve_context_window_tokens(policy)
    if tokens is None:
        return min(40000, ceiling)

    context_chars = max(tokens - 8000, 0) * 3
    floor = min(8000, ceiling)

    return max(min(context_chars, ceiling), floor)


def chunk_observations(observations: list[Observation], budget: int) -> list[list[Observation]]:
    chunks: list[list[Observation]] = []
    current: list[Observation] = []
    current_length = 0
    for observation in observations:
        block_length = len(_observation_block(observation, budget))
        separator_length = 2 if current else 0
        if current and current_length + separator_length + block_length > budget:
            chunks.append(current)
            current = []
            current_length = 0
            separator_length = 0
        current.append(observation)
        current_length += separator_length + block_length

    if current:
        chunks.append(current)

    return chunks


def session_candidate_content_hash(session_id: uuid.UUID, title: str, body: str) -> str:
    return hashlib.sha256(f'{session_id}:{title}:{body}'.encode()).hexdigest()


def _parse_reduced_source_ids(raw: object) -> tuple[int, ...] | None:
    if not isinstance(raw, list):
        return None

    source_ids: list[int] = []
    for source_id in raw:
        if isinstance(source_id, bool) or not isinstance(source_id, int):
            return None

        source_ids.append(source_id)

    return tuple(source_ids)


def _parse_reduced_candidate_item(item: object) -> _ReducedCandidate | None:
    if not isinstance(item, dict):
        return None

    title = item.get('title')
    body = item.get('body')
    if not isinstance(title, str) or not isinstance(body, str):
        return None

    if not title.strip() or not body.strip():
        return None

    source_ids = _parse_reduced_source_ids(item.get('source_ids'))
    if source_ids is None:
        return None

    return _ReducedCandidate(
        title=title.strip()[:255],
        body=body.strip(),
        confidence=_clamp_confidence(item.get('confidence')),
        source_ids=source_ids,
        kind=clamp_memory_kind(item.get('kind')),
    )


def _parse_reduced_candidates(raw_body: str) -> tuple[_ReducedCandidate, ...] | None:
    try:
        parsed = json.loads(strip_json_fence(raw_body))
    except (json.JSONDecodeError, TypeError):
        return None

    if not isinstance(parsed, dict):
        return None

    items = parsed.get('memories')
    if not isinstance(items, list):
        return None

    candidates: list[_ReducedCandidate] = []
    for item in items:
        candidate = _parse_reduced_candidate_item(item)
        if candidate is None:
            return None

        candidates.append(candidate)

    return tuple(candidates)


class DistillSession:
    def execute(self, data: DistillSessionInput) -> DistillSessionResult:
        session = self._load_session(data.session_id)
        observations = self._session_observations(session, data.upper_sequence_inclusive)
        if not observations:
            return DistillSessionResult(session=session, auto_promoted=(), queued_for_review=())

        correlation_id = data.correlation_id or data.request_id or str(session.id)
        structlog.contextvars.bind_contextvars(
            correlation_id=correlation_id,
            session_id=str(session.id),
        )
        run_scope = data.run_id or data.correlation_id or data.request_id or uuid.uuid4().hex
        resolved, gateway = self._resolve_gateway(session)
        budget = _distill_chunk_char_budget(resolved.policy)
        chunks = chunk_observations(observations, budget)

        active_resolved, active_gateway = resolved, gateway
        provider_results: list[ProviderCallResult] = []
        synthesized: list[tuple[SynthesizedCandidate, dict[str, object]]] = []
        for index, chunk in enumerate(chunks):
            prompt = session_distillation_prompt(chunk, budget)
            provider_result, used_resolved = self._call_chunk(
                session,
                active_gateway,
                active_resolved,
                prompt,
                correlation_id,
                run_scope,
                index,
            )
            if used_resolved.policy.id != active_resolved.policy.id:
                active_resolved = used_resolved
                active_gateway = get_provider_gateway(active_resolved.policy)
            provider_results.append(provider_result)
            provenance = self._provenance(provider_result, used_resolved)
            synthesized.extend(
                (candidate, provenance) for candidate in parse_synthesized_candidates(provider_result.generated_body)
            )

        synthesized = self._reduce_candidates(
            session,
            active_gateway,
            active_resolved,
            synthesized,
            provider_results,
            correlation_id,
            run_scope,
            budget,
            data,
            chunk_count=len(chunks),
        )

        with transaction.atomic():
            locked_session = self._lock_session(data.session_id)
            threshold = resolve_auto_approve_threshold(locked_session.organization, data.auto_approve_threshold)

            to_curate: list[MemoryCandidate] = []
            auto_promoted: list[Memory] = []
            queued: list[MemoryCandidate] = []
            for candidate_input, provenance in synthesized:
                candidate, created = self._get_or_create_candidate(locked_session, candidate_input, provenance)
                if not created:
                    self._classify_existing(candidate, auto_promoted, queued)
                    continue
                if is_auto_promotable(candidate_input.confidence, threshold) and not candidate_input.parse_fallback:
                    to_curate.append(candidate)
                else:
                    self._audit_held(locked_session, candidate, candidate_input.confidence, threshold, data)
                    queued.append(candidate)

        for candidate in to_curate:
            curation = CurateMemoryCandidate().execute(
                CurateMemoryCandidateInput(candidate_id=candidate.id, correlation_id=correlation_id),
            )
            if curation.memory is not None:
                auto_promoted.append(curation.memory)

        return DistillSessionResult(
            session=locked_session,
            auto_promoted=tuple(auto_promoted),
            queued_for_review=tuple(queued),
            provider_call_ids=tuple(str(result.call_record_id) for result in provider_results),
            truncated=False,
        )

    def _load_session(self, session_id: uuid.UUID) -> AgentSession:
        try:
            return AgentSession.objects.select_related('organization', 'project', 'team').get(id=session_id)
        except AgentSession.DoesNotExist as error:
            raise MemoryWorkerError('session not found') from error

    def _lock_session(self, session_id: uuid.UUID) -> AgentSession:
        try:
            return (
                AgentSession.objects.select_for_update(of=('self',))
                .select_related('organization', 'project', 'team')
                .get(id=session_id)
            )
        except AgentSession.DoesNotExist as error:
            raise MemoryWorkerError('session not found') from error

    def _session_observations(
        self,
        session: AgentSession,
        upper_sequence_inclusive: int | None,
    ) -> list[Observation]:
        queryset = Observation.objects.filter(
            organization_id=session.organization_id,
            project_id=session.project_id,
            session=session,
        )
        if upper_sequence_inclusive is None:
            return list(queryset.order_by('prompt_number', 'created_at'))

        return list(
            queryset.filter(
                session_sequence__gt=0,
                session_sequence__lte=upper_sequence_inclusive,
            )
            .filter(useful_observation_q())
            .order_by('session_sequence'),
        )

    def _resolve_gateway(
        self,
        session: AgentSession,
    ) -> tuple[ResolvedModelPolicy, FakeProviderGateway | OpenAICompatibleGateway | AnthropicMessagesGateway]:
        try:
            resolved = self._resolve_policy(session)

            return resolved, get_provider_gateway(resolved.policy)
        except (ModelPolicyError, ProviderSecretError) as error:
            raise MemoryWorkerError(redact_error(str(error)), retryable=getattr(error, 'retryable', False)) from error

    def _call_chunk(
        self,
        session: AgentSession,
        gateway: FakeProviderGateway | OpenAICompatibleGateway | AnthropicMessagesGateway,
        resolved: ResolvedModelPolicy,
        prompt: str,
        correlation_id: str,
        run_scope: str,
        index: int,
    ) -> tuple[ProviderCallResult, ResolvedModelPolicy]:
        try:
            return call_with_fallback(
                resolved,
                gateway,
                ProviderCallInput(
                    organization_id=session.organization_id,
                    project_id=session.project_id,
                    team_id=session.team_id,
                    policy=resolved.policy,
                    request_id=f'distill-session:{session.id}:{run_scope}:curation:chunk:{index}',
                    trace_id=correlation_id or f'distill-session:{session.id}',
                    prompt=prompt,
                    system_prompt=session_distillation_system_prompt(),
                    response_kind='candidates',
                ),
            )
        except (ModelPolicyError, ProviderSecretError) as error:
            raise MemoryWorkerError(redact_error(str(error)), retryable=getattr(error, 'retryable', False)) from error

    def _resolve_policy(self, session: AgentSession) -> ResolvedModelPolicy:
        try:
            return ResolveModelPolicy().execute(
                ResolveModelPolicyInput(
                    organization_id=session.organization_id,
                    project_id=session.project_id,
                    team_id=session.team_id,
                    task_type='curation',
                ),
            )
        except ModelPolicyError:
            return ResolveModelPolicy().execute(
                ResolveModelPolicyInput(
                    organization_id=session.organization_id,
                    project_id=session.project_id,
                    team_id=session.team_id,
                    task_type='generation',
                ),
            )

    def _provenance(self, provider_result: ProviderCallResult, resolved: ResolvedModelPolicy) -> dict[str, object]:
        return {
            'provider_call_id': str(provider_result.call_record_id),
            'provider': provider_result.provider,
            'model': provider_result.model,
            'policy_id': str(resolved.policy.id),
            'policy_version': resolved.policy.version,
            'task_type': resolved.policy.task_type,
            'redaction_state': provider_result.redaction_state,
        }

    def _reduce_candidates(
        self,
        session: AgentSession,
        gateway: FakeProviderGateway | OpenAICompatibleGateway | AnthropicMessagesGateway,
        resolved: ResolvedModelPolicy,
        synthesized: list[tuple[SynthesizedCandidate, dict[str, object]]],
        provider_results: list[ProviderCallResult],
        correlation_id: str,
        run_scope: str,
        budget: int,
        data: DistillSessionInput,
        *,
        chunk_count: int,
    ) -> list[tuple[SynthesizedCandidate, dict[str, object]]]:
        reduce_target = int(os.environ.get('ENGRAM_DISTILL_REDUCE_TARGET', '12'))
        if chunk_count <= 1 or len(synthesized) <= reduce_target:
            return synthesized

        drafts = [
            {
                'id': index,
                'title': candidate.title,
                'body': candidate.body,
                'confidence': float(candidate.confidence),
            }
            for index, (candidate, _provenance) in enumerate(synthesized)
        ]
        reduce_prompt = json.dumps(drafts)
        if len(reduce_prompt) > budget:
            self._audit_reduce_skipped(session, data, reason='over_budget', draft_count=len(synthesized))

            return synthesized

        try:
            reduce_result, used_resolved = call_with_fallback(
                resolved,
                gateway,
                ProviderCallInput(
                    organization_id=session.organization_id,
                    project_id=session.project_id,
                    team_id=session.team_id,
                    policy=resolved.policy,
                    request_id=f'distill-session:{session.id}:{run_scope}:curation:reduce',
                    trace_id=correlation_id or f'distill-session:{session.id}',
                    prompt=reduce_prompt,
                    system_prompt=session_reduce_system_prompt(),
                    response_kind='candidates',
                ),
            )
        except (ModelPolicyError, ProviderSecretError):
            self._audit_reduce_skipped(session, data, reason='provider_error', draft_count=len(synthesized))

            return synthesized

        parsed_reduced = _parse_reduced_candidates(reduce_result.generated_body)
        if not parsed_reduced:
            reason = 'parse_failed' if parsed_reduced is None else 'empty'
            self._audit_reduce_skipped(session, data, reason=reason, draft_count=len(synthesized))

            return synthesized

        provider_results.append(reduce_result)
        provenance = self._provenance(reduce_result, used_resolved)
        provenance['reduced'] = True

        return self._merge_reduced_candidates(synthesized, parsed_reduced, provenance)

    def _merge_reduced_candidates(
        self,
        synthesized: list[tuple[SynthesizedCandidate, dict[str, object]]],
        reduced: tuple[_ReducedCandidate, ...],
        provenance: dict[str, object],
    ) -> list[tuple[SynthesizedCandidate, dict[str, object]]]:
        merged: list[tuple[SynthesizedCandidate, dict[str, object]]] = []
        for item in reduced:
            supporting: list[str] = []
            seen: set[str] = set()
            best_kind = ''
            best_confidence: Decimal | None = None
            for source_id in item.source_ids:
                if source_id < 0 or source_id >= len(synthesized):
                    continue

                draft_candidate, _draft_provenance = synthesized[source_id]
                for observation_id in draft_candidate.supporting_observation_ids:
                    if observation_id not in seen:
                        seen.add(observation_id)
                        supporting.append(observation_id)

                if best_confidence is None or draft_candidate.confidence > best_confidence:
                    best_confidence = draft_candidate.confidence
                    best_kind = draft_candidate.kind

            merged.append(
                (
                    SynthesizedCandidate(
                        title=item.title,
                        body=item.body,
                        confidence=item.confidence,
                        supporting_observation_ids=tuple(supporting),
                        kind=item.kind or best_kind,
                    ),
                    dict(provenance),
                ),
            )

        return merged

    def _get_or_create_candidate(
        self,
        session: AgentSession,
        candidate_input: SynthesizedCandidate,
        provenance: dict[str, object],
    ) -> tuple[MemoryCandidate, bool]:
        content_hash = session_candidate_content_hash(session.id, candidate_input.title, candidate_input.body)
        existing = (
            MemoryCandidate.objects.select_related('promoted_memory')
            .filter(
                organization_id=session.organization_id,
                project_id=session.project_id,
                content_hash=content_hash,
            )
            .first()
        )
        if existing is not None:
            return existing, False

        candidate = MemoryCandidate.objects.create(
            organization=session.organization,
            project=session.project,
            team=session.team,
            source_observation=None,
            title=candidate_input.title[:255],
            body=candidate_input.body,
            status=CandidateStatus.PROPOSED,
            visibility_scope=VisibilityScope.PROJECT,
            evidence=self._candidate_evidence(session, candidate_input, provenance),
            content_hash=content_hash,
            confidence=candidate_input.confidence,
            kind=candidate_input.kind,
        )

        return candidate, True

    def _candidate_evidence(
        self,
        session: AgentSession,
        candidate_input: SynthesizedCandidate,
        provenance: dict[str, object],
    ) -> list[dict[str, object]]:
        evidence: dict[str, object] = {
            'session_id': str(session.id),
            'kind': 'session_distillation',
            'supporting_observation_ids': list(candidate_input.supporting_observation_ids),
        }
        if candidate_input.parse_fallback:
            evidence['parse_fallback'] = True
        evidence.update(provenance)

        return [
            evidence,
        ]

    def _classify_existing(
        self,
        candidate: MemoryCandidate,
        auto_promoted: list[Memory],
        queued: list[MemoryCandidate],
    ) -> None:
        # Distillation re-runs are propose-only: an already-held candidate is never re-gated here even if the
        # threshold was later lowered. Promoting held candidates is the review queue's responsibility.
        if candidate.status == CandidateStatus.PROMOTED and candidate.promoted_memory_id:
            auto_promoted.append(candidate.promoted_memory)

            return

        queued.append(candidate)

    def _audit_held(
        self,
        session: AgentSession,
        candidate: MemoryCandidate,
        confidence: Decimal,
        threshold: Decimal,
        data: DistillSessionInput,
    ) -> None:
        AuditEvent.objects.create(
            organization=session.organization,
            project=session.project,
            team=session.team,
            event_type='MemoryCandidateHeldForReview',
            actor_type='system',
            target_type='memory_candidate',
            target_id=str(candidate.id),
            capability='memories:review',
            result=AuditResult.RECORDED,
            request_id=data.request_id,
            correlation_id=data.correlation_id,
            metadata=redact_value(
                {
                    'reason': 'below_auto_approve_threshold',
                    'candidate_id': str(candidate.id),
                    'confidence': str(confidence) if confidence is not None else None,
                    'threshold': str(threshold),
                    'source_observation_id': (
                        str(candidate.source_observation_id) if candidate.source_observation_id else None
                    ),
                    'session_id': str(session.id),
                },
            ),
        )

    def _audit_reduce_skipped(
        self,
        session: AgentSession,
        data: DistillSessionInput,
        *,
        reason: str,
        draft_count: int,
    ) -> None:
        AuditEvent.objects.create(
            organization=session.organization,
            project=session.project,
            team=session.team,
            event_type='SessionDistillationReduceSkipped',
            actor_type='system',
            target_type='agent_session',
            target_id=str(session.id),
            capability='memories:review',
            result=AuditResult.RECORDED,
            request_id=data.request_id,
            correlation_id=data.correlation_id,
            metadata={
                'reason': reason,
                'draft_count': draft_count,
                'session_id': str(session.id),
            },
        )


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


def _accepted_stage_rows(window: DistillationWindow, stage_kind: str) -> list[DistillationStage]:
    return list(
        DistillationStage.objects.filter(
            window=window,
            stage_kind=stage_kind,
            status=DistillationStageStatus.COMPLETE,
        )
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
        reduction_stages = _accepted_stage_rows(window, DistillationStageKind.REDUCE)
        try:
            pending_reduction = derive_first_pending_reduction_target(
                extraction_stages,
                reduction_stages,
                reduction_target=window.reduction_target,
                prompt_budget=window.chunk_char_budget,
            )
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
                reduction_target=window.reduction_target,
                prompt_budget=window.chunk_char_budget,
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


def run_session_distillation_with_tracking(
    session_id: uuid.UUID,
    *,
    request_id: str = '',
    correlation_id: str = '',
    auto_approve_threshold: Decimal | None = None,
    existing_run_id: uuid.UUID | None = None,
) -> DistillSessionResult:
    session = AgentSession.objects.select_related('organization', 'project', 'team').get(id=session_id)

    run = None
    if existing_run_id is not None:
        run = WorkflowRun.objects.filter(
            id=existing_run_id,
            organization=session.organization,
            project=session.project,
            run_type=WorkflowRunType.SESSION_DISTILLATION,
            status=WorkflowRunStatus.QUEUED,
        ).first()

    if run is None:
        run = WorkflowRun.objects.create(
            organization=session.organization,
            project=session.project,
            team=session.team,
            run_type=WorkflowRunType.SESSION_DISTILLATION,
            status=WorkflowRunStatus.QUEUED,
            input_snapshot={'session_id': str(session_id)},
            request_id=request_id,
            correlation_id=correlation_id,
        )

    run.status = WorkflowRunStatus.RUNNING

    run.started_at = timezone.now()

    run.save(update_fields=['status', 'started_at', 'updated_at'])

    try:
        result = DistillSession().execute(
            DistillSessionInput(
                session_id=session_id,
                request_id=request_id,
                correlation_id=correlation_id,
                auto_approve_threshold=auto_approve_threshold,
                run_id=str(run.id),
            ),
        )
    except Exception as error:
        run.status = WorkflowRunStatus.FAILED

        run.failure_reason = str(error)[:1024]

        run.finished_at = timezone.now()

        run.save(update_fields=['status', 'failure_reason', 'finished_at', 'updated_at'])

        raise

    run.status = WorkflowRunStatus.SUCCEEDED

    run.finished_at = timezone.now()

    if result.auto_promoted:
        run.result_memory = result.auto_promoted[0]

    run.provider_call_ids = list(result.provider_call_ids)

    update_fields = ['status', 'finished_at', 'result_memory', 'provider_call_ids', 'updated_at']
    if result.truncated:
        run.escalation = True

        update_fields.append('escalation')

    run.save(update_fields=update_fields)

    return result
