from __future__ import annotations

import datetime
import hashlib
import os
import uuid
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q, QuerySet
from django.db.models.deletion import ProtectedError
from django.utils import timezone
from rest_framework import status as drf_status

from engram.access.services import AccessDeniedError, EffectiveScope
from engram.core.domain.usecases.errors import DomainError
from engram.core.models import (
    AuditEvent,
    AuditResult,
    CandidateStatus,
    LinkType,
    Memory,
    MemoryCandidate,
    MemoryConflict,
    MemoryLink,
    MemoryStatus,
    MemoryTransition,
    MemoryVersion,
    Observation,
    Organization,
    OrganizationSettings,
    Project,
    RetrievalDocument,
    VisibilityScope,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowRunType,
)
from engram.core.redaction import redact_value as core_redact_value
from engram.memory.candidate_parsing import (
    parse_synthesized_candidates,
    truncate_with_marker,
)
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
)

logger = structlog.get_logger(__name__)

if TYPE_CHECKING:
    from engram.memory.transitions import MemoryFence
    from engram.memory.work_execution import WorkClaim

_ProviderGateway = FakeProviderGateway | OpenAICompatibleGateway | AnthropicMessagesGateway


def _audit_provider_fallback_used(
    resolved: ResolvedModelPolicy,
    fallback_resolved: ResolvedModelPolicy,
    data: ProviderCallInput,
    error: ModelPolicyError,
) -> None:
    AuditEvent.objects.create(
        organization_id=data.organization_id,
        project_id=data.project_id,
        team_id=data.team_id,
        event_type='ProviderFallbackUsed',
        actor_type='system',
        target_type='model_policy',
        target_id=str(resolved.policy.id),
        capability='memories:review',
        result=AuditResult.RECORDED,
        request_id=data.request_id,
        correlation_id=data.trace_id,
        metadata={
            'primary_policy_id': str(resolved.policy.id),
            'fallback_policy_id': str(fallback_resolved.policy.id),
            'task_type': resolved.policy.task_type,
            'error_code': error.code,
        },
    )


def call_with_fallback(
    resolved: ResolvedModelPolicy,
    gateway: _ProviderGateway,
    data: ProviderCallInput,
) -> tuple[ProviderCallResult, ResolvedModelPolicy]:
    try:
        return gateway.call(data), resolved
    except ModelPolicyError as error:
        if not resolved.policy.fallback_enabled:
            raise

        fallback_resolved = ResolveModelPolicy().execute(
            ResolveModelPolicyInput(
                organization_id=data.organization_id,
                project_id=data.project_id,
                team_id=data.team_id,
                task_type='generation',
            ),
        )
        if fallback_resolved.policy.id == resolved.policy.id:
            raise

        fallback_gateway = get_provider_gateway(fallback_resolved.policy)
        fallback_result = fallback_gateway.call(replace(data, policy=fallback_resolved.policy))
        _audit_provider_fallback_used(resolved, fallback_resolved, data, error)

        return fallback_result, fallback_resolved


@dataclass(frozen=True)
class MemoryCandidateWorkerInput:
    observation_id: uuid.UUID
    worker_id: str = 'memory-worker'


@dataclass(frozen=True)
class MemoryCandidateWorkerResult:
    candidate: MemoryCandidate | None
    duplicate: bool
    memory: Memory | None = None
    memory_version: MemoryVersion | None = None
    retrieval_document: RetrievalDocument | None = None
    held_for_review: bool = False
    curated_decision: str = ''
    skipped: bool = False


class MemoryWorkerError(Exception):
    def __init__(self, message: str, *, retryable: bool = False, code: str = '') -> None:
        super().__init__(message)
        self.retryable = retryable
        self.code = code


@dataclass(frozen=True)
class GeneratedMemoryCandidate:
    title: str
    body: str
    evidence: list[dict[str, object]]
    confidence: Decimal = Decimal('0.000')
    kind: str = ''


@dataclass(frozen=True)
class PromoteMemoryCandidateInput:
    candidate_id: uuid.UUID
    defer_embedding: bool = False
    actor_type: str = 'memory_worker'
    actor_id: str = 'memory-worker'
    capability: str = 'memories:write'
    request_id: str = ''
    correlation_id: str = ''
    reason: str = 'candidate promotion'
    origin: str = 'memory-worker'
    work_claim: WorkClaim | None = None


@dataclass(frozen=True)
class PromoteMemoryCandidateResult:
    candidate: MemoryCandidate
    memory: Memory
    memory_version: MemoryVersion
    retrieval_document: RetrievalDocument
    duplicate: bool


@dataclass(frozen=True)
class MemoryFeedbackInput:
    scope: EffectiveScope
    memory_id: uuid.UUID
    project_id: uuid.UUID
    team_id: uuid.UUID | None
    action: str
    reason: str
    request_id: str
    correlation_id: str = ''


@dataclass(frozen=True)
class MemoryFeedbackResult:
    memory: Memory
    action: str
    retrieval_documents_updated: int
    already_applied: bool

    def to_response(self) -> dict[str, object]:
        return {
            'memory_id': str(self.memory.id),
            'project_id': str(self.memory.project_id),
            'team_id': str(self.memory.team_id) if self.memory.team_id else '',
            'action': self.action,
            'stale': self.memory.stale,
            'refuted': self.memory.refuted,
            'retrieval_documents_updated': self.retrieval_documents_updated,
            'already_applied': self.already_applied,
        }


class MemoryFeedbackError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class RecordMemoryFeedback:
    def execute(self, data: MemoryFeedbackInput) -> MemoryFeedbackResult:
        scope = data.scope
        with transaction.atomic():
            memory = self._lock_memory(data, scope)
            self._ensure_team_scope(memory, scope)
            already_applied = self._already_applied(memory, data.action)
            if already_applied:
                updated = self._matching_retrieval_documents(memory, data.action)
            else:
                memory, updated = self._apply_transition(memory, scope, data)
            self._log(memory, data.action, updated, already_applied)

        return MemoryFeedbackResult(
            memory=memory,
            action=data.action,
            retrieval_documents_updated=updated,
            already_applied=already_applied,
        )

    def _lock_memory(self, data: MemoryFeedbackInput, scope: EffectiveScope) -> Memory:
        return lock_memory_for_update(scope, data.project_id, data.memory_id, MemoryFeedbackError)

    def _ensure_team_scope(self, memory: Memory, scope: EffectiveScope) -> None:
        ensure_memory_team_scope(memory, scope)

    def _already_applied(self, memory: Memory, action: str) -> bool:
        return bool(getattr(memory, action))

    def _matching_retrieval_documents(self, memory: Memory, action: str) -> int:
        return RetrievalDocument.objects.filter(
            organization=memory.organization,
            project=memory.project,
            memory=memory,
            **{action: True},
        ).count()

    def _apply_transition(
        self,
        memory: Memory,
        scope: EffectiveScope,
        data: MemoryFeedbackInput,
    ) -> tuple[Memory, int]:
        from engram.memory.transitions import (
            MarkMemoryStale,
            MemoryStateInput,
            MemoryTransitionError,
            RefuteMemory,
            TransitionRequest,
            TransitionScope,
            build_memory_fence,
        )

        transition = MarkMemoryStale() if data.action == 'stale' else RefuteMemory()
        try:
            result = transition.execute(
                MemoryStateInput(
                    request=TransitionRequest(
                        scope=TransitionScope(
                            organization_id=memory.organization_id,
                            project_id=memory.project_id,
                            team_id=memory.team_id,
                        ),
                        idempotency_key=f'memory-feedback:{memory.id}:{data.action}:{data.request_id}:v1',
                        actor_type=scope.actor_type,
                        actor_id=scope.actor_id,
                        capability='memories:review',
                        request_id=data.request_id,
                        correlation_id=data.correlation_id,
                        reason=data.reason,
                        origin='memory-feedback',
                    ),
                    memory_fence=build_memory_fence(memory),
                ),
            )
        except MemoryTransitionError as error:
            raise MemoryFeedbackError(error.code, str(error)) from error

        return result.memory, int(bool(getattr(result.retrieval_document, data.action)))

    def _log(self, memory: Memory, action: str, updated: int, already_applied: bool) -> None:
        logger.info(
            'memory_feedback_recorded',
            organization_id=str(memory.organization_id),
            project_id=str(memory.project_id),
            memory_id=str(memory.id),
            action=action,
            already_applied=already_applied,
            retrieval_documents_updated=updated,
        )


