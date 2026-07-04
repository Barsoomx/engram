from __future__ import annotations

import uuid
from typing import Any

import structlog

from engram.access.models import Identity
from engram.console.services import (
    MemoryReviewError,
    approve_memory_candidate,
    archive_memory,
    edit_memory_body,
    get_review_candidate_or_404,
    get_review_memory_or_404,
    narrow_memory,
    reject_review_item,
    restore_memory,
    supersede_memory,
)
from engram.core.domain.usecases.base import BaseUseCaseInputDTO, BaseUseCaseOutputDTO
from engram.core.domain.usecases.transactional_base import UseCaseTransactional
from engram.core.models import MemoryCandidate, Organization

logger = structlog.get_logger(__name__)


class ReviewActionInput(BaseUseCaseInputDTO):
    organization: Organization
    actor_identity: Identity
    item_id: uuid.UUID
    action_name: str
    reason: str
    body: str | None = None
    target_memory_id: uuid.UUID | None = None


class ReviewActionOutput(BaseUseCaseOutputDTO):
    result: dict[str, Any]


class ReviewActionUseCase(UseCaseTransactional[ReviewActionInput, ReviewActionOutput]):
    _DISPATCH = {
        'approve': '_apply_approve',
        'edit': '_apply_edit',
        'narrow': '_apply_narrow',
        'supersede': '_apply_supersede',
        'reject': '_apply_reject',
        'archive': '_apply_archive',
        'restore': '_apply_restore',
    }

    def _execute(self, input_dto: ReviewActionInput | None) -> ReviewActionOutput:
        assert input_dto is not None

        handler_name = self._DISPATCH.get(input_dto.action_name)

        if handler_name is None:
            raise MemoryReviewError('unknown_action', f'unknown action {input_dto.action_name!r}')

        result = getattr(self, handler_name)(input_dto)

        logger.info(
            'memory_review_action_applied',
            action=result['action'],
            item_id=str(input_dto.item_id),
            item_type='candidate' if 'candidate_id' in result else 'memory',
            organization_id=str(input_dto.organization.id),
        )

        return ReviewActionOutput(result=result)

    def _apply_approve(self, input_dto: ReviewActionInput) -> dict[str, Any]:
        candidate = get_review_candidate_or_404(input_dto.organization, input_dto.item_id)

        memory = approve_memory_candidate(
            input_dto.organization,
            input_dto.actor_identity,
            candidate,
            input_dto.reason,
        )

        return {
            'action': 'approve',
            'candidate_id': str(candidate.id),
            'memory_id': str(memory.id),
        }

    def _apply_edit(self, input_dto: ReviewActionInput) -> dict[str, Any]:
        memory = get_review_memory_or_404(input_dto.organization, input_dto.item_id)

        if not input_dto.body:
            raise MemoryReviewError('body_required', 'body is required for edit action')

        version = edit_memory_body(
            input_dto.organization,
            input_dto.actor_identity,
            memory,
            input_dto.body,
            input_dto.reason,
        )

        return {
            'action': 'edit',
            'memory_id': str(memory.id),
            'version': version.version,
        }

    def _apply_narrow(self, input_dto: ReviewActionInput) -> dict[str, Any]:
        memory = get_review_memory_or_404(input_dto.organization, input_dto.item_id)

        if input_dto.target_memory_id is None:
            raise MemoryReviewError(
                'target_required',
                'target_memory_id is required for narrow action',
            )

        link = narrow_memory(
            input_dto.organization,
            input_dto.actor_identity,
            memory,
            input_dto.target_memory_id,
            input_dto.reason,
        )

        return {
            'action': 'narrow',
            'memory_id': str(memory.id),
            'link_id': str(link.id),
        }

    def _apply_supersede(self, input_dto: ReviewActionInput) -> dict[str, Any]:
        memory = get_review_memory_or_404(input_dto.organization, input_dto.item_id)

        if input_dto.target_memory_id is None:
            raise MemoryReviewError(
                'target_required',
                'target_memory_id is required for supersede action',
            )

        link = supersede_memory(
            input_dto.organization,
            input_dto.actor_identity,
            memory,
            input_dto.target_memory_id,
            input_dto.reason,
        )

        return {
            'action': 'supersede',
            'memory_id': str(memory.id),
            'link_id': str(link.id),
        }

    def _apply_reject(self, input_dto: ReviewActionInput) -> dict[str, Any]:
        candidate = MemoryCandidate.objects.filter(
            organization=input_dto.organization,
            id=input_dto.item_id,
        ).first()

        if candidate is not None:
            reject_review_item(input_dto.organization, input_dto.actor_identity, candidate, input_dto.reason)

            return {
                'action': 'reject',
                'candidate_id': str(candidate.id),
            }

        memory = get_review_memory_or_404(input_dto.organization, input_dto.item_id)

        reject_review_item(input_dto.organization, input_dto.actor_identity, memory, input_dto.reason)

        return {
            'action': 'reject',
            'memory_id': str(memory.id),
        }

    def _apply_archive(self, input_dto: ReviewActionInput) -> dict[str, Any]:
        memory = get_review_memory_or_404(input_dto.organization, input_dto.item_id)

        archive_memory(input_dto.organization, input_dto.actor_identity, memory, input_dto.reason)

        return {
            'action': 'archive',
            'memory_id': str(memory.id),
        }

    def _apply_restore(self, input_dto: ReviewActionInput) -> dict[str, Any]:
        memory = get_review_memory_or_404(input_dto.organization, input_dto.item_id)

        restore_memory(input_dto.organization, input_dto.actor_identity, memory, input_dto.reason)

        return {
            'action': 'restore',
            'memory_id': str(memory.id),
        }
