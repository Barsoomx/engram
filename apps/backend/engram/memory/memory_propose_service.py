from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

from django.db import IntegrityError, transaction
from django.utils import timezone

from engram.access.services import EffectiveScope
from engram.core.models import (
    AuditEvent,
    AuditResult,
    CandidateStatus,
    MemoryCandidate,
    MemoryCandidateSource,
    MemoryCandidateSourceKind,
    Project,
    ProjectTeam,
    VisibilityScope,
    WorkflowRunOrigin,
    clamp_memory_kind,
)
from engram.memory.candidate_decision_work import ensure_candidate_decision_work_locked
from engram.memory.import_provenance import agent_proposal_candidate_content_hash
from engram.memory.serializers import MEMORY_PROPOSE_BODY_MAX_LENGTH
from engram.memory.services import redact_text
from engram.memory.work_dispatch import queue_work_attempt
from engram.memory.workflow_work import canonical_json_bytes

_TITLE_MAX_LENGTH = 255
_AUDIT_FIELD_MAX_LENGTH = 255


class ProposeMemoryError(Exception):
    def __init__(self, code: str, detail: str = '') -> None:
        self.code = code
        self.detail = detail
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ProposeMemoryInput:
    scope: EffectiveScope
    project: Project
    team_id: uuid.UUID | None
    title: str
    body: str
    kind: str
    request_id: str
    correlation_id: str = ''


@dataclass(frozen=True, slots=True)
class ProposeMemoryResult:
    candidate_id: uuid.UUID
    status: str
    decision_work_queued: bool