def _is_skip(generated: GeneratedMemoryCandidate) -> bool:
    title = generated.title.strip()
    body = generated.body.strip()

    if not title or not body:
        return True

    return title.upper() == 'SKIP' and body.upper() in ('', 'SKIP')


class ProcessObservationRecorded:
    def execute(self, data: MemoryCandidateWorkerInput) -> MemoryCandidateWorkerResult:
        observation = self._read_observation(data.observation_id)
        correlation_id = self._originating_correlation_id(observation)
        structlog.contextvars.bind_contextvars(
            correlation_id=correlation_id,
            observation_id=str(data.observation_id),
        )
        if self._already_skipped(observation):
            return MemoryCandidateWorkerResult(candidate=None, duplicate=True, skipped=True)

        pre_llm_skip_reason = self._pre_llm_skip_reason(observation)
        if pre_llm_skip_reason is not None:
            self._audit_skipped(observation, reason=pre_llm_skip_reason)
            logger.info(
                'memory_candidate_skipped',
                observation_id=str(observation.id),
                reason=pre_llm_skip_reason,
            )

            return MemoryCandidateWorkerResult(candidate=None, duplicate=False, skipped=True)

        generated = self._generate_candidate(observation, correlation_id=correlation_id)

        if _is_skip(generated):
            self._audit_skipped(observation, reason='no_durable_signal')
            logger.info(
                'memory_candidate_skipped',
                observation_id=str(observation.id),
                reason='no_durable_signal',
            )

            return MemoryCandidateWorkerResult(candidate=None, duplicate=False, skipped=True)

        with transaction.atomic():
            observation = self._lock_observation(data.observation_id)
            candidate, candidate_created = self._get_or_create_candidate(observation, generated)
            threshold = resolve_auto_approve_threshold(observation.organization)
            already_promoted = candidate.status == CandidateStatus.PROMOTED and candidate.promoted_memory_id is not None
            promotable = is_auto_promotable(candidate.confidence, threshold) and not self._is_parse_fallback(candidate)
            should_promote = already_promoted or (candidate.decision_work_contract_version == 1 and promotable)
            if not should_promote and candidate_created:
                self._audit_held(observation, candidate, threshold)

        if already_promoted and candidate.decision_work_contract_version != 1:
            memory = candidate.promoted_memory
            if memory is None:
                raise MemoryWorkerError('legacy promoted candidate has no memory pointer')
            version = MemoryVersion.objects.filter(memory=memory, version=memory.current_version).first()
            document = RetrievalDocument.objects.filter(memory=memory, memory_version=version).first()

            return MemoryCandidateWorkerResult(
                candidate=candidate,
                duplicate=True,
                memory=memory,
                memory_version=version,
                retrieval_document=document,
                curated_decision='legacy_replay',
            )

        if should_promote:
            from engram.memory.curation import CurateMemoryCandidate, CurateMemoryCandidateInput

            curation = CurateMemoryCandidate().execute(
                CurateMemoryCandidateInput(candidate_id=candidate.id, correlation_id=correlation_id),
            )

            return MemoryCandidateWorkerResult(
                candidate=curation.candidate,
                duplicate=not candidate_created or curation.duplicate,
                memory=curation.memory,
                memory_version=curation.memory_version,
                retrieval_document=curation.retrieval_document,
                curated_decision=curation.decision,
            )

        return MemoryCandidateWorkerResult(
            candidate=candidate,
            duplicate=not candidate_created,
            held_for_review=True,
        )

    def _originating_correlation_id(self, observation: Observation) -> str:
        raw = observation.raw_event
        if raw is not None:
            return raw.correlation_id or raw.request_id or str(observation.id)

        return str(observation.id)

    def _read_observation(self, observation_id: uuid.UUID) -> Observation:
        try:
            return Observation.objects.select_related('organization', 'project', 'team', 'raw_event').get(
                id=observation_id,
            )
        except Observation.DoesNotExist as error:
            raise MemoryWorkerError('observation not found') from error

    def _lock_observation(self, observation_id: uuid.UUID) -> Observation:
        try:
            return (
                Observation.objects.select_for_update(of=('self',))
                .select_related('organization', 'project', 'team', 'raw_event')
                .get(id=observation_id)
            )
        except Observation.DoesNotExist as error:
            raise MemoryWorkerError('observation not found') from error

    def _get_or_create_candidate(
        self,
        observation: Observation,
        generated: GeneratedMemoryCandidate,
    ) -> tuple[MemoryCandidate, bool]:
        candidate_hash = memory_candidate_content_hash(observation)
        candidate = MemoryCandidate.objects.filter(
            organization=observation.organization,
            project=observation.project,
            content_hash=candidate_hash,
        ).first()
        if candidate is not None:
            if not self._has_provider_provenance(candidate):
                candidate.title = generated.title
                candidate.body = generated.body
                candidate.evidence = generated.evidence
                candidate.confidence = generated.confidence
                candidate.kind = generated.kind
                candidate.save(update_fields=['title', 'body', 'evidence', 'confidence', 'kind', 'updated_at'])

            return candidate, False

        candidate = MemoryCandidate.objects.create(
            organization=observation.organization,
            project=observation.project,
            team=observation.team,
            source_observation=observation,
            title=generated.title,
            body=generated.body,
            status=CandidateStatus.PROPOSED,
            visibility_scope=VisibilityScope.PROJECT,
            evidence=generated.evidence,
            content_hash=candidate_hash,
            confidence=generated.confidence,
            kind=generated.kind,
        )

        return candidate, True

    def _generate_candidate(self, observation: Observation, *, correlation_id: str = '') -> GeneratedMemoryCandidate:
        try:
            resolved = ResolveModelPolicy().execute(
                ResolveModelPolicyInput(
                    organization_id=observation.organization_id,
                    project_id=observation.project_id,
                    team_id=observation.team_id,
                    task_type='generation',
                ),
            )
            provider_result = get_provider_gateway(resolved.policy).call(
                ProviderCallInput(
                    organization_id=observation.organization_id,
                    project_id=observation.project_id,
                    team_id=observation.team_id,
                    policy=resolved.policy,
                    request_id=f'memory-worker:{observation.id}:generation',
                    trace_id=correlation_id or f'memory-worker:{observation.id}',
                    prompt=realtime_provider_prompt(observation, _realtime_prompt_char_budget()),
                    system_prompt=realtime_generation_system_prompt(),
                    response_kind='candidates',
                ),
            )
        except (ModelPolicyError, ProviderSecretError) as error:
            raise MemoryWorkerError(redact_error(str(error)), retryable=getattr(error, 'retryable', False)) from error

        provenance = {
            'provider_call_id': str(provider_result.call_record_id),
            'provider': provider_result.provider,
            'model': provider_result.model,
            'policy_id': str(resolved.policy.id),
            'policy_version': resolved.policy.version,
            'task_type': resolved.policy.task_type,
            'redaction_state': provider_result.redaction_state,
        }
        candidates = parse_synthesized_candidates(provider_result.generated_body)
        if not candidates:
            return GeneratedMemoryCandidate(title='', body='', evidence=[])

        synthesized = candidates[0]
        evidence = candidate_evidence(observation, synthesized.title, provenance)
        if synthesized.parse_fallback:
            evidence[0]['parse_fallback'] = True

        return GeneratedMemoryCandidate(
            title=synthesized.title,
            body=synthesized.body,
            evidence=evidence,
            confidence=synthesized.confidence,
            kind=synthesized.kind,
        )

    def _has_provider_provenance(self, candidate: MemoryCandidate) -> bool:
        if not candidate.evidence:
            return False
        evidence = candidate.evidence[0]

        return isinstance(evidence, dict) and bool(evidence.get('provider_call_id'))

    def _is_parse_fallback(self, candidate: MemoryCandidate) -> bool:
        if not candidate.evidence:
            return False
        evidence = candidate.evidence[0]

        return isinstance(evidence, dict) and bool(evidence.get('parse_fallback'))

    def _pre_llm_skip_reason(self, observation: Observation) -> str | None:
        if observation.observation_type in _LIFECYCLE_OBSERVATION_TYPES:
            return 'lifecycle_event'

        content_length = len(observation.title.strip()) + len(observation.body.strip())
        if content_length < _realtime_min_content_chars():
            return 'content_below_min'

        return None

    def _already_skipped(self, observation: Observation) -> bool:
        return AuditEvent.objects.filter(
            organization=observation.organization,
            project=observation.project,
            event_type='MemoryCandidateSkipped',
            target_type='observation',
            target_id=str(observation.id),
        ).exists()

    def _audit_skipped(self, observation: Observation, *, reason: str) -> None:
        AuditEvent.objects.create(
            organization=observation.organization,
            project=observation.project,
            team=observation.team,
            event_type='MemoryCandidateSkipped',
            actor_type='system',
            target_type='observation',
            target_id=str(observation.id),
            capability='memories:review',
            result=AuditResult.RECORDED,
            metadata={'reason': reason},
        )

    def _audit_held(self, observation: Observation, candidate: MemoryCandidate, threshold: Decimal) -> None:
        AuditEvent.objects.create(
            organization=observation.organization,
            project=observation.project,
            team=observation.team,
            event_type='MemoryCandidateHeldForReview',
            actor_type='system',
            target_type='memory_candidate',
            target_id=str(candidate.id),
            capability='memories:review',
            result=AuditResult.RECORDED,
            metadata=redact_value(
                {
                    'reason': 'below_auto_approve_threshold',
                    'candidate_id': str(candidate.id),
                    'confidence': str(candidate.confidence) if candidate.confidence is not None else None,
                    'threshold': str(threshold),
                    'source_observation_id': str(observation.id),
                },
            ),
        )


