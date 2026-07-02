from __future__ import annotations

import hashlib
import secrets
import uuid
from collections.abc import Iterable
from typing import Any

import structlog
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import status

from engram.access.models import (
    ApiKey,
    ApiKeyCapability,
    Capability,
    Identity,
    IdentityType,
    MembershipStatus,
    OrganizationMembership,
    Role,
)
from engram.access.services import (
    api_key_fingerprint,
    api_key_prefix,
    hash_api_key,
)
from engram.console.exceptions import (
    MemberAlreadyInvitedError,
    ProjectSlugTakenError,
    TeamSlugTakenError,
)
from engram.core.domain.usecases.errors import DomainError
from engram.core.models import (
    AuditEvent,
    AuditResult,
    CandidateStatus,
    LinkType,
    Memory,
    MemoryCandidate,
    MemoryLink,
    MemoryStatus,
    MemoryVersion,
    Organization,
    Project,
    Team,
)
from engram.core.repository import canonicalize_repository_url

logger = structlog.get_logger(__name__)

API_KEY_TOKEN_PREFIX = 'egk_'

WILDCARD_ADMIN_CAPABILITY = 'policy:admin'


def audit_admin_action(
    *,
    organization: Organization,
    actor_identity: Identity,
    event_type: str,
    target_type: str,
    target_id: str,
    metadata: dict[str, Any] | None = None,
    result: str = AuditResult.RECORDED,
) -> AuditEvent:
    return AuditEvent.objects.create(
        organization=organization,
        event_type=event_type,
        actor_type='user',
        actor_id=str(actor_identity.id),
        target_type=target_type,
        target_id=target_id,
        capability='',
        result=result,
        metadata=metadata or {},
    )


@transaction.atomic
def create_team(
    *,
    organization: Organization,
    name: str,
    slug: str,
) -> Team:
    try:
        team = Team.objects.create(organization=organization, name=name, slug=slug)
    except IntegrityError:
        raise TeamSlugTakenError(
            f'team slug {slug!r} already exists in this organization',
        ) from None

    logger.info(
        'team_created',
        organization_id=str(organization.id),
        team_id=str(team.id),
        slug=slug,
    )

    return team


@transaction.atomic
def archive_team(team: Team) -> Team:
    team.archived_at = timezone.now()

    team.save(update_fields=['archived_at', 'updated_at'])

    logger.info(
        'team_archived',
        organization_id=str(team.organization_id),
        team_id=str(team.id),
    )

    return team


@transaction.atomic
def create_project(
    *,
    organization: Organization,
    name: str,
    slug: str,
    repository_url: str = '',
    default_branch: str = '',
) -> Project:
    try:
        project = Project.objects.create(
            organization=organization,
            name=name,
            slug=slug,
            repository_url=canonicalize_repository_url(repository_url) or repository_url,
            default_branch=default_branch,
        )
    except IntegrityError:
        raise ProjectSlugTakenError(
            f'project slug {slug!r} already exists in this organization',
        ) from None

    logger.info(
        'project_created',
        organization_id=str(organization.id),
        project_id=str(project.id),
        slug=slug,
    )

    return project


@transaction.atomic
def archive_project(project: Project) -> Project:
    project.archived_at = timezone.now()

    project.save(update_fields=['archived_at', 'updated_at'])

    logger.info(
        'project_archived',
        organization_id=str(project.organization_id),
        project_id=str(project.id),
    )

    return project


@transaction.atomic
def invite_member(
    *,
    organization: Organization,
    external_id: str,
    display_name: str,
    email: str,
    role: Role,
) -> OrganizationMembership:
    try:
        identity = Identity.objects.create(
            organization=organization,
            identity_type=IdentityType.USER,
            external_id=external_id,
            display_name=display_name,
            email=email,
        )

        membership = OrganizationMembership.objects.create(
            organization=organization,
            identity=identity,
            role=role,
            status=MembershipStatus.INVITED,
        )
    except IntegrityError:
        raise MemberAlreadyInvitedError(
            f'identity {external_id!r} is already a member of this organization',
        ) from None

    logger.info(
        'member_invited',
        organization_id=str(organization.id),
        identity_id=str(identity.id),
        role=role.code,
    )

    return membership