class ProposeMemory:
    def execute(self, data: ProposeMemoryInput) -> ProposeMemoryResult:
        scope = data.scope
        effective_team_id = scope.team_ids[0] if scope.team_bound else data.team_id

        title = redact_text(data.title).strip()
        body = redact_text(data.body).strip()
        if not title or not body:
            raise ProposeMemoryError('empty_content', 'Proposed memory title and body must be non-empty.')

        if len(title) > _TITLE_MAX_LENGTH or len(body) > MEMORY_PROPOSE_BODY_MAX_LENGTH:
            raise ProposeMemoryError('content_too_long', 'Proposed memory content is too long after redaction.')

        clamped_kind = clamp_memory_kind(data.kind)
        if effective_team_id is not None and not ProjectTeam.objects.filter(
            project_id=data.project.id,
            team_id=effective_team_id,
        ).exists():
            raise ProposeMemoryError('team_not_in_project', 'Team is not linked to this project.')

        content_hash = agent_proposal_candidate_content_hash(title, body, clamped_kind, effective_team_id)
        anchors = self._build_anchors(scope, data)
        anchors_hash = hashlib.sha256(canonical_json_bytes(anchors)).hexdigest()

        existing = MemoryCandidate.objects.filter(
            organization_id=scope.organization_id,
            project_id=data.project.id,
            content_hash=content_hash,
        ).first()
        if existing is not None and existing.status != CandidateStatus.PROPOSED:
            self._write_audit(existing, scope, data, reused=True)

            return ProposeMemoryResult(existing.id, existing.status, False)

        if existing is not None:
            return self._reuse_proposed(existing.id, scope, data)

        return self._create_new(
            scope,
            data,
            effective_team_id=effective_team_id,
            title=title,
            body=body,
            clamped_kind=clamped_kind,
            content_hash=content_hash,
            anchors=anchors,
            anchors_hash=anchors_hash,
        )

    def _build_anchors(self, scope: EffectiveScope, data: ProposeMemoryInput) -> dict[str, object]:
        return {
            'schema': 'agent_proposal_source.v1',
            'actor_type': scope.actor_type,
            'actor_id': scope.actor_id,
            'api_key_id': str(scope.api_key_id) if scope.actor_type == 'api_key' else None,
            'request_id': data.request_id,
            'correlation_id': data.correlation_id or '',
        }

    def _reuse_proposed(
        self,
        candidate_id: uuid.UUID,
        scope: EffectiveScope,
        data: ProposeMemoryInput,
    ) -> ProposeMemoryResult:
        with transaction.atomic():
            candidate = MemoryCandidate.objects.select_for_update().get(id=candidate_id)

            return self._settle_existing(candidate, scope, data)

    def _create_new(
        self,
        scope: EffectiveScope,
        data: ProposeMemoryInput,
        *,
        effective_team_id: uuid.UUID | None,
        title: str,
        body: str,
        clamped_kind: str,
        content_hash: str,
        anchors: dict[str, object],
        anchors_hash: str,
    ) -> ProposeMemoryResult:
        visibility = VisibilityScope.TEAM if effective_team_id is not None else VisibilityScope.PROJECT
        with transaction.atomic():
            try:
                with transaction.atomic():
                    candidate = MemoryCandidate.objects.create(
                        organization_id=scope.organization_id,
                        project_id=data.project.id,
                        team_id=effective_team_id,
                        source_observation=None,
                        title=title,
                        body=body,
                        status=CandidateStatus.PROPOSED,
                        visibility_scope=visibility,
                        evidence=[],
                        content_hash=content_hash,
                        confidence=None,
                        kind=clamped_kind,
                    )
                    MemoryCandidateSource.objects.create(
                        organization_id=scope.organization_id,
                        project_id=data.project.id,
                        team_id=effective_team_id,
                        candidate=candidate,
                        source_kind=MemoryCandidateSourceKind.AGENT_PROPOSAL,
                        anchors=anchors,
                        anchors_hash=anchors_hash,
                    )
                    candidate.decision_work_contract_version = 1
                    candidate.save(update_fields=['decision_work_contract_version', 'updated_at'])
            except IntegrityError:
                winner = MemoryCandidate.objects.select_for_update().get(
                    organization_id=scope.organization_id,
                    project_id=data.project.id,
                    content_hash=content_hash,
                )

                return self._settle_existing(winner, scope, data)

            work, created = ensure_candidate_decision_work_locked(candidate)
            if created:
                queue_work_attempt(work_id=work.id, now=timezone.now(), origin=WorkflowRunOrigin.MANUAL)
            self._write_audit(candidate, scope, data, reused=False)

            return ProposeMemoryResult(candidate.id, candidate.status, created)

    def _settle_existing(
        self,
        candidate: MemoryCandidate,
        scope: EffectiveScope,
        data: ProposeMemoryInput,
    ) -> ProposeMemoryResult:
        if candidate.status != CandidateStatus.PROPOSED:
            self._write_audit(candidate, scope, data, reused=True)

            return ProposeMemoryResult(candidate.id, candidate.status, False)

        work, created = ensure_candidate_decision_work_locked(candidate)
        if created:
            queue_work_attempt(work_id=work.id, now=timezone.now(), origin=WorkflowRunOrigin.MANUAL)
        self._write_audit(candidate, scope, data, reused=True)

        return ProposeMemoryResult(candidate.id, candidate.status, created)

    def _write_audit(
        self,
        candidate: MemoryCandidate,
        scope: EffectiveScope,
        data: ProposeMemoryInput,
        *,
        reused: bool,
    ) -> None:
        metadata: dict[str, object] = {
            'kind': candidate.kind,
            'body_length': len(candidate.body),
            'reused': reused,
        }
        if reused:
            metadata['status'] = candidate.status
        AuditEvent.objects.create(
            organization_id=candidate.organization_id,
            project_id=candidate.project_id,
            team_id=candidate.team_id,
            event_type='MemoryProposeReused' if reused else 'MemoryProposed',
            actor_type=scope.actor_type,
            actor_id=scope.actor_id,
            target_type='memory_candidate',
            target_id=str(candidate.id),
            capability='memories:propose',
            result=AuditResult.RECORDED,
            request_id=data.request_id[:_AUDIT_FIELD_MAX_LENGTH],
            correlation_id=(data.correlation_id or '')[:_AUDIT_FIELD_MAX_LENGTH],
            metadata=metadata,
        )

        return
