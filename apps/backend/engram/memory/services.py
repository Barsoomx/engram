from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from decimal import Decimal

from django.db import transaction

from engram.access.services import AccessDeniedError, EffectiveScope, ResolveApiKeyScope
from engram.context.services import IndexMemoryVersion, IndexMemoryVersionInput
from engram.core.models import (
    AuditEvent,
    AuditResult,
    CandidateStatus,
    Memory,
    MemoryCandidate,
    MemoryStatus,
    MemoryVersion,
    Observation,
    RetrievalDocument,
    VisibilityScope,
)
from engram.core.redaction import redact_value as core_redact_value
from engram.model_policy.services import (
    FakeProviderGateway,
    ModelPolicyError,
    ProviderCallInput,
    ProviderSecretError,
    ResolveModelPolicy,
    ResolveModelPolicyInput,
)


@dataclass(frozen=True)
class MemoryCandidateWorkerInput:
    observation_id: uuid.UUID
    worker_id: str = 'memory-worker'


@dataclass(frozen=True)
class MemoryCandidateWorkerResult:
    candidate: MemoryCandidate
    memory: Memory
    memory_version: MemoryVersion
    retrieval_document: RetrievalDocument
    duplicate: bool


class MemoryWorkerError(Exception):
    pass


@dataclass(frozen=True)
class GeneratedMemoryCandidate:
    title: str
    body: str
    evidence: list[dict[str, object]]


@dataclass(frozen=True)
class PromoteMemoryCandidateInput:
    candidate_id: uuid.UUID


@dataclass(frozen=True)
class PromoteMemoryCandidateResult:
    candidate: MemoryCandidate
    memory: Memory
    memory_version: MemoryVersion
    retrieval_document: RetrievalDocument
    duplicate: bool


@dataclass(frozen=True)
class MemoryFeedbackInput:
    raw_key: str
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
        scope = ResolveApiKeyScope().execute(
            raw_key=data.raw_key,
            required_capability='memories:review',
            requested_project_id=data.project_id,
            requested_team_id=data.team_id,
            request_id=data.request_id,
            correlation_id=data.correlation_id,
            target_type='memory',
            target_id=str(data.memory_id),
        )
        with transaction.atomic():
            memory = self._lock_memory(data, scope)
            self._ensure_team_scope(memory, scope)
            already_applied = self._already_applied(memory, data.action)
            self._apply(memory, data.action)
            updated = self._sync_retrieval_documents(memory, data.action)
            self._audit(memory, scope, data, updated, already_applied)

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

    def _apply(self, memory: Memory, action: str) -> None:
        if getattr(memory, action):
            return

        setattr(memory, action, True)
        memory.save(update_fields=[action, 'updated_at'])

    def _sync_retrieval_documents(self, memory: Memory, action: str) -> int:
        return RetrievalDocument.objects.filter(
            organization=memory.organization,
            project=memory.project,
            memory=memory,
        ).update(**{action: True})

    def _audit(
        self,
        memory: Memory,
        scope: EffectiveScope,
        data: MemoryFeedbackInput,
        updated: int,
        already_applied: bool,
    ) -> None:
        AuditEvent.objects.create(
            organization=memory.organization,
            project=memory.project,
            team=memory.team,
            event_type='MemoryFeedbackRecorded',
            actor_type=scope.actor_type,
            actor_id=scope.actor_id,
            target_type='memory',
            target_id=str(memory.id),
            capability='memories:review',
            result=AuditResult.ALLOWED,
            request_id=data.request_id,
            correlation_id=data.correlation_id,
            metadata={
                'action': data.action,
                'reason': redact_text(data.reason),
                'retrieval_documents_updated': updated,
                'already_applied': already_applied,
                'scope_filters': {
                    'organization_id': str(scope.organization_id),
                    'project_ids': [str(project_id) for project_id in scope.project_ids],
                    'team_ids': [str(team_id) for team_id in scope.team_ids],
                },
            },
        )


