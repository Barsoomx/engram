from __future__ import annotations

import structlog

from engram.access.models import OrganizationMembership, Role
from engram.console.exceptions import LastOwnerError
from engram.core.domain.usecases.base import BaseUseCaseInputDTO, BaseUseCaseOutputDTO
from engram.core.domain.usecases.transactional_base import UseCaseTransactional
from engram.core.models import Organization

logger = structlog.get_logger(__name__)

OWNER_ROLE_CODE = 'organization_owner'


def _active_owner_count(organization: Organization) -> int:
    return OrganizationMembership.objects.filter(
        organization=organization,
        role__code=OWNER_ROLE_CODE,
        active=True,
    ).count()


class SetMemberRoleInput(BaseUseCaseInputDTO):
    membership: OrganizationMembership
    role: Role


class SetMemberRoleOutput(BaseUseCaseOutputDTO):
    membership: OrganizationMembership


class SetMemberRole(UseCaseTransactional[SetMemberRoleInput, SetMemberRoleOutput]):
    def _execute(self, input_dto: SetMemberRoleInput | None) -> SetMemberRoleOutput:
        assert input_dto is not None

        membership = input_dto.membership
        role = input_dto.role

        is_current_owner = membership.role.code == OWNER_ROLE_CODE and membership.active

        if is_current_owner and role.code != OWNER_ROLE_CODE:
            if _active_owner_count(membership.organization) <= 1:
                raise LastOwnerError(
                    'cannot demote the last active organization owner',
                )

        membership.role = role

        membership.save(update_fields=['role', 'updated_at'])

        logger.info(
            'member_role_changed',
            organization_id=str(membership.organization_id),
            identity_id=str(membership.identity_id),
            role=role.code,
        )

        return SetMemberRoleOutput(membership=membership)


class RemoveMemberInput(BaseUseCaseInputDTO):
    membership: OrganizationMembership


class RemoveMemberOutput(BaseUseCaseOutputDTO):
    membership: OrganizationMembership


class RemoveMember(UseCaseTransactional[RemoveMemberInput, RemoveMemberOutput]):
    def _execute(self, input_dto: RemoveMemberInput | None) -> RemoveMemberOutput:
        assert input_dto is not None

        membership = input_dto.membership

        is_current_owner = membership.role.code == OWNER_ROLE_CODE and membership.active

        if is_current_owner and _active_owner_count(membership.organization) <= 1:
            raise LastOwnerError(
                'cannot remove the last active organization owner',
            )

        membership.active = False

        membership.save(update_fields=['active', 'updated_at'])

        logger.info(
            'member_removed',
            organization_id=str(membership.organization_id),
            identity_id=str(membership.identity_id),
        )

        return RemoveMemberOutput(membership=membership)