class MemberNotFoundError(Exception):
    pass


@transaction.atomic
def activate_member(
    *,
    organization: Organization,
    actor_identity: Identity,
    membership_id: uuid.UUID,
) -> OrganizationMembership:
    membership = (
        OrganizationMembership.objects.select_for_update().filter(organization=organization, id=membership_id).first()
    )

    if membership is None:
        raise MemberNotFoundError('member not found')

    if membership.status == MembershipStatus.ACTIVE:
        return membership

    membership.status = MembershipStatus.ACTIVE

    membership.save(update_fields=['status', 'updated_at'])

    audit_admin_action(
        organization=organization,
        actor_identity=actor_identity,
        event_type='MemberActivated',
        target_type='member',
        target_id=str(membership.id),
    )

    logger.info(
        'member_activated',
        organization_id=str(organization.id),
        identity_id=str(membership.identity_id),
    )

    return membership


class CapabilityWideningError(DomainError):
    default_error_code = 'capability_widening'
    default_status_code = status.HTTP_400_BAD_REQUEST


def _issuer_can_grant(
    requested_capabilities: Iterable[str],
    issuer_capabilities: Iterable[str],
) -> set[str]:
    issuer = set(issuer_capabilities)

    granted: set[str] = set()

    for capability in requested_capabilities:
        if capability in issuer:
            granted.add(capability)

            continue

        group = capability.split(':')[0]

        if f'{group}:*' in issuer or WILDCARD_ADMIN_CAPABILITY in issuer:
            granted.add(capability)

            continue

        raise CapabilityWideningError(
            f'issuer cannot grant capability {capability!r}',
        )

    return granted


def generate_api_key_plaintext() -> str:
    return f'{API_KEY_TOKEN_PREFIX}{secrets.token_urlsafe(32)}'


@transaction.atomic
def issue_api_key(
    *,
    organization: Organization,
    owner_identity: Identity,
    name: str,
    capabilities: list[str],
    team: Team | None = None,
    project: Project | None = None,
    expires_at: Any = None,
) -> tuple[ApiKey, str]:
    capability_objs = list(Capability.objects.filter(code__in=capabilities))

    found_codes = {capability.code for capability in capability_objs}

    missing_codes = set(capabilities) - found_codes

    if missing_codes:
        raise CapabilityWideningError(
            f'unknown capabilities: {sorted(missing_codes)}',
        )

    plaintext = generate_api_key_plaintext()

    api_key = ApiKey(
        organization=organization,
        owner_identity=owner_identity,
        name=name,
        key_prefix=api_key_prefix(plaintext),
        key_hash=hash_api_key(plaintext),
        key_fingerprint=api_key_fingerprint(plaintext),
        team=team,
        project=project,
        active=True,
        expires_at=expires_at,
    )

    api_key.full_clean()

    api_key.save()

    for capability in capability_objs:
        ApiKeyCapability.objects.get_or_create(api_key=api_key, capability=capability)

    logger.info(
        'api_key_issued',
        organization_id=str(organization.id),
        key_id=str(api_key.id),
        capabilities=capabilities,
    )

    return api_key, plaintext


@transaction.atomic
def revoke_api_key(api_key: ApiKey) -> ApiKey:
    api_key.revoked_at = timezone.now()

    api_key.save(update_fields=['revoked_at', 'updated_at'])

    logger.info(
        'api_key_revoked',
        organization_id=str(api_key.organization_id),
        key_id=str(api_key.id),
    )

    return api_key


REVIEW_LOW_CONFIDENCE_THRESHOLD = '0.300'


class MemoryReviewError(DomainError):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message, error_code=code, status_code=status)

        self.code = code
        self.status = status

        self.status = status


def get_review_candidate_or_404(
    organization: Organization,
    item_id: uuid.UUID,
) -> MemoryCandidate:
    candidate = MemoryCandidate.objects.filter(
        organization=organization,
        id=item_id,
    ).first()

    if candidate is None:
        raise MemoryReviewError('not_found', 'review item not found', status=404)

    return candidate