class ProcessObservationRecorded:
    def execute(self, data: MemoryCandidateWorkerInput) -> MemoryCandidateWorkerResult:
        with transaction.atomic():
            observation = self._lock_observation(data.observation_id)
            generated = self._generate_candidate(observation)
            candidate, candidate_created = self._get_or_create_candidate(observation, generated)
            promotion = PromoteMemoryCandidate().execute(
                PromoteMemoryCandidateInput(candidate_id=candidate.id),
            )

            return MemoryCandidateWorkerResult(
                candidate=promotion.candidate,
                memory=promotion.memory,
                memory_version=promotion.memory_version,
                retrieval_document=promotion.retrieval_document,
                duplicate=not candidate_created or promotion.duplicate,
            )

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
                candidate.save(update_fields=['title', 'body', 'evidence', 'updated_at'])

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
            confidence=Decimal('0.500'),
        )

        return candidate, True

    def _generate_candidate(self, observation: Observation) -> GeneratedMemoryCandidate:
        try:
            resolved = ResolveModelPolicy().execute(
                ResolveModelPolicyInput(
                    organization_id=observation.organization_id,
                    project_id=observation.project_id,
                    team_id=observation.team_id,
                    task_type='generation',
                ),
            )
            provider_result = FakeProviderGateway().call(
                ProviderCallInput(
                    organization_id=observation.organization_id,
                    project_id=observation.project_id,
                    team_id=observation.team_id,
                    policy=resolved.policy,
                    request_id=f'memory-worker:{observation.id}:generation',
                    trace_id=f'memory-worker:{observation.id}',
                    prompt=provider_prompt(observation),
                ),
            )
        except (ModelPolicyError, ProviderSecretError) as error:
            raise MemoryWorkerError(redact_error(str(error))) from error

        provenance = {
            'provider_call_id': str(provider_result.call_record_id),
            'provider': provider_result.provider,
            'model': provider_result.model,
            'policy_id': str(resolved.policy.id),
            'policy_version': resolved.policy.version,
            'task_type': resolved.policy.task_type,
            'redaction_state': provider_result.redaction_state,
        }

        return GeneratedMemoryCandidate(
            title=provider_result.generated_title,
            body=provider_result.generated_body,
            evidence=candidate_evidence(observation, provider_result.generated_title, provenance),
        )

    def _has_provider_provenance(self, candidate: MemoryCandidate) -> bool:
        if not candidate.evidence:
            return False
        evidence = candidate.evidence[0]

        return isinstance(evidence, dict) and bool(evidence.get('provider_call_id'))


def memory_candidate_content_hash(observation: Observation) -> str:
    source = f'{observation.id}:{observation.content_hash}'

    return hashlib.sha256(source.encode()).hexdigest()


def candidate_title(observation: Observation) -> str:
    return redact_text(observation.title)[:255]


def candidate_body(observation: Observation) -> str:
    body = observation.body.strip()
    if body:
        return redact_text(body)

    return redact_text(observation.title)


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


def provider_prompt(observation: Observation) -> str:
    return '\n'.join(
        [
            f'Title: {redact_text(observation.title)}',
            f'Body: {redact_text(observation.body)}',
            f'Files read: {redact_value(observation.files_read)}',
            f'Files modified: {redact_value(observation.files_modified)}',
            f'Source metadata: {redact_value(observation.source_metadata)}',
        ],
    )


def redact_value(value: object) -> object:
    return core_redact_value(value).value


def redact_text(value: str) -> str:
    return str(redact_value(value))


def redact_error(message: str) -> str:
    return redact_text(message)


class PromoteMemoryCandidate:
    def execute(self, data: PromoteMemoryCandidateInput) -> PromoteMemoryCandidateResult:
        with transaction.atomic():
            candidate = self._lock_candidate(data.candidate_id)
            if candidate.status == CandidateStatus.PROMOTED and candidate.promoted_memory_id:
                return self._existing_result(candidate)
            if candidate.status != CandidateStatus.PROPOSED:
                raise MemoryWorkerError('Only proposed memory candidates can be promoted')

            memory = Memory.objects.create(
                organization=candidate.organization,
                project=candidate.project,
                team=candidate.team,
                title=candidate.title,
                body=candidate.body,
                status=MemoryStatus.APPROVED,
                visibility_scope=candidate.visibility_scope,
                confidence=candidate.confidence,
                metadata=self._memory_metadata(candidate),
            )
            version = MemoryVersion.objects.create(
                organization=candidate.organization,
                project=candidate.project,
                memory=memory,
                source_observation=candidate.source_observation,
                version=1,
                body=candidate.body,
                content_hash=candidate.content_hash,
            )
            candidate.status = CandidateStatus.PROMOTED
            candidate.promoted_memory = memory
            candidate.save(update_fields=['status', 'promoted_memory', 'updated_at'])
            retrieval_document = self._index_memory_version(candidate, version)

            return PromoteMemoryCandidateResult(
                candidate=candidate,
                memory=memory,
                memory_version=version,
                retrieval_document=retrieval_document,
                duplicate=False,
            )

    def _lock_candidate(self, candidate_id: uuid.UUID) -> MemoryCandidate:
        try:
            return MemoryCandidate.objects.select_for_update().get(id=candidate_id)
        except MemoryCandidate.DoesNotExist as error:
            raise MemoryWorkerError('memory candidate not found') from error

    def _existing_result(self, candidate: MemoryCandidate) -> PromoteMemoryCandidateResult:
        memory = candidate.promoted_memory
        version = MemoryVersion.objects.get(memory=memory, version=memory.current_version)
        retrieval_document = self._index_memory_version(candidate, version)

        return PromoteMemoryCandidateResult(
            candidate=candidate,
            memory=memory,
            memory_version=version,
            retrieval_document=retrieval_document,
            duplicate=True,
        )

    def _index_memory_version(self, candidate: MemoryCandidate, version: MemoryVersion) -> RetrievalDocument:
        index_result = IndexMemoryVersion().execute(IndexMemoryVersionInput(memory_version_id=version.id))
        document = index_result.retrieval_document
        file_paths = self._candidate_file_paths(candidate)
        if document.file_paths == file_paths:
            return document

        document.file_paths = file_paths
        document.save(update_fields=['file_paths', 'updated_at'])

        return document

    def _memory_metadata(self, candidate: MemoryCandidate) -> dict[str, object]:
        metadata = {
            'source': 'memory_candidate',
            'memory_candidate_id': str(candidate.id),
            'evidence': candidate.evidence,
            'file_paths': self._candidate_file_paths(candidate),
        }
        metadata.update(self._provider_provenance(candidate))

        return metadata

    def _provider_provenance(self, candidate: MemoryCandidate) -> dict[str, object]:
        if not candidate.evidence:
            return {}
        evidence = candidate.evidence[0]
        if not isinstance(evidence, dict) or 'provider_call_id' not in evidence:
            return {}

        return {
            key: evidence[key]
            for key in (
                'provider_call_id',
                'provider',
                'model',
                'policy_id',
                'policy_version',
                'task_type',
                'redaction_state',
            )
            if key in evidence
        }

    def _candidate_file_paths(self, candidate: MemoryCandidate) -> list[str]:
        observation = candidate.source_observation
        if observation is None:
            return []

        return [
            redact_text(file_path)
            for file_path in [
                *observation.files_read,
                *observation.files_modified,
            ]
        ]