_LIFECYCLE_OBSERVATION_TYPES = frozenset({'session_start', 'session_end'})
_DEFAULT_REALTIME_MIN_CONTENT_CHARS = 80
_DEFAULT_REALTIME_PROMPT_CHAR_BUDGET = 12000


def _realtime_min_content_chars() -> int:
    return int(os.environ.get('ENGRAM_REALTIME_MIN_CONTENT_CHARS', str(_DEFAULT_REALTIME_MIN_CONTENT_CHARS)))


def _realtime_prompt_char_budget() -> int:
    return int(os.environ.get('ENGRAM_REALTIME_PROMPT_CHAR_BUDGET', str(_DEFAULT_REALTIME_PROMPT_CHAR_BUDGET)))


def is_auto_promotable(confidence: Decimal | None, threshold: Decimal) -> bool:
    return confidence is not None and confidence >= threshold


def resolve_auto_approve_threshold(organization: Organization, override: Decimal | None = None) -> Decimal:
    if override is not None:
        return override

    org_threshold = (
        OrganizationSettings.objects.filter(organization=organization)
        .values_list('distillation_auto_approve_threshold', flat=True)
        .first()
    )
    if org_threshold is not None:
        return org_threshold

    return Decimal(str(settings.ENGRAM_DISTILLATION_AUTO_APPROVE_THRESHOLD))


def memory_candidate_content_hash(observation: Observation) -> str:
    return hashlib.sha256(observation.content_hash.encode()).hexdigest()


def candidate_evidence(
    observation: Observation,
    title: str | None = None,
    provenance: dict[str, object] | None = None,
) -> list[dict[str, object]]:
    evidence = {
        'observation_id': str(observation.id),
        'raw_event_id': str(observation.raw_event_id) if observation.raw_event_id else '',
        'event_type': observation.source_metadata.get('event_type', observation.observation_type),
        'title': redact_text(title or observation.title),
        'files_read': redact_value(observation.files_read),
        'files_modified': redact_value(observation.files_modified),
    }
    if provenance:
        evidence.update(redact_value(provenance))

    return [
        evidence,
    ]


def distillation_system_prompt() -> str:
    return (
        'You are a memory distillation engine for software engineering sessions.\n'
        'Given structured observation data, produce a concise, durable, runtime-neutral engineering memory.\n'
        '\n'
        'Rules:\n'
        '- Output the Title on the first line (single line, under 255 characters).\n'
        '- Output the Body on the remaining lines.\n'
        '- Preserve exact identifiers verbatim: file paths, function names, class names, '
        'CLI commands, error strings, ticket identifiers, URLs, and config keys.\n'
        '- Be concise. Drop session chatter, acknowledgements, timestamps, and credential-shaped values.\n'
        '- Do not invent facts not present in the input.\n'
        '- If the observation contains no durable engineering signal (routine status checks, empty '
        'search results, plain acknowledgements), output only the word SKIP as the entire response, '
        'with no title, body, or explanation.\n'
        '- Do not name any AI assistant, tool, or product by brand.\n'
        '- The Title must stand alone as a searchable summary.\n'
        '- The Body must be self-contained for future retrieval.'
    )


def provider_prompt(observation: Observation) -> str:
    return '\n'.join(
        [
            f'Title: {redact_text(observation.title)}',
            f'Body: {redact_text(observation.body)}',
            f'Facts: {redact_value(observation.facts)}',
            f'Narrative: {redact_text(observation.narrative)}',
            f'Concepts: {redact_value(observation.concepts)}',
            f'Files read: {redact_value(observation.files_read)}',
            f'Files modified: {redact_value(observation.files_modified)}',
            f'Source metadata: {redact_value(observation.source_metadata)}',
        ],
    )


def realtime_provider_prompt(observation: Observation, cap: int) -> str:
    return truncate_with_marker(provider_prompt(observation), cap)


def realtime_generation_system_prompt() -> str:
    return (
        'You are a memory distillation engine for software engineering sessions.\n'
        'Given a single structured observation, decide whether it carries a durable, '
        'runtime-neutral engineering memory.\n'
        '\n'
        'Rules:\n'
        '- Output a single JSON object only, with exactly one key "memories".\n'
        '- "memories" is an array with at most one object with the keys '
        '"title", "body", "confidence", and optionally "kind".\n'
        '- If the observation carries no durable engineering signal (routine status checks, empty '
        'search results, plain acknowledgements), output {"memories": []}.\n'
        '- "confidence" is a number between 0 and 1: 0.9 or higher for verified facts with direct '
        'evidence, 0.6-0.8 for plausible conclusions, 0.3-0.5 for unverified hypotheses, below 0.3 '
        'for speculation.\n'
        '- "kind" is optional: one of "decision", "convention", "gotcha", "architecture", "incident" '
        'when the memory clearly fits one of those categories, omitted otherwise.\n'
        '- Preserve exact identifiers verbatim: file paths, function names, class names, '
        'CLI commands, error strings, ticket identifiers, URLs, and config keys.\n'
        '- Drop session chatter, acknowledgements, timestamps, and credential-shaped values.\n'
        '- Do not invent facts not present in the input.\n'
        '- Do not name any AI assistant, tool, or product by brand.'
    )


def redact_value(value: object) -> object:
    return core_redact_value(value).value


def redact_text(value: str) -> str:
    return str(redact_value(value))


def redact_error(message: str) -> str:
    return redact_text(message)