def get_review_memory_or_404(
    organization: Organization,
    memory_id: uuid.UUID,
) -> Memory:
    memory = Memory.objects.filter(organization=organization, id=memory_id).first()

    if memory is None:
        raise MemoryReviewError('not_found', 'memory not found', status=404)

    return memory


def _lock_candidate_or_404(
    organization: Organization,
    candidate_id: uuid.UUID,
) -> MemoryCandidate:
    candidate = (
        MemoryCandidate.objects.select_for_update()
        .filter(
            organization=organization,
            id=candidate_id,
        )
        .first()
    )

    if candidate is None:
        raise MemoryReviewError('not_found', 'candidate not found', status=404)

    return candidate


def _lock_memory_or_404(
    organization: Organization,
    memory_id: uuid.UUID,
) -> Memory:
    memory = (
        Memory.objects.select_for_update()
        .filter(
            organization=organization,
            id=memory_id,
        )
        .first()
    )

    if memory is None:
        raise MemoryReviewError('not_found', 'memory not found', status=404)

    return memory


@transaction.atomic
def approve_memory_candidate(
    organization: Organization,
    actor_identity: Identity,
    candidate: MemoryCandidate,
    reason: str,
) -> Memory:
    candidate = _lock_candidate_or_404(organization, candidate.id)

    if candidate.status != CandidateStatus.PROPOSED:
        raise MemoryReviewError(
            'invalid_state',
            'only proposed candidates can be approved',
        )

    memory = Memory.objects.create(
        organization=candidate.organization,
        project=candidate.project,
        team=candidate.team,
        title=candidate.title,
        body=candidate.body,
        status=MemoryStatus.APPROVED,
        visibility_scope=candidate.visibility_scope,
        confidence=candidate.confidence,
        metadata={
            'source': 'memory_candidate',
            'memory_candidate_id': str(candidate.id),
            'evidence': candidate.evidence,
        },
    )

    MemoryVersion.objects.create(
        organization=memory.organization,
        project=memory.project,
        memory=memory,
        version=1,
        body=candidate.body,
        content_hash=candidate.content_hash,
        source_observation=candidate.source_observation,
    )

    candidate.status = CandidateStatus.PROMOTED

    candidate.promoted_memory = memory

    candidate.save(update_fields=['status', 'promoted_memory', 'updated_at'])

    audit_admin_action(
        organization=organization,
        actor_identity=actor_identity,
        event_type='MemoryReviewed',
        target_type='memory_candidate',
        target_id=str(candidate.id),
        metadata={'action': 'approve', 'reason': reason},
    )

    return memory


@transaction.atomic
def edit_memory_body(
    organization: Organization,
    actor_identity: Identity,
    memory: Memory,
    body: str,
    reason: str,
) -> MemoryVersion:
    memory = _lock_memory_or_404(organization, memory.id)

    next_version = memory.current_version + 1

    version = MemoryVersion.objects.create(
        organization=memory.organization,
        project=memory.project,
        memory=memory,
        version=next_version,
        body=body,
        content_hash=_memory_body_hash(memory.id, next_version, body),
    )

    memory.body = body

    memory.current_version = next_version

    memory.save(update_fields=['body', 'current_version', 'updated_at'])

    audit_admin_action(
        organization=organization,
        actor_identity=actor_identity,
        event_type='MemoryReviewed',
        target_type='memory',
        target_id=str(memory.id),
        metadata={'action': 'edit', 'reason': reason, 'version': next_version},
    )

    return version


@transaction.atomic
def narrow_memory(
    organization: Organization,
    actor_identity: Identity,
    memory: Memory,
    target_memory_id: uuid.UUID,
    reason: str,
) -> MemoryLink:
    memory = _lock_memory_or_404(organization, memory.id)

    return _record_memory_link(
        organization=organization,
        actor_identity=actor_identity,
        memory=memory,
        link_type=LinkType.NARROWED_BY,
        target_id=target_memory_id,
        action='narrow',
        reason=reason,
    )