@dataclass(frozen=True)
class UpdateMemoryBodyInput:
    raw_key: str
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


class MemoryVersionError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def memory_body_content_hash(memory: Memory, next_version: int, body: str) -> str:
    source = f'{memory.id}:{next_version}:{body}'

    return hashlib.sha256(source.encode()).hexdigest()


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
        scope = ResolveApiKeyScope().execute(
            raw_key=data.raw_key,
            required_capability='memories:review',
            requested_project_id=data.project_id,
            requested_team_id=data.team_id,
            request_id=data.request_id,
            correlation_id=data.correlation_id,
            target_type='memory',
            target_id=str(data.memory_id),
        )
        with transaction.atomic():
            memory = lock_memory_for_update(scope, data.project_id, data.memory_id, MemoryVersionError)
            ensure_memory_team_scope(memory, scope)
            latest_version = memory.versions.order_by('-version').first()
            if latest_version is not None and latest_version.body == data.body:
                retrieval_document = self._index_version(latest_version)

                return UpdateMemoryBodyResult(
                    memory=memory,
                    memory_version=latest_version,
                    retrieval_document=retrieval_document,
                )
            version = self._create_version(memory, data)
            memory.body = data.body
            memory.current_version = version.version
            memory.save(update_fields=['body', 'current_version', 'updated_at'])
            retrieval_document = self._index_version(version)
            self._audit(memory, version, scope, data)

        memory.refresh_from_db()

        return UpdateMemoryBodyResult(
            memory=memory,
            memory_version=version,
            retrieval_document=retrieval_document,
        )

    def _create_version(self, memory: Memory, data: UpdateMemoryBodyInput) -> MemoryVersion:
        next_version = memory.current_version + 1

        return MemoryVersion.objects.create(
            organization=memory.organization,
            project=memory.project,
            memory=memory,
            version=next_version,
            body=data.body,
            content_hash=memory_body_content_hash(memory, next_version, data.body),
        )

    def _index_version(self, version: MemoryVersion) -> RetrievalDocument:
        result = IndexMemoryVersion().execute(IndexMemoryVersionInput(memory_version_id=version.id))

        return result.retrieval_document

    def _audit(
        self,
        memory: Memory,
        version: MemoryVersion,
        scope: EffectiveScope,
        data: UpdateMemoryBodyInput,
    ) -> None:
        AuditEvent.objects.create(
            organization=memory.organization,
            project=memory.project,
            team=memory.team,
            event_type='MemoryVersionCreated',
            actor_type=scope.actor_type,
            actor_id=scope.actor_id,
            target_type='memory',
            target_id=str(memory.id),
            capability='memories:review',
            result=AuditResult.ALLOWED,
            request_id=data.request_id,
            correlation_id=data.correlation_id,
            metadata={
                'version': version.version,
                'reason': redact_text(data.reason),
                'scope_filters': {
                    'organization_id': str(scope.organization_id),
                    'project_ids': [str(project_id) for project_id in scope.project_ids],
                    'team_ids': [str(team_id) for team_id in scope.team_ids],
                },
            },
        )