class PromoteMemoryCandidate:
    def execute(self, data: PromoteMemoryCandidateInput) -> PromoteMemoryCandidateResult:
        from engram.memory.candidate_decision_work import evidence_manifest
        from engram.memory.transitions import (
            CandidateFence,
            TransitionRequest,
            TransitionScope,
        )
        from engram.memory.transitions import (
            PromoteMemoryCandidate as AtomicPromoteMemoryCandidate,
        )
        from engram.memory.transitions import (
            PromoteMemoryCandidateInput as AtomicPromoteMemoryCandidateInput,
        )

        try:
            candidate = MemoryCandidate.objects.get(id=data.candidate_id)
        except MemoryCandidate.DoesNotExist as error:
            raise MemoryWorkerError('memory candidate not found') from error
        if candidate.decision_work_contract_version != 1:
            raise MemoryWorkerError(
                'legacy v0 memory candidate promotion is disabled; '
                'typed promotion requires decision work contract version 1',
                code='legacy_candidate_contract',
            )

        _entries, manifest_hash = evidence_manifest(candidate)
        atomic_result = AtomicPromoteMemoryCandidate().execute(
            AtomicPromoteMemoryCandidateInput(
                request=TransitionRequest(
                    scope=TransitionScope(
                        organization_id=candidate.organization_id,
                        project_id=candidate.project_id,
                        team_id=candidate.team_id,
                    ),
                    idempotency_key=f'candidate:{candidate.id}:settle:v1',
                    actor_type=data.actor_type,
                    actor_id=data.actor_id,
                    capability=data.capability,
                    request_id=data.request_id or f'typed-promotion:{candidate.id}',
                    correlation_id=data.correlation_id or f'typed-promotion:{candidate.id}',
                    reason=data.reason,
                    origin=data.origin,
                ),
                candidate_fence=CandidateFence(
                    candidate_id=candidate.id,
                    candidate_content_hash=candidate.content_hash,
                    evidence_manifest_hash=manifest_hash,
                ),
                work_claim=data.work_claim,
            ),
        )
        candidate.refresh_from_db()

        return PromoteMemoryCandidateResult(
            candidate=candidate,
            memory=atomic_result.memory,
            memory_version=atomic_result.memory_version,
            retrieval_document=atomic_result.retrieval_document,
            duplicate=atomic_result.duplicate,
        )


@dataclass(frozen=True)
class UpdateMemoryBodyInput:
    scope: EffectiveScope
    memory_id: uuid.UUID
    project_id: uuid.UUID
    team_id: uuid.UUID | None
    body: str
    reason: str
    request_id: str
    correlation_id: str = ''


@dataclass(frozen=True)
class UpdateMemoryBodyResult:
    memory: Memory
    memory_version: MemoryVersion
    retrieval_document: RetrievalDocument

    def to_response(self) -> dict[str, object]:
        return {
            'memory_id': str(self.memory.id),
            'project_id': str(self.memory.project_id),
            'team_id': str(self.memory.team_id) if self.memory.team_id else '',
            'current_version': self.memory.current_version,
            'memory_version_id': str(self.memory_version.id),
            'retrieval_document_id': str(self.retrieval_document.id),
        }


MEMORY_VERSION_STATUS = {
    'memory_not_found': drf_status.HTTP_404_NOT_FOUND,
}