@transaction.atomic
def supersede_memory(
    organization: Organization,
    actor_identity: Identity,
    memory: Memory,
    target_memory_id: uuid.UUID,
    reason: str,
) -> MemoryLink:
    memory = _lock_memory_or_404(organization, memory.id)

    memory.stale = True

    memory.save(update_fields=['stale', 'updated_at'])

    return _record_memory_link(
        organization=organization,
        actor_identity=actor_identity,
        memory=memory,
        link_type=LinkType.SUPERSEDED_BY,
        target_id=target_memory_id,
        action='supersede',
        reason=reason,
    )


def _record_memory_link(
    organization: Organization,
    actor_identity: Identity,
    memory: Memory,
    link_type: str,
    target_id: uuid.UUID,
    action: str,
    reason: str,
) -> MemoryLink:
    if memory.organization_id != organization.id:
        raise MemoryReviewError('not_found', 'memory not found', status=404)

    target = str(target_id)

    link, _created = MemoryLink.objects.get_or_create(
        memory=memory,
        link_type=link_type,
        target=target,
        defaults={
            'organization': memory.organization,
            'project': memory.project,
            'label': '',
        },
    )

    audit_admin_action(
        organization=organization,
        actor_identity=actor_identity,
        event_type='MemoryReviewed',
        target_type='memory',
        target_id=str(memory.id),
        metadata={
            'action': action,
            'reason': reason,
            'link_id': str(link.id),
            'target_memory_id': target,
        },
    )

    return link


@transaction.atomic
def reject_review_item(
    organization: Organization,
    actor_identity: Identity,
    item: MemoryCandidate | Memory,
    reason: str,
) -> None:
    if isinstance(item, MemoryCandidate):
        item = _lock_candidate_or_404(organization, item.id)

        if item.status == CandidateStatus.REJECTED:
            return

        item.status = CandidateStatus.REJECTED

        item.save(update_fields=['status', 'updated_at'])

        target_type = 'memory_candidate'

    else:
        item = _lock_memory_or_404(organization, item.id)

        if item.status == MemoryStatus.REFUTED:
            return

        item.status = MemoryStatus.REFUTED

        item.save(update_fields=['status', 'updated_at'])

        target_type = 'memory'

    audit_admin_action(
        organization=organization,
        actor_identity=actor_identity,
        event_type='MemoryReviewed',
        target_type=target_type,
        target_id=str(item.id),
        metadata={'action': 'reject', 'reason': reason},
    )


@transaction.atomic
def archive_memory(
    organization: Organization,
    actor_identity: Identity,
    memory: Memory,
    reason: str,
) -> Memory:
    memory = _lock_memory_or_404(organization, memory.id)

    if memory.status == MemoryStatus.ARCHIVED:
        return memory

    memory.status = MemoryStatus.ARCHIVED

    memory.save(update_fields=['status', 'updated_at'])

    audit_admin_action(
        organization=organization,
        actor_identity=actor_identity,
        event_type='MemoryReviewed',
        target_type='memory',
        target_id=str(memory.id),
        metadata={'action': 'archive', 'reason': reason},
    )

    return memory


@transaction.atomic
def bulk_archive_memories(
    organization: Organization,
    actor_identity: Identity,
    reason: str,
    *,
    ids: list[uuid.UUID] | None = None,
    confidence_lte: str | None = None,
) -> list[uuid.UUID]:
    archived_ids: list[uuid.UUID] = []

    if ids is not None:
        memories = list(
            Memory.objects.filter(organization=organization, id__in=ids),
        )

    else:
        memories = list(
            Memory.objects.filter(
                organization=organization,
                confidence__lte=confidence_lte,
            ).exclude(status=MemoryStatus.ARCHIVED),
        )

    for memory in memories:
        archive_memory(organization, actor_identity, memory, reason)

        archived_ids.append(memory.id)

    return archived_ids


def _memory_body_hash(memory_id: uuid.UUID, version: int, body: str) -> str:
    source = f'{memory_id}:{version}:{body}'

    return hashlib.sha256(source.encode()).hexdigest()
