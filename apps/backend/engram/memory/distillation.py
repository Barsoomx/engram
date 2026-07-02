from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

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
)
from engram.memory.curation import CurateMemoryCandidate, CurateMemoryCandidateInput
from engram.memory.services import (
    MemoryWorkerError,
    is_auto_promotable,
    redact_error,
    redact_text,
    redact_value,
    resolve_auto_approve_threshold,
)
from engram.model_policy.services import (
    ModelPolicyError,
    ProviderCallInput,
    ProviderCallResult,
    ProviderSecretError,
    ResolvedModelPolicy,
    ResolveModelPolicy,
    ResolveModelPolicyInput,
    get_provider_gateway,
)

logger = structlog.get_logger(__name__)

_FALLBACK_CONFIDENCE = Decimal('0.500')
_CONFIDENCE_QUANTUM = Decimal('0.001')


@dataclass(frozen=True)
class DistillSessionInput:
    session_id: uuid.UUID
    request_id: str = ''
    correlation_id: str = ''
    auto_approve_threshold: Decimal | None = None
    run_id: str = ''


@dataclass(frozen=True)
class SynthesizedCandidate:
    title: str
    body: str
    confidence: Decimal
    supporting_observation_ids: tuple[str, ...]


@dataclass(frozen=True)
class DistillSessionResult:
    session: AgentSession
    auto_promoted: tuple[Memory, ...]
    queued_for_review: tuple[MemoryCandidate, ...]
    provider_call_ids: tuple[str, ...] = ()


def session_distillation_system_prompt() -> str:
    return (
        'You are a session distillation engine for software engineering sessions.\n'
        'Given structured observations from one agent session, synthesize durable, '
        'runtime-neutral engineering memories.\n'
        '\n'
        'Rules:\n'
        '- Output a JSON array only. Each element is an object with the keys '
        '"title", "body", "confidence", "supporting_observation_ids".\n'
        '- "confidence" is a number between 0 and 1 reflecting how durable and reliable the memory is.\n'
        '- "supporting_observation_ids" lists the observation ids the memory is derived from.\n'
        '- Consolidate related observations into a small number of high-signal memories.\n'
        '- Preserve exact identifiers verbatim: file paths, function names, class names, '
        'CLI commands, error strings, ticket identifiers, URLs, and config keys.\n'
        '- Drop session chatter, acknowledgements, timestamps, and credential-shaped values.\n'
        '- Do not invent facts not present in the input.\n'
        '- Do not name any AI assistant, tool, or product by brand.'
    )


def session_distillation_prompt(observations: list[Observation]) -> str:
    blocks = []
    for observation in observations:
        blocks.append(
            '\n'.join(
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
            ),
        )

    return '\n\n'.join(blocks)


def session_candidate_content_hash(session_id: uuid.UUID, title: str, body: str) -> str:
    return hashlib.sha256(f'{session_id}:{title}:{body}'.encode()).hexdigest()


def _clamp_confidence(value: object) -> Decimal:
    try:
        confidence = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return _FALLBACK_CONFIDENCE

    confidence = max(Decimal('0'), min(Decimal('1'), confidence))

    return confidence.quantize(_CONFIDENCE_QUANTUM)


def _fallback_candidate(raw_body: str) -> SynthesizedCandidate:
    text = raw_body.strip()
    title = text.splitlines()[0][:255] if text else 'Session distillation'

    return SynthesizedCandidate(
        title=title,
        body=text or title,
        confidence=_FALLBACK_CONFIDENCE,
        supporting_observation_ids=(),
    )


def parse_synthesized_candidates(raw_body: str) -> tuple[SynthesizedCandidate, ...]:
    try:
        parsed = json.loads(raw_body)
    except (json.JSONDecodeError, TypeError):
        return (_fallback_candidate(raw_body),)

    if not isinstance(parsed, list):
        return (_fallback_candidate(raw_body),)

    candidates: list[SynthesizedCandidate] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        title = str(item.get('title') or '').strip()
        body = str(item.get('body') or '').strip()
        if not title and not body:
            continue
        supporting = tuple(str(value) for value in (item.get('supporting_observation_ids') or []))
        candidates.append(
            SynthesizedCandidate(
                title=(title or body)[:255],
                body=body or title,
                confidence=_clamp_confidence(item.get('confidence')),
                supporting_observation_ids=supporting,
            ),
        )

    if not candidates:
        return (_fallback_candidate(raw_body),)

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
        prompt = session_distillation_prompt(observations)
        provider_result, resolved = self._synthesize(session, prompt, correlation_id, data.run_id)
        synthesized = parse_synthesized_candidates(provider_result.generated_body)
        provenance = self._provenance(provider_result, resolved)

        with transaction.atomic():
            locked_session = self._lock_session(data.session_id)
            threshold = resolve_auto_approve_threshold(locked_session.organization, data.auto_approve_threshold)

            to_curate: list[MemoryCandidate] = []
            auto_promoted: list[Memory] = []
            queued: list[MemoryCandidate] = []
            for candidate_input in synthesized:
                candidate, created = self._get_or_create_candidate(locked_session, candidate_input, provenance)
                if not created:
                    self._classify_existing(candidate, auto_promoted, queued)
                    continue
                if is_auto_promotable(candidate_input.confidence, threshold):
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
            provider_call_ids=(str(provider_result.call_record_id),),
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

    def _synthesize(
        self,
        session: AgentSession,
        prompt: str,
        correlation_id: str,
        run_id: str,
    ) -> tuple[ProviderCallResult, ResolvedModelPolicy]:
        request_id = (
            f'distill-session:{session.id}:{run_id}:curation'
            if run_id
            else f'distill-session:{session.id}:curation'
        )
        try:
            resolved = self._resolve_policy(session)
            provider_result = get_provider_gateway(resolved.policy).call(
                ProviderCallInput(
                    organization_id=session.organization_id,
                    project_id=session.project_id,
                    team_id=session.team_id,
                    policy=resolved.policy,
                    request_id=request_id,
                    trace_id=correlation_id or f'distill-session:{session.id}',
                    prompt=prompt,
                    system_prompt=session_distillation_system_prompt(),
                    response_kind='candidates',
                ),
            )
        except (ModelPolicyError, ProviderSecretError) as error:
            raise MemoryWorkerError(redact_error(str(error)), retryable=getattr(error, 'retryable', False)) from error

        return provider_result, resolved

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
            metadata={
                'confidence': str(confidence),
                'threshold': str(threshold),
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

    run.save(
        update_fields=[
            'status',
            'finished_at',
            'result_memory',
            'provider_call_ids',
            'updated_at',
        ],
    )

    return result
