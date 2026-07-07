from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from decimal import Decimal

import structlog
from django.db import transaction
from django.utils import timezone

from engram.core.models import (
    AgentSession,
    AuditEvent,
    AuditResult,
    CandidateStatus,
    Memory,
    MemoryCandidate,
    Observation,
    VisibilityScope,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowRunType,
    clamp_memory_kind,
)
from engram.memory.candidate_parsing import (
    SynthesizedCandidate,
    _clamp_confidence,
    parse_synthesized_candidates,
    strip_json_fence,
    truncate_with_marker,
)
from engram.memory.curation import CurateMemoryCandidate, CurateMemoryCandidateInput
from engram.memory.services import (
    MemoryWorkerError,
    call_with_fallback,
    is_auto_promotable,
    redact_error,
    redact_text,
    redact_value,
    resolve_auto_approve_threshold,
)
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


def _observation_block(observation: Observation, cap: int) -> str:
    block = '\n'.join(
        [
            f'Observation: {observation.id}',
            f'Title: {redact_text(observation.title)}',
            f'Body: {redact_text(observation.body)}',
            f'Facts: {redact_value(observation.facts)}',
            f'Narrative: {redact_text(observation.narrative)}',
            f'Concepts: {redact_value(observation.concepts)}',
            f'Files read: {redact_value(observation.files_read)}',
            f'Files modified: {redact_value(observation.files_modified)}',
        ],
    )
    return truncate_with_marker(block, cap)


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


def _distill_max_chunks() -> int:
    return int(os.environ.get('ENGRAM_DISTILL_MAX_CHUNKS', '8'))


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
        observations = self._session_observations(session)
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
        max_chunks = _distill_max_chunks()
        truncated = len(chunks) > max_chunks
        if truncated:
            chunks_total = len(chunks)
            chunks = chunks[:max_chunks]
            self._audit_truncated(
                session,
                data,
                chunks_total=chunks_total,
                chunks_processed=len(chunks),
                observation_count=len(observations),
                observations_distilled=sum(len(chunk) for chunk in chunks),
            )

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
            truncated=truncated,
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

    def _session_observations(self, session: AgentSession) -> list[Observation]:
        return list(
            Observation.objects.filter(
                organization_id=session.organization_id,
                project_id=session.project_id,
                session=session,
            ).order_by('prompt_number', 'created_at'),
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

    def _audit_truncated(
        self,
        session: AgentSession,
        data: DistillSessionInput,
        *,
        chunks_total: int,
        chunks_processed: int,
        observation_count: int,
        observations_distilled: int,
    ) -> None:
        AuditEvent.objects.create(
            organization=session.organization,
            project=session.project,
            team=session.team,
            event_type='SessionDistillationTruncated',
            actor_type='system',
            target_type='agent_session',
            target_id=str(session.id),
            capability='memories:review',
            result=AuditResult.RECORDED,
            request_id=data.request_id,
            correlation_id=data.correlation_id,
            metadata={
                'chunks_total': chunks_total,
                'chunks_processed': chunks_processed,
                'observation_count': observation_count,
                'observations_distilled': observations_distilled,
            },
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


def run_session_distillation_with_tracking(
    session_id: uuid.UUID,
    *,
    request_id: str = '',
    correlation_id: str = '',
    auto_approve_threshold: Decimal | None = None,
) -> DistillSessionResult:
    session = AgentSession.objects.select_related('organization', 'project', 'team').get(id=session_id)

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