class MemoryVersionError(DomainError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(
            message,
            error_code=code,
            status_code=MEMORY_VERSION_STATUS.get(code, drf_status.HTTP_400_BAD_REQUEST),
        )
        self.code = code


def lock_memory_for_update(
    scope: EffectiveScope,
    project_id: uuid.UUID,
    memory_id: uuid.UUID,
    error_cls: type[Exception],
) -> Memory:
    memory = (
        Memory.objects.select_for_update()
        .filter(
            organization_id=scope.organization_id,
            project_id=project_id,
            id=memory_id,
        )
        .first()
    )
    if memory is None:
        raise error_cls('memory_not_found', 'Memory was not found')

    return memory


def ensure_memory_team_scope(memory: Memory, scope: EffectiveScope) -> None:
    if (
        memory.visibility_scope == VisibilityScope.TEAM
        and memory.team_id is not None
        and memory.team_id not in scope.team_ids
    ):
        raise AccessDeniedError('team_scope_denied', 'Memory is outside effective team scope')


class UpdateMemoryBody:
    def execute(self, data: UpdateMemoryBodyInput) -> UpdateMemoryBodyResult:
        from engram.memory.transitions import (
            MemoryTransitionError,
            ReviseMemory,
            ReviseMemoryInput,
            TransitionRequest,
            TransitionScope,
            build_memory_fence,
        )

        scope = data.scope
        with transaction.atomic():
            memory = lock_memory_for_update(scope, data.project_id, data.memory_id, MemoryVersionError)
            ensure_memory_team_scope(memory, scope)
            if memory.stale or memory.refuted:
                raise MemoryVersionError('memory_not_editable', 'Memory is stale or refuted and cannot be edited')

            latest_version = memory.versions.order_by('-version').first()
            if latest_version is not None and latest_version.body == data.body:
                retrieval_document = RetrievalDocument.objects.filter(memory_version=latest_version).first()
                if retrieval_document is None:
                    raise MemoryVersionError(
                        'projection_missing',
                        'Memory version has no exact retrieval document',
                    )

                return UpdateMemoryBodyResult(
                    memory=memory,
                    memory_version=latest_version,
                    retrieval_document=retrieval_document,
                )

            try:
                result = ReviseMemory().execute(
                    ReviseMemoryInput(
                        request=TransitionRequest(
                            scope=TransitionScope(
                                organization_id=memory.organization_id,
                                project_id=memory.project_id,
                                team_id=memory.team_id,
                            ),
                            idempotency_key=f'memory-version:{memory.id}:{data.request_id}:v1',
                            actor_type=scope.actor_type,
                            actor_id=scope.actor_id,
                            capability='memories:review',
                            request_id=data.request_id,
                            correlation_id=data.correlation_id,
                            reason=data.reason,
                            origin='memory-version',
                        ),
                        memory_fence=build_memory_fence(memory),
                        title=memory.title,
                        body=data.body,
                    ),
                )
            except MemoryTransitionError as error:
                raise MemoryVersionError(error.code, str(error)) from error

        memory = result.memory
        memory.refresh_from_db()

        return UpdateMemoryBodyResult(
            memory=memory,
            memory_version=result.memory_version,
            retrieval_document=result.retrieval_document,
        )


@dataclass(frozen=True)
class MemoryDiffInput:
    scope: EffectiveScope
    memory_id: uuid.UUID
    project_id: uuid.UUID
    from_version: int
    to_version: int


class MemoryDiffError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ResolveMemoryDiff:
    def execute(self, data: MemoryDiffInput) -> dict[str, object]:
        scope = data.scope
        memory = Memory.objects.filter(
            organization_id=scope.organization_id,
            project_id=data.project_id,
            id=data.memory_id,
        ).first()
        if memory is None:
            raise MemoryDiffError('memory_not_found', 'Memory was not found')

        ensure_memory_team_scope(memory, scope)
        from_slice = self._get_version(memory, data.from_version)
        to_slice = self._get_version(memory, data.to_version)

        return {
            'from': self._version_slice(from_slice),
            'to': self._version_slice(to_slice),
        }

    def _get_version(self, memory: Memory, version_number: int) -> MemoryVersion:
        version = MemoryVersion.objects.filter(memory=memory, version=version_number).first()
        if version is None:
            raise MemoryDiffError('version_not_found', f'Memory version {version_number} was not found')

        return version

    def _version_slice(self, version: MemoryVersion) -> dict[str, object]:
        return {
            'version': version.version,
            'body': redact_text(version.body),
            'created_at': version.created_at,
        }


@dataclass(frozen=True)
class MemoryLinkInput:
    scope: EffectiveScope
    memory_id: uuid.UUID
    project_id: uuid.UUID
    team_id: uuid.UUID | None
    link_type: str
    target: str
    label: str
    request_id: str
    correlation_id: str = ''


@dataclass(frozen=True)
class MemoryLinkResult:
    memory: Memory
    link: MemoryLink
    created: bool

    def to_response(self) -> dict[str, object]:
        return {
            'memory_id': str(self.memory.id),
            'link_id': str(self.link.id),
            'link_type': self.link.link_type,
            'target': redact_text(self.link.target),
            'label': redact_text(self.link.label),
            'created': self.created,
        }


MEMORY_LINK_STATUS = {
    'memory_not_found': drf_status.HTTP_404_NOT_FOUND,
    'link_not_found': drf_status.HTTP_404_NOT_FOUND,
    'semantic_link_protected': drf_status.HTTP_409_CONFLICT,
}

GENERIC_MEMORY_LINK_TYPES = frozenset(
    {
        LinkType.FILE,
        LinkType.SYMBOL,
        LinkType.COMMIT,
        LinkType.ISSUE,
    },
)


class MemoryLinkError(DomainError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(
            message,
            error_code=code,
            status_code=MEMORY_LINK_STATUS.get(code, drf_status.HTTP_400_BAD_REQUEST),
        )
        self.code = code


class RecordMemoryLink:
    def execute(self, data: MemoryLinkInput) -> MemoryLinkResult:
        if data.link_type not in GENERIC_MEMORY_LINK_TYPES:
            raise MemoryLinkError(
                'semantic_link_requires_transition',
                'Semantic links must be created by a memory transition',
            )

        scope = data.scope
        with transaction.atomic():
            memory = lock_memory_for_update(scope, data.project_id, data.memory_id, MemoryLinkError)
            ensure_memory_team_scope(memory, scope)
            link, created = MemoryLink.objects.get_or_create(
                memory=memory,
                link_type=data.link_type,
                target=data.target,
                defaults={
                    'organization': memory.organization,
                    'project': memory.project,
                    'label': data.label,
                },
            )
            if not created and data.label and link.label != data.label:
                link.label = data.label
                link.save(update_fields=['label', 'updated_at'])
            self._audit(memory, link, scope, data, created)

        return MemoryLinkResult(memory=memory, link=link, created=created)

    def _audit(
        self,
        memory: Memory,
        link: MemoryLink,
        scope: EffectiveScope,
        data: MemoryLinkInput,
        created: bool,
    ) -> None:
        AuditEvent.objects.create(
            organization=memory.organization,
            project=memory.project,
            team=memory.team,
            event_type='MemoryLinkRecorded',
            actor_type=scope.actor_type,
            actor_id=scope.actor_id,
            target_type='memory_link',
            target_id=str(link.id),
            capability='memories:review',
            result=AuditResult.ALLOWED,
            request_id=data.request_id,
            correlation_id=data.correlation_id,
            metadata={
                'memory_id': str(memory.id),
                'link_type': link.link_type,
                'created': created,
                'target': redact_text(link.target),
                'scope_filters': {
                    'organization_id': str(scope.organization_id),
                    'project_ids': [str(project_id) for project_id in scope.project_ids],
                    'team_ids': [str(team_id) for team_id in scope.team_ids],
                },
            },
        )

        logger.info(
            'memory_link_recorded',
            memory_id=str(memory.id),
            item_id=str(link.id),
            link_type=link.link_type,
            created=created,
        )


@dataclass(frozen=True)
class RemoveMemoryLinkInput:
    scope: EffectiveScope
    memory_id: uuid.UUID
    project_id: uuid.UUID
    team_id: uuid.UUID | None
    link_id: uuid.UUID
    request_id: str
    correlation_id: str = ''


@dataclass(frozen=True)
class RemoveMemoryLinkResult:
    memory: Memory
    link_id: uuid.UUID
    link_type: str

    def to_response(self) -> dict[str, object]:
        return {
            'memory_id': str(self.memory.id),
            'link_id': str(self.link_id),
            'link_type': self.link_type,
            'deleted': True,
        }


class RemoveMemoryLink:
    def execute(self, data: RemoveMemoryLinkInput) -> RemoveMemoryLinkResult:
        scope = data.scope
        with transaction.atomic():
            memory = lock_memory_for_update(scope, data.project_id, data.memory_id, MemoryLinkError)
            ensure_memory_team_scope(memory, scope)
            link = MemoryLink.objects.filter(
                organization=memory.organization,
                project=memory.project,
                memory=memory,
                id=data.link_id,
            ).first()
            if link is None:
                raise MemoryLinkError('link_not_found', 'Memory link was not found')

            link_type = link.link_type
            target = link.target
            if (
                MemoryTransition.objects.filter(semantic_link_id=link.id).exists()
                or MemoryConflict.objects.filter(semantic_link_id=link.id).exists()
            ):
                raise MemoryLinkError(
                    'semantic_link_protected',
                    'Memory link is protected by a memory transition or conflict',
                )
            if link_type not in GENERIC_MEMORY_LINK_TYPES:
                raise MemoryLinkError(
                    'semantic_link_requires_transition',
                    'Semantic links must be removed by a memory transition',
                )
            try:
                link.delete()
            except ProtectedError as error:
                raise MemoryLinkError(
                    'semantic_link_protected',
                    'Memory link is protected by a memory transition or conflict',
                ) from error

            self._audit(memory, scope, data, link_type, target)

        return RemoveMemoryLinkResult(memory=memory, link_id=data.link_id, link_type=link_type)

    def _audit(
        self,
        memory: Memory,
        scope: EffectiveScope,
        data: RemoveMemoryLinkInput,
        link_type: str,
        target: str,
    ) -> None:
        AuditEvent.objects.create(
            organization=memory.organization,
            project=memory.project,
            team=memory.team,
            event_type='MemoryLinkRemoved',
            actor_type=scope.actor_type,
            actor_id=scope.actor_id,
            target_type='memory_link',
            target_id=str(data.link_id),
            capability='memories:review',
            result=AuditResult.ALLOWED,
            request_id=data.request_id,
            correlation_id=data.correlation_id,
            metadata={
                'memory_id': str(memory.id),
                'link_type': link_type,
                'target': redact_text(target),
                'scope_filters': {
                    'organization_id': str(scope.organization_id),
                    'project_ids': [str(project_id) for project_id in scope.project_ids],
                    'team_ids': [str(team_id) for team_id in scope.team_ids],
                },
            },
        )


@dataclass(frozen=True)
class DigestInput:
    project_id: uuid.UUID
    memory_ids: tuple[uuid.UUID, ...]
    request_id: str
    correlation_id: str = ''


@dataclass(frozen=True)
class DigestResult:
    memory: Memory
    memory_version: MemoryVersion
    retrieval_document: RetrievalDocument
    provider_call_id: uuid.UUID

    def to_response(self) -> dict[str, object]:
        return {
            'memory_id': str(self.memory.id),
            'memory_version_id': str(self.memory_version.id),
            'retrieval_document_id': str(self.retrieval_document.id),
            'provider_call_id': str(self.provider_call_id),
            'title': str(self.memory.title),
        }


def digest_system_prompt() -> str:
    return (
        'You are a memory synthesis engine for software engineering sessions.\n'
        'Given a list of approved engineering memories, produce a daily digest.\n'
        '\n'
        'Rules:\n'
        '- Output the Title on the first line (single line, under 255 characters) summarising the digest theme.\n'
        '- Output the Body on the remaining lines.\n'
        '- In the Body, consolidate and de-duplicate related memories. Group by theme.\n'
        '- Highlight decisions, changes, and risks explicitly.\n'
        '- Be concise. Drop redundant detail.\n'
        '- Do not invent facts not present in the source memories.\n'
        '- Do not name any AI assistant, tool, or product by brand.\n'
        '- The output must be parseable: Title on the first non-empty line, Body on subsequent lines.'
    )


def _digest_source_char_budget() -> int:
    return int(os.environ.get('ENGRAM_DIGEST_SOURCE_CHAR_BUDGET', '2000'))


def _digest_max_sources() -> int:
    return int(os.environ.get('ENGRAM_DIGEST_MAX_SOURCES', '200'))


def _digest_source_line(source: Memory, cap: int) -> str:
    line = f'- {source.title}: {source.body}'
    if len(line) <= cap:
        return line

    marker = f'\n[truncated {len(line) - cap} chars]'
    head = line[: max(cap - len(marker), 0)]

    return head + marker


def digest_prompt(sources: tuple[Memory, ...], cap: int | None = None) -> str:
    budget = cap if cap is not None else _digest_source_char_budget()
    lines = [_digest_source_line(source, budget) for source in sources]

    return '\n'.join(lines)


def digest_content_hash(project_id: uuid.UUID, memory_ids: tuple[uuid.UUID, ...]) -> str:
    material = f'{project_id}:{sorted(str(mid) for mid in memory_ids)}'

    return hashlib.sha256(material.encode()).hexdigest()


def _frozen_digest_source_fences(
    sources: tuple[Memory, ...],
    *,
    team_id: uuid.UUID | None,
) -> tuple[MemoryFence, ...]:
    from engram.memory.transitions import build_memory_fence

    legacy_ids = [
        str(source.id)
        for source in sources
        if source.transition_contract_version != 1 or source.current_transition_id is None
    ]
    if legacy_ids:
        raise MemoryWorkerError(
            f'digest sources are not owned by the v1 transition contract: {", ".join(sorted(legacy_ids))}',
            code='legacy_source_contract',
        )
    foreign_team_ids = [str(source.id) for source in sources if source.team_id != team_id]
    if foreign_team_ids:
        raise MemoryWorkerError(
            f'digest sources are outside the exact team scope: {", ".join(sorted(foreign_team_ids))}',
            code='work_scope_invalid',
        )

    return tuple(build_memory_fence(source) for source in sorted(sources, key=lambda item: str(item.id)))


def render_frozen_daily_digest_provider_result(
    *,
    project: Project,
    sources: tuple[object, ...],
    request_id: str,
    trace_id: str,
) -> ProviderCallResult:
    resolved = ResolveModelPolicy().execute(
        ResolveModelPolicyInput(
            organization_id=project.organization_id,
            project_id=project.id,
            team_id=None,
            task_type='digest',
        ),
    )
    prompt = digest_prompt(sources)
    try:
        provider_result, _used_resolved = call_with_fallback(
            resolved,
            get_provider_gateway(resolved.policy),
            ProviderCallInput(
                organization_id=project.organization_id,
                project_id=project.id,
                team_id=None,
                policy=resolved.policy,
                request_id=request_id,
                trace_id=trace_id,
                prompt=prompt,
                system_prompt=digest_system_prompt(),
            ),
        )
    except (ModelPolicyError, ProviderSecretError) as error:
        raise MemoryWorkerError(
            f'digest provider unavailable: {error}',
            retryable=getattr(error, 'retryable', False),
        ) from error

    return provider_result


class GenerateDigest:
    def execute(self, data: DigestInput) -> DigestResult:
        from engram.memory.transitions import (
            MemoryTransitionError,
            PublishDigestMemory,
            PublishDigestMemoryInput,
            TransitionRequest,
            TransitionScope,
        )

        project = Project.objects.get(id=data.project_id)
        base_sources = Memory.objects.filter(
            id__in=data.memory_ids,
            organization=project.organization,
            project=project,
            status=MemoryStatus.APPROVED,
        )
        total_sources = base_sources.count()
        if total_sources == 0:
            raise MemoryWorkerError('no approved source memories found for digest')

        max_sources = _digest_max_sources()
        selected = list(base_sources.order_by('-updated_at', 'id')[:max_sources])
        sources = tuple(sorted(selected, key=lambda memory: (memory.title, str(memory.id))))
        content_hash = digest_content_hash(project.id, data.memory_ids)
        existing = self._find_existing(project, content_hash)
        if existing is not None:
            return existing
        source_fences = _frozen_digest_source_fences(sources, team_id=None)
        if total_sources > len(sources):
            self._audit_sources_truncated(
                project,
                data,
                total_sources=total_sources,
                sources_used=len(sources),
            )
        resolved = ResolveModelPolicy().execute(
            ResolveModelPolicyInput(
                organization_id=project.organization_id,
                project_id=project.id,
                team_id=None,
                task_type='digest',
            ),
        )
        prompt = digest_prompt(sources)
        try:
            provider_result, _used_resolved = call_with_fallback(
                resolved,
                get_provider_gateway(resolved.policy),
                ProviderCallInput(
                    organization_id=project.organization_id,
                    project_id=project.id,
                    team_id=None,
                    policy=resolved.policy,
                    request_id=f'{data.request_id}:{content_hash}',
                    trace_id=data.request_id,
                    prompt=prompt,
                    system_prompt=digest_system_prompt(),
                ),
            )
        except (ModelPolicyError, ProviderSecretError) as error:
            raise MemoryWorkerError(
                f'digest provider unavailable: {error}',
                retryable=getattr(error, 'retryable', False),
            ) from error
        try:
            transition_result = PublishDigestMemory().execute(
                PublishDigestMemoryInput(
                    request=TransitionRequest(
                        scope=TransitionScope(
                            organization_id=project.organization_id,
                            project_id=project.id,
                            team_id=None,
                        ),
                        idempotency_key=f'daily-digest:{project.id}:{content_hash}:publish:v1',
                        actor_type='api_key',
                        actor_id='digest-generator',
                        capability='memories:review',
                        request_id=data.request_id,
                        correlation_id=data.correlation_id,
                        reason='publish daily structured digest',
                        origin='memory-services-daily-digest',
                    ),
                    source_memory_fences=source_fences,
                    title=f'Digest {provider_result.generated_title}',
                    body=provider_result.generated_body,
                    metadata={
                        'kind': 'digest',
                        'digest_kind': 'daily_structured',
                        'source_memory_ids': [str(source.id) for source in sources],
                        'content_hash': content_hash,
                        'provider_call_id': str(provider_result.call_record_id),
                        'provider': provider_result.provider,
                        'model': provider_result.model,
                    },
                    visibility_scope=VisibilityScope.PROJECT,
                ),
            )
        except MemoryTransitionError as error:
            raise MemoryWorkerError(
                f'digest publication failed: {error}',
                retryable=error.retryable,
                code=error.code,
            ) from error

        return DigestResult(
            memory=transition_result.memory,
            memory_version=transition_result.memory_version,
            retrieval_document=transition_result.retrieval_document,
            provider_call_id=provider_result.call_record_id,
        )

    def _audit_sources_truncated(
        self,
        project: Project,
        data: DigestInput,
        *,
        total_sources: int,
        sources_used: int,
    ) -> None:
        AuditEvent.objects.create(
            organization=project.organization,
            project=project,
            event_type='DigestSourcesTruncated',
            actor_type='system',
            target_type='project',
            target_id=str(project.id),
            capability='memories:review',
            result=AuditResult.RECORDED,
            request_id=data.request_id,
            correlation_id=data.correlation_id,
            metadata={
                'total_sources': total_sources,
                'sources_used': sources_used,
                'max_sources': _digest_max_sources(),
            },
        )

    def _find_existing(self, project: Project, content_hash: str) -> DigestResult | None:
        existing_memory = (
            Memory.objects.filter(
                organization=project.organization,
                project=project,
                kind='digest',
                metadata__content_hash=content_hash,
                versions__retrieval_document__isnull=False,
            )
            .order_by('-created_at')
            .first()
        )
        if existing_memory is None:
            return None

        existing_version = MemoryVersion.objects.filter(memory=existing_memory).order_by('version').first()
        existing_doc = RetrievalDocument.objects.filter(memory=existing_memory).first()

        metadata = existing_memory.metadata if isinstance(existing_memory.metadata, dict) else {}
        call_id_str = metadata.get('provider_call_id')
        provider_call_id = uuid.UUID(call_id_str) if call_id_str else existing_version.id

        return DigestResult(
            memory=existing_memory,
            memory_version=existing_version,
            retrieval_document=existing_doc,
            provider_call_id=provider_call_id,
        )


DAILY_DIGEST_WINDOW_DAYS = 7


def _audit_digest_failed(
    project: Project,
    *,
    digest_kind: str,
    error: Exception,
    request_id: str,
    correlation_id: str,
) -> None:
    AuditEvent.objects.create(
        organization=project.organization,
        project=project,
        event_type='DigestFailed',
        actor_type='system',
        target_type='project',
        target_id=str(project.id),
        capability='memories:review',
        result=AuditResult.ERROR,
        request_id=request_id,
        correlation_id=correlation_id,
        metadata={
            'digest_kind': digest_kind,
            'reason': str(error)[:1024],
        },
    )


def run_daily_digest_with_tracking(
    organization_id: uuid.UUID,
    project_id: uuid.UUID,
    memory_ids: tuple[uuid.UUID, ...],
    *,
    window_days: int = DAILY_DIGEST_WINDOW_DAYS,
    request_id: str = '',
    correlation_id: str = '',
    existing_run_id: uuid.UUID | None = None,
) -> DigestResult:
    project = Project.objects.get(id=project_id, organization_id=organization_id)

    run = None
    if existing_run_id is not None:
        run = WorkflowRun.objects.filter(
            id=existing_run_id,
            organization=project.organization,
            project=project,
            run_type=WorkflowRunType.DAILY_DIGEST,
            status=WorkflowRunStatus.QUEUED,
        ).first()

    if run is None:
        try:
            with transaction.atomic():
                run = WorkflowRun.objects.create(
                    organization=project.organization,
                    project=project,
                    run_type=WorkflowRunType.DAILY_DIGEST,
                    status=WorkflowRunStatus.QUEUED,
                    input_snapshot={
                        'memory_ids': [str(value) for value in memory_ids],
                        'window_days': window_days,
                    },
                    request_id=request_id,
                    correlation_id=correlation_id,
                )
        except IntegrityError as error:
            raise MemoryWorkerError('daily digest run is already active for this project') from error

    run.status = WorkflowRunStatus.RUNNING

    run.started_at = timezone.now()

    run.save(update_fields=['status', 'started_at', 'updated_at'])

    try:
        result = GenerateDigest().execute(
            DigestInput(
                project_id=project_id,
                memory_ids=memory_ids,
                request_id=request_id,
                correlation_id=correlation_id,
            ),
        )
    except Exception as error:
        run.status = WorkflowRunStatus.FAILED

        run.failure_reason = str(error)[:1024]

        run.finished_at = timezone.now()

        run.save(update_fields=['status', 'failure_reason', 'finished_at', 'updated_at'])

        if not getattr(error, 'retryable', False):
            _audit_digest_failed(
                project,
                digest_kind='daily_structured',
                error=error,
                request_id=request_id,
                correlation_id=correlation_id,
            )

        raise

    run.status = WorkflowRunStatus.SUCCEEDED

    run.finished_at = timezone.now()

    run.result_memory = result.memory

    run.provider_call_ids = [str(result.provider_call_id)]

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


WEEKLY_DIGEST_WINDOW_DAYS = 7


@dataclass(frozen=True)
class WeeklyDigestInput:
    organization_id: uuid.UUID
    project_id: uuid.UUID
    window_days: int = WEEKLY_DIGEST_WINDOW_DAYS
    team_id: uuid.UUID | None = None
    weeks_back: int = 0
    request_id: str = ''
    correlation_id: str = ''


@dataclass(frozen=True)
class WeeklyDigestResult:
    digest_memory: Memory
    counts: dict[str, int]
    memory_changes: dict[str, list[dict]]
    ready: bool


def weekly_digest_content_hash(
    project_id: uuid.UUID,
    window_start: datetime.datetime,
    window_end: datetime.datetime,
    team_id: uuid.UUID | None = None,
) -> str:
    material = f'{project_id}:{window_start.date().isoformat()}:{window_end.date().isoformat()}:{team_id or ""}'

    return hashlib.sha256(material.encode()).hexdigest()


_WEEKLY_DIGEST_BUCKET_ORDER = (
    ('added', 'Added'),
    ('superseded', 'Superseded'),
    ('retired', 'Retired'),
    ('merged', 'Merged'),
    ('refuted', 'Refuted'),
)


def render_weekly_digest_body(
    memory_changes: dict[str, list[dict]],
    window_end: datetime.datetime,
    window_days: int,
) -> str:
    lines = [f'Structured weekly digest covering {window_days} days ending {window_end.date()}.']
    for key, header in _WEEKLY_DIGEST_BUCKET_ORDER:
        items = memory_changes.get(key, [])
        lines.append('')
        lines.append(f'## {header} ({len(items)})')
        lines.extend(f'- {item.get("title", "")}' for item in items)

    return '\n'.join(lines)


class BuildWeeklyStructuredDigest:
    def execute(self, data: WeeklyDigestInput) -> WeeklyDigestResult:
        from engram.memory.transitions import (
            MemoryTransitionError,
            PublishDigestMemory,
            PublishDigestMemoryInput,
            TransitionRequest,
            TransitionScope,
        )

        project, window_start, window_end, content_hash = self._resolve_window(data)

        existing = self._find_existing(project, content_hash)

        if existing is not None:
            return existing

        memory_changes, counts = self._build_buckets(project, window_start, window_end, data.team_id)
        source_ids = {uuid.UUID(str(item['id'])) for items in memory_changes.values() for item in items}
        if not source_ids:
            raise MemoryWorkerError(
                'weekly digest publication requires transition-owned source memories',
                code='provenance',
            )
        source_memories = tuple(
            Memory.objects.filter(
                organization=project.organization,
                project=project,
                id__in=source_ids,
            )
        )
        if len(source_memories) != len(source_ids):
            raise MemoryWorkerError('weekly digest source memory is outside project scope', code='work_scope_invalid')
        source_fences = _frozen_digest_source_fences(source_memories, team_id=data.team_id)
        visibility_scope = VisibilityScope.TEAM if data.team_id else VisibilityScope.PROJECT
        title = f'Weekly Structured Digest {window_start.date()} to {window_end.date()}'
        body = render_weekly_digest_body(memory_changes, window_end, data.window_days)
        metadata = {
            'kind': 'digest',
            'digest_kind': 'weekly_structured',
            'window_start': window_start.isoformat(),
            'window_end': window_end.isoformat(),
            'window_days': data.window_days,
            'memory_changes': memory_changes,
            'counts': counts,
            'content_hash': content_hash,
            'ready': False,
            'reviewed_at': None,
        }
        try:
            transition_result = PublishDigestMemory().execute(
                PublishDigestMemoryInput(
                    request=TransitionRequest(
                        scope=TransitionScope(
                            organization_id=project.organization_id,
                            project_id=project.id,
                            team_id=data.team_id,
                        ),
                        idempotency_key=f'weekly-digest:{project.id}:{content_hash}:publish:v1',
                        actor_type='system',
                        actor_id='weekly-digest-builder',
                        capability='memories:review',
                        request_id=data.request_id,
                        correlation_id=data.correlation_id,
                        reason='publish weekly structured digest',
                        origin='memory-services-weekly-digest',
                    ),
                    source_memory_fences=source_fences,
                    title=title,
                    body=body,
                    metadata=metadata,
                    visibility_scope=visibility_scope,
                ),
            )
        except MemoryTransitionError as error:
            raise MemoryWorkerError(
                f'weekly digest publication failed: {error}',
                retryable=error.retryable,
                code=error.code,
            ) from error

        return WeeklyDigestResult(
            digest_memory=transition_result.memory,
            counts=counts,
            memory_changes=memory_changes,
            ready=False,
        )

    def find_existing(
        self,
        data: WeeklyDigestInput,
    ) -> tuple[WeeklyDigestResult | None, datetime.datetime, datetime.datetime]:
        project, window_start, window_end, content_hash = self._resolve_window(data)

        return self._find_existing(project, content_hash), window_start, window_end

    def _resolve_window(
        self,
        data: WeeklyDigestInput,
    ) -> tuple[Project, datetime.datetime, datetime.datetime, str]:
        project = Project.objects.get(id=data.project_id, organization_id=data.organization_id)

        today = timezone.now().date()

        current_monday = today - datetime.timedelta(days=today.isoweekday() - 1)

        anchor_monday = current_monday - datetime.timedelta(weeks=max(0, data.weeks_back))

        tzinfo = timezone.get_current_timezone()

        window_end = datetime.datetime.combine(anchor_monday, datetime.time.min, tzinfo=tzinfo)

        window_start = datetime.datetime.combine(
            anchor_monday - datetime.timedelta(days=7),
            datetime.time.min,
            tzinfo=tzinfo,
        )

        content_hash = weekly_digest_content_hash(project.id, window_start, window_end, data.team_id)

        return project, window_start, window_end, content_hash

    def _find_existing(self, project: Project, content_hash: str) -> WeeklyDigestResult | None:
        existing = (
            Memory.objects.filter(
                organization=project.organization,
                project=project,
                kind='digest',
                metadata__digest_kind='weekly_structured',
                metadata__content_hash=content_hash,
                versions__retrieval_document__isnull=False,
            )
            .order_by('-created_at')
            .first()
        )

        if existing is None:
            return None

        metadata = existing.metadata if isinstance(existing.metadata, dict) else {}

        return WeeklyDigestResult(
            digest_memory=existing,
            counts=metadata.get('counts', {}),
            memory_changes=metadata.get('memory_changes', {}),
            ready=metadata.get('ready', False),
        )

    def _scope_to_team(self, queryset: QuerySet[Memory], team_id: uuid.UUID | None) -> QuerySet[Memory]:
        if team_id is None:
            return queryset

        return queryset.filter(team_id=team_id)

    def _build_buckets(
        self,
        project: Project,
        window_start: datetime.datetime,
        window_end: datetime.datetime,
        team_id: uuid.UUID | None = None,
    ) -> tuple[dict, dict]:
        org = project.organization

        refuted_qs = Memory.objects.filter(
            organization=org,
            project=project,
            updated_at__gte=window_start,
            updated_at__lt=window_end,
        ).filter(Q(status=MemoryStatus.REFUTED) | Q(refuted=True))

        refuted_qs = self._scope_to_team(refuted_qs, team_id)

        refuted_items = list(refuted_qs)

        refuted_ids = {m.id for m in refuted_items}

        retired_qs = Memory.objects.filter(
            organization=org,
            project=project,
            status=MemoryStatus.ARCHIVED,
            updated_at__gte=window_start,
            updated_at__lt=window_end,
        ).exclude(id__in=refuted_ids)

        retired_qs = self._scope_to_team(retired_qs, team_id)

        retired_items = list(retired_qs)

        retired_ids = {m.id for m in retired_items}

        superseded_links = list(
            MemoryLink.objects.filter(
                organization=org,
                project=project,
                link_type=LinkType.SUPERSEDED_BY,
                created_at__gte=window_start,
                created_at__lt=window_end,
            )
        )

        superseded_candidate_ids = {link.memory_id for link in superseded_links}

        superseded_ids = superseded_candidate_ids - refuted_ids - retired_ids

        superseded_link_time: dict[uuid.UUID, datetime.datetime] = {}

        for link in superseded_links:
            if link.memory_id in superseded_ids and link.memory_id not in superseded_link_time:
                superseded_link_time[link.memory_id] = link.created_at

        merged_links = list(
            MemoryLink.objects.filter(
                organization=org,
                project=project,
                link_type=LinkType.NARROWED_BY,
                created_at__gte=window_start,
                created_at__lt=window_end,
            )
        )

        merged_candidate_ids = {link.memory_id for link in merged_links}

        merged_ids = merged_candidate_ids - refuted_ids - retired_ids - superseded_ids

        merged_link_time: dict[uuid.UUID, datetime.datetime] = {}

        for link in merged_links:
            if link.memory_id in merged_ids and link.memory_id not in merged_link_time:
                merged_link_time[link.memory_id] = link.created_at

        excluded_from_added = refuted_ids | retired_ids | superseded_ids | merged_ids

        added_qs = Memory.objects.filter(
            organization=org,
            project=project,
            created_at__gte=window_start,
            created_at__lt=window_end,
        ).exclude(id__in=excluded_from_added)

        added_qs = self._scope_to_team(added_qs, team_id)

        added_items = list(added_qs)

        link_memory_ids = superseded_ids | merged_ids

        link_memories: dict[uuid.UUID, Memory] = {}

        if link_memory_ids:
            link_memories_qs = Memory.objects.filter(
                organization=org,
                project=project,
                id__in=link_memory_ids,
            )

            link_memories_qs = self._scope_to_team(link_memories_qs, team_id)

            link_memories = {m.id: m for m in link_memories_qs}

        def _item(m: Memory, at: datetime.datetime) -> dict:
            return {
                'id': str(m.id),
                'title': redact_text(m.title),
                'at': at.isoformat(),
            }

        memory_changes: dict[str, list[dict]] = {
            'refuted': [_item(m, m.updated_at) for m in refuted_items],
            'retired': [_item(m, m.updated_at) for m in retired_items],
            'superseded': [
                _item(link_memories[mid], superseded_link_time[mid])
                for mid in superseded_ids
                if mid in link_memories and mid in superseded_link_time
            ],
            'merged': [
                _item(link_memories[mid], merged_link_time[mid])
                for mid in merged_ids
                if mid in link_memories and mid in merged_link_time
            ],
            'added': [_item(m, m.created_at) for m in added_items],
        }

        counts: dict[str, int] = {bucket: len(items) for bucket, items in memory_changes.items()}

        return memory_changes, counts


def run_weekly_digest_with_tracking(
    organization_id: uuid.UUID,
    project_id: uuid.UUID,
    *,
    window_days: int = WEEKLY_DIGEST_WINDOW_DAYS,
    request_id: str = '',
    correlation_id: str = '',
    existing_run_id: uuid.UUID | None = None,
) -> WeeklyDigestResult:
    project = Project.objects.get(id=project_id, organization_id=organization_id)

    run = None
    if existing_run_id is not None:
        run = WorkflowRun.objects.filter(
            id=existing_run_id,
            organization=project.organization,
            project=project,
            run_type=WorkflowRunType.WEEKLY_DIGEST,
            status=WorkflowRunStatus.QUEUED,
        ).first()

    if run is None:
        run = WorkflowRun.objects.create(
            organization=project.organization,
            project=project,
            run_type=WorkflowRunType.WEEKLY_DIGEST,
            status=WorkflowRunStatus.QUEUED,
            input_snapshot={'window_days': window_days},
            request_id=request_id,
            correlation_id=correlation_id,
        )

    run.status = WorkflowRunStatus.RUNNING

    run.started_at = timezone.now()

    run.save(update_fields=['status', 'started_at', 'updated_at'])

    try:
        result = BuildWeeklyStructuredDigest().execute(
            WeeklyDigestInput(
                organization_id=organization_id,
                project_id=project_id,
                window_days=window_days,
                request_id=request_id,
                correlation_id=correlation_id,
            ),
        )
    except Exception as error:
        run.status = WorkflowRunStatus.FAILED

        run.failure_reason = str(error)[:1024]

        run.finished_at = timezone.now()

        run.save(update_fields=['status', 'failure_reason', 'finished_at', 'updated_at'])

        if not getattr(error, 'retryable', False):
            _audit_digest_failed(
                project,
                digest_kind='weekly_structured',
                error=error,
                request_id=request_id,
                correlation_id=correlation_id,
            )

        raise

    run.status = WorkflowRunStatus.SUCCEEDED

    run.finished_at = timezone.now()

    run.result_memory = result.digest_memory

    run.save(
        update_fields=[
            'status',
            'finished_at',
            'result_memory',
            'updated_at',
        ],
    )

    return result
