from __future__ import annotations

import hashlib
import secrets
import uuid
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

import structlog
from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef, Q, Subquery
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
    EffectiveScope,
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
    CurationDecision,
    Memory,
    MemoryCandidate,
    MemoryConflict,
    MemoryLink,
    MemoryReviewExample,
    MemoryStatus,
    MemoryVersion,
    Observation,
    Organization,
    Project,
    Team,
)
from engram.core.redaction import redact_value as core_redact_value
from engram.core.repository import canonicalize_repository_url
from engram.memory.conflict_links import clear_candidate_conflict_links
from engram.memory.import_provenance import candidate_evidence_manifest
from engram.memory.services import (
    PromoteMemoryCandidate,
    PromoteMemoryCandidateInput,
)
from engram.memory.transitions import (
    ArchiveMemory,
    CandidateFence,
    MemoryStateInput,
    MemoryTransitionError,
    MergeMemories,
    MergeMemoriesInput,
    RefuteMemory,
    ResolveMemoryConflict,
    ResolveMemoryConflictInput,
    RestoreMemory,
    ReviseMemory,
    ReviseMemoryInput,
    SupersedeMemories,
    SupersedeMemoriesInput,
    TransitionRequest,
    TransitionScope,
    build_memory_fence,
)

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


def reactivate_member(
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

    if membership.active:
        return membership

    membership.active = True

    membership.save(update_fields=['active', 'updated_at'])

    audit_admin_action(
        organization=organization,
        actor_identity=actor_identity,
        event_type='MemberReactivated',
        target_type='member',
        target_id=str(membership.id),
    )

    logger.info(
        'member_reactivated',
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


def _redact_text(value: str) -> str:
    return str(core_redact_value(value).value)


def _review_example_curator_context(item: MemoryCandidate | Memory) -> dict[str, object]:
    if not isinstance(item, MemoryCandidate):
        return {}

    context: dict[str, object] = {}

    conflicts = [entry for entry in item.evidence if isinstance(entry, dict) and entry.get('type') == 'conflict']

    if conflicts:
        context['conflicts'] = conflicts

    held_event = (
        AuditEvent.objects.filter(
            organization=item.organization,
            target_type='memory_candidate',
            target_id=str(item.id),
            event_type='MemoryCandidateHeldForReview',
        )
        .order_by('-created_at')
        .first()
    )

    if held_event is not None:
        context['held_reason'] = held_event.metadata.get('reason', '')

    return context


def _record_review_example(
    *,
    organization: Organization,
    actor_identity: Identity,
    item: MemoryCandidate | Memory,
    action: str,
    reason: str,
    curator_context: dict[str, object] | None = None,
) -> MemoryReviewExample:
    is_candidate = isinstance(item, MemoryCandidate)

    if is_candidate:
        evidence = list(item.evidence)
    else:
        evidence = list(item.metadata.get('evidence', [])) if isinstance(item.metadata, dict) else []

    snapshot = {
        'title': _redact_text(item.title),
        'body': _redact_text(item.body),
        'status': item.status,
        'confidence': str(item.confidence) if item.confidence is not None else None,
        'kind': item.kind,
        'visibility_scope': item.visibility_scope,
        'evidence': evidence,
    }

    return MemoryReviewExample.objects.create(
        organization=organization,
        project=item.project,
        team=item.team,
        item_type='memory_candidate' if is_candidate else 'memory',
        item_id=str(item.id),
        action=action,
        snapshot=snapshot,
        curator_context=curator_context if curator_context is not None else _review_example_curator_context(item),
        reason=reason,
        actor_id=str(actor_identity.id),
    )


def _memory_transition_request(
    *,
    organization: Organization,
    actor_identity: Identity,
    review_example: MemoryReviewExample,
    action: str,
    reason: str,
) -> TransitionRequest:
    operation_id = f'console-memory-review:{review_example.id}:{action}:v1'

    return TransitionRequest(
        scope=TransitionScope(
            organization_id=organization.id,
            project_id=review_example.project_id,
            team_id=review_example.team_id,
        ),
        idempotency_key=operation_id,
        actor_type='user',
        actor_id=str(actor_identity.id),
        capability='memories:review',
        request_id=operation_id,
        correlation_id=operation_id,
        reason=reason,
        origin='console',
    )


def _execute_memory_transition(call: Any) -> Any:
    try:
        return call()
    except MemoryTransitionError as error:
        code = {
            'scope': 'not_found',
            'memory_state': 'invalid_state',
            'stale_decision': 'invalid_state',
            'projection': 'invalid_state',
            'idempotency_collision': 'invalid_state',
        }.get(error.code, error.code)
        status_code = 404 if code == 'not_found' else 400
        raise MemoryReviewError(code, str(error), status=status_code) from error


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
    if candidate.decision_work_contract_version != 1:
        raise MemoryReviewError(
            'invalid_state',
            'legacy memory candidate promotion is disabled; typed promotion requires decision work contract version 1',
        )

    review_example = _record_review_example(
        organization=organization,
        actor_identity=actor_identity,
        item=candidate,
        action='approve',
        reason=reason,
    )

    operation_id = f'console-memory-review:{review_example.id}'
    memory = (
        PromoteMemoryCandidate()
        .execute(
            PromoteMemoryCandidateInput(
                candidate_id=candidate.id,
                actor_type='user',
                actor_id=str(actor_identity.id),
                capability='memories:review',
                request_id=operation_id,
                correlation_id=operation_id,
                reason=reason,
                origin='console',
            )
        )
        .memory
    )

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

    if memory.kind == 'digest':
        raise MemoryReviewError('invalid_state', 'digest memories cannot be edited')

    review_example = _record_review_example(
        organization=organization,
        actor_identity=actor_identity,
        item=memory,
        action='edit',
        reason=reason,
    )
    result = _execute_memory_transition(
        lambda: ReviseMemory().execute(
            ReviseMemoryInput(
                request=_memory_transition_request(
                    organization=organization,
                    actor_identity=actor_identity,
                    review_example=review_example,
                    action='edit',
                    reason=reason,
                ),
                memory_fence=build_memory_fence(memory),
                title=memory.title,
                body=body,
            )
        )
    )

    return result.memory_version


def _resolve_link_target_or_404(
    organization: Organization,
    memory: Memory,
    target_id: uuid.UUID,
) -> Memory:
    target = Memory.objects.filter(
        organization=organization,
        project=memory.project,
        id=target_id,
    ).first()

    if target is None:
        raise MemoryReviewError('not_found', 'target memory not found', status=404)

    return target


@transaction.atomic
def narrow_memory(
    organization: Organization,
    actor_identity: Identity,
    memory: Memory,
    target_memory_id: uuid.UUID,
    reason: str,
) -> MemoryLink:
    memory = _lock_memory_or_404(organization, memory.id)

    target = _resolve_link_target_or_404(organization, memory, target_memory_id)

    review_example = _record_review_example(
        organization=organization,
        actor_identity=actor_identity,
        item=memory,
        action='narrow',
        reason=reason,
    )

    result = _execute_memory_transition(
        lambda: MergeMemories().execute(
            MergeMemoriesInput(
                request=_memory_transition_request(
                    organization=organization,
                    actor_identity=actor_identity,
                    review_example=review_example,
                    action='narrow',
                    reason=reason,
                ),
                source_memory_fence=build_memory_fence(memory),
                result_memory_fence=build_memory_fence(target),
                title=target.title,
                body=target.body,
            )
        )
    )

    return result.transition.semantic_link


@transaction.atomic
def supersede_memory(
    organization: Organization,
    actor_identity: Identity,
    memory: Memory,
    target_memory_id: uuid.UUID,
    reason: str,
) -> MemoryLink:
    memory = _lock_memory_or_404(organization, memory.id)

    target = _resolve_link_target_or_404(organization, memory, target_memory_id)

    review_example = _record_review_example(
        organization=organization,
        actor_identity=actor_identity,
        item=memory,
        action='supersede',
        reason=reason,
    )

    result = _execute_memory_transition(
        lambda: SupersedeMemories().execute(
            SupersedeMemoriesInput(
                request=_memory_transition_request(
                    organization=organization,
                    actor_identity=actor_identity,
                    review_example=review_example,
                    action='supersede',
                    reason=reason,
                ),
                source_memory_fence=build_memory_fence(memory),
                result_memory_fence=build_memory_fence(target),
            )
        )
    )

    return result.transition.semantic_link


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

        if item.status != CandidateStatus.PROPOSED:
            raise MemoryReviewError(
                'invalid_state',
                'only proposed candidates can be rejected',
            )

        _record_review_example(
            organization=organization,
            actor_identity=actor_identity,
            item=item,
            action='reject',
            reason=reason,
        )

        item.status = CandidateStatus.REJECTED

        item.save(update_fields=['status', 'updated_at'])

        clear_candidate_conflict_links(item)

    else:
        item = _lock_memory_or_404(organization, item.id)

        if item.status == MemoryStatus.REFUTED:
            return

        review_example = _record_review_example(
            organization=organization,
            actor_identity=actor_identity,
            item=item,
            action='reject',
            reason=reason,
        )
        _execute_memory_transition(
            lambda: RefuteMemory().execute(
                MemoryStateInput(
                    request=_memory_transition_request(
                        organization=organization,
                        actor_identity=actor_identity,
                        review_example=review_example,
                        action='reject',
                        reason=reason,
                    ),
                    memory_fence=build_memory_fence(item),
                )
            )
        )

        return

    audit_admin_action(
        organization=organization,
        actor_identity=actor_identity,
        event_type='MemoryReviewed',
        target_type='memory_candidate',
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

    review_example = _record_review_example(
        organization=organization,
        actor_identity=actor_identity,
        item=memory,
        action='archive',
        reason=reason,
    )

    result = _execute_memory_transition(
        lambda: ArchiveMemory().execute(
            MemoryStateInput(
                request=_memory_transition_request(
                    organization=organization,
                    actor_identity=actor_identity,
                    review_example=review_example,
                    action='archive',
                    reason=reason,
                ),
                memory_fence=build_memory_fence(memory),
            )
        )
    )

    return result.memory


@transaction.atomic
def restore_memory(
    organization: Organization,
    actor_identity: Identity,
    memory: Memory,
    reason: str,
) -> Memory:
    memory = _lock_memory_or_404(organization, memory.id)

    if memory.status == MemoryStatus.APPROVED and not memory.refuted and not memory.stale:
        raise MemoryReviewError('invalid_state', 'memory is already active')

    if not memory.versions.filter(version=memory.current_version).exists():
        raise MemoryReviewError('invalid_state', 'memory has no version to restore')

    review_example = _record_review_example(
        organization=organization,
        actor_identity=actor_identity,
        item=memory,
        action='restore',
        reason=reason,
    )

    result = _execute_memory_transition(
        lambda: RestoreMemory().execute(
            MemoryStateInput(
                request=_memory_transition_request(
                    organization=organization,
                    actor_identity=actor_identity,
                    review_example=review_example,
                    action='restore',
                    reason=reason,
                ),
                memory_fence=build_memory_fence(memory),
            )
        )
    )

    return result.memory


REVIEW_MEMORY_STATUSES = (
    MemoryStatus.CONFLICT,
    MemoryStatus.REFUTED,
)

REVIEW_MEMORY_CONFIDENCE_THRESHOLD = '0.300'


def reviewable_memory_filter() -> Q:
    return (
        Q(status__in=REVIEW_MEMORY_STATUSES)
        | Q(status=MemoryStatus.APPROVED, confidence__lte=REVIEW_MEMORY_CONFIDENCE_THRESHOLD)
        | Q(status=MemoryStatus.APPROVED, refuted=True)
    )


@transaction.atomic
def bulk_archive_memories(
    organization: Organization,
    actor_identity: Identity,
    reason: str,
    *,
    ids: list[uuid.UUID] | None = None,
    confidence_lte: str | None = None,
    project_id: uuid.UUID | None = None,
    team_id: uuid.UUID | None = None,
) -> list[uuid.UUID]:
    archived_ids: list[uuid.UUID] = []

    if ids is not None:
        memories = list(
            Memory.objects.filter(organization=organization, id__in=ids),
        )

    else:
        queryset = Memory.objects.filter(
            organization=organization,
            confidence__lte=confidence_lte,
            transition_contract_version=1,
            current_transition__isnull=False,
        ).filter(reviewable_memory_filter())

        if project_id is not None:
            queryset = queryset.filter(project_id=project_id)

        if team_id is not None:
            queryset = queryset.filter(team_id=team_id)

        memories = list(queryset)

    for memory in memories:
        archive_memory(organization, actor_identity, memory, reason)

        archived_ids.append(memory.id)

    return archived_ids


CONFLICT_RESOLUTION_ACTIONS = (
    'publish_candidate',
    'merge_candidate',
    'supersede_memory',
    'reject_candidate',
)

_CONFLICT_TARGET_ACTIONS = ('merge_candidate', 'supersede_memory')


def _scope_conflict_candidates(queryset: Any, scope: EffectiveScope) -> Any:
    return queryset.filter(project_id__in=scope.project_ids).filter(
        Q(team_id__isnull=True) | Q(team_id__in=scope.team_ids),
    )


def open_conflict_candidates(
    organization: Organization,
    scope: EffectiveScope,
    *,
    project_id: uuid.UUID | None = None,
    team_id: uuid.UUID | None = None,
    opened_at__gte: Any = None,
    search: str | None = None,
) -> Any:
    unresolved = MemoryConflict.objects.filter(
        candidate_id=OuterRef('pk'),
        resolved_transition__isnull=True,
    )

    queryset = _scope_conflict_candidates(
        MemoryCandidate.objects.filter(organization=organization).filter(Exists(unresolved)),
        scope,
    )

    if project_id is not None:
        queryset = queryset.filter(project_id=project_id)

    if team_id is not None:
        queryset = queryset.filter(team_id=team_id)

    if search:
        queryset = queryset.filter(_conflict_search_predicate(search))

    opened_at_subquery = (
        MemoryConflict.objects.filter(
            candidate_id=OuterRef('pk'),
            resolved_transition__isnull=True,
        )
        .order_by('created_at')
        .values('created_at')[:1]
    )

    queryset = queryset.annotate(opened_at=Subquery(opened_at_subquery))

    if opened_at__gte is not None:
        queryset = queryset.filter(opened_at__gte=opened_at__gte)

    return queryset


def _conflict_search_predicate(search: str) -> Q:
    compared = MemoryConflict.objects.filter(
        candidate_id=OuterRef('pk'),
        resolved_transition__isnull=True,
        memory_version__body__icontains=search,
    )

    return Q(title__icontains=search) | Q(body__icontains=search) | Exists(compared)


def open_conflicts_for_candidates(
    organization: Organization,
    candidate_ids: Iterable[uuid.UUID],
) -> dict[uuid.UUID, list[MemoryConflict]]:
    conflicts = (
        MemoryConflict.objects.filter(
            organization=organization,
            candidate_id__in=list(candidate_ids),
            resolved_transition__isnull=True,
        )
        .select_related('memory', 'memory_version', 'opened_transition')
        .order_by('candidate_id', 'id')
    )

    grouped: dict[uuid.UUID, list[MemoryConflict]] = defaultdict(list)

    for conflict in conflicts:
        grouped[conflict.candidate_id].append(conflict)

    return grouped


def conflict_decision_context(
    conflicts: list[MemoryConflict],
) -> tuple[dict[uuid.UUID, CurationDecision], CurationDecision | None, dict[str, Observation]]:
    conflict_ids = [conflict.id for conflict in conflicts]
    decisions_by_conflict: dict[uuid.UUID, CurationDecision] = {}
    for decision in (
        CurationDecision.objects.filter(conflict_id__in=conflict_ids)
        .select_related('provider_call_record', 'policy')
        .order_by('created_at', 'id')
    ):
        decisions_by_conflict.setdefault(decision.conflict_id, decision)

    primary: CurationDecision | None = None
    for conflict in sorted(conflicts, key=lambda item: str(item.id)):
        if conflict.id in decisions_by_conflict:
            primary = decisions_by_conflict[conflict.id]
            break

    observation_ids: set[str] = set()
    for decision in decisions_by_conflict.values():
        membership = decision.evidence_membership or {}
        for entry in membership.get('candidate', []):
            observation_ids.add(entry.get('observation_id'))
        for target in membership.get('targets', []):
            for entry in target.get('sources', []):
                observation_ids.add(entry.get('observation_id'))

    observations = {
        str(observation.id): observation
        for observation in Observation.objects.filter(id__in=[value for value in observation_ids if value])
    }

    return decisions_by_conflict, primary, observations


def get_conflict_candidate_or_404(
    organization: Organization,
    candidate_id: uuid.UUID,
    scope: EffectiveScope,
) -> MemoryCandidate:
    unresolved = MemoryConflict.objects.filter(
        candidate_id=OuterRef('pk'),
        resolved_transition__isnull=True,
    )

    candidate = (
        _scope_conflict_candidates(
            MemoryCandidate.objects.filter(
                organization=organization,
                id=candidate_id,
            ),
            scope,
        )
        .filter(Exists(unresolved))
        .first()
    )

    if candidate is None:
        raise MemoryReviewError('not_found', 'conflict not found', status=404)

    return candidate


def conflict_set_etag(candidate: MemoryCandidate) -> str:
    open_conflicts = list(
        MemoryConflict.objects.filter(
            candidate=candidate,
            resolved_transition__isnull=True,
        )
        .select_related('memory')
        .order_by('id'),
    )

    parts = [
        str(candidate.id),
        ','.join(sorted(str(conflict.id) for conflict in open_conflicts)),
        ','.join(sorted(str(conflict.opened_transition_id) for conflict in open_conflicts)),
        ','.join(sorted(conflict.evidence_hash for conflict in open_conflicts)),
        ','.join(sorted(build_memory_fence(conflict.memory).state_hash for conflict in open_conflicts)),
    ]

    digest = hashlib.sha256('|'.join(parts).encode()).hexdigest()

    return f'"{digest}"'


@transaction.atomic
def resolve_candidate_conflicts(
    *,
    organization: Organization,
    actor_identity: Identity,
    candidate: MemoryCandidate,
    action: str,
    reason: str,
    target_memory_id: uuid.UUID | None = None,
    merged_title: str | None = None,
    merged_body: str | None = None,
    expected_etag: str | None = None,
) -> dict[str, Any]:
    candidate = (
        MemoryCandidate.objects.select_for_update()
        .filter(organization=organization, id=candidate.id)
        .first()
    )

    if candidate is None:
        raise MemoryReviewError('not_found', 'conflict not found', status=404)

    title_limit = Memory._meta.get_field('title').max_length
    if merged_title is not None and len(merged_title) > title_limit:
        raise MemoryReviewError('invalid_title', 'merged_title exceeds the maximum length', status=400)

    if expected_etag is not None and expected_etag != conflict_set_etag(candidate):
        raise MemoryReviewError('stale_conflict_set', 'conflict set has changed', status=412)

    open_conflicts = list(
        MemoryConflict.objects.filter(
            candidate=candidate,
            resolved_transition__isnull=True,
        )
        .select_related('memory')
        .order_by('id'),
    )

    if not open_conflicts:
        raise MemoryReviewError('not_found', 'conflict not found', status=404)

    conflict_ids = tuple(conflict.id for conflict in open_conflicts)
    conflict_memory_fences = tuple(build_memory_fence(conflict.memory) for conflict in open_conflicts)

    selected_memory_fence = _selected_conflict_memory_fence(open_conflicts, action, target_memory_id)

    _entries, manifest_hash = candidate_evidence_manifest(candidate)
    candidate_fence = CandidateFence(
        candidate_id=candidate.id,
        candidate_content_hash=candidate.content_hash,
        evidence_manifest_hash=manifest_hash,
    )

    operation_id = f'console-conflict-resolve:{candidate.id}:{uuid.uuid4()}:v1'

    title = merged_title if action != 'reject_candidate' else None
    body = merged_body if action != 'reject_candidate' else None

    result = _execute_conflict_resolution(
        lambda: ResolveMemoryConflict().execute(
            ResolveMemoryConflictInput(
                request=TransitionRequest(
                    scope=TransitionScope(
                        organization_id=organization.id,
                        project_id=candidate.project_id,
                        team_id=candidate.team_id,
                    ),
                    idempotency_key=operation_id,
                    actor_type='user',
                    actor_id=str(actor_identity.id),
                    capability='memories:admin',
                    request_id=operation_id,
                    correlation_id=operation_id,
                    reason=reason,
                    origin='console',
                ),
                candidate_fence=candidate_fence,
                conflict_ids=conflict_ids,
                conflict_memory_fences=conflict_memory_fences,
                resolution=action,
                selected_memory_fence=selected_memory_fence,
                title=title,
                body=body,
            )
        )
    )

    _record_review_example(
        organization=organization,
        actor_identity=actor_identity,
        item=candidate,
        action=action,
        reason=reason,
        curator_context={'conflict_ids': [str(conflict_id) for conflict_id in conflict_ids]},
    )

    rejected = action == 'reject_candidate'

    return {
        'id': str(candidate.id),
        'candidate_id': str(candidate.id),
        'state': 'resolved',
        'action': action,
        'conflict_ids': [str(conflict_id) for conflict_id in conflict_ids],
        'transition_id': str(result.transition.id),
        'memory_id': None if rejected else str(result.memory.id),
        'version_id': None if rejected else str(result.memory_version.id),
    }


def _selected_conflict_memory_fence(
    open_conflicts: list[MemoryConflict],
    action: str,
    target_memory_id: uuid.UUID | None,
) -> Any:
    if action in _CONFLICT_TARGET_ACTIONS:
        if target_memory_id is None:
            raise MemoryReviewError('invalid_target', 'target_memory_id is required for this action')

        selected = next(
            (conflict for conflict in open_conflicts if conflict.memory_id == target_memory_id),
            None,
        )

        if selected is None:
            raise MemoryReviewError('invalid_target', 'target memory is not in the conflict set')

        return build_memory_fence(selected.memory)

    if target_memory_id is not None:
        raise MemoryReviewError('invalid_target', 'target memory is not allowed for this action')

    return None


def _execute_conflict_resolution(call: Any) -> Any:
    try:
        return call()

    except MemoryTransitionError as error:
        if error.retryable or error.code == 'stale_decision':
            raise MemoryReviewError('stale_conflict_set', str(error), status=412) from error

        if error.code == 'scope':
            raise MemoryReviewError('not_found', str(error), status=404) from error

        raise MemoryReviewError('invalid_state', str(error), status=400) from error
