from __future__ import annotations

import re
import uuid
from urllib.parse import urlparse

from django.db import transaction

from engram.access.services import AccessDeniedError, EffectiveScope
from engram.core.domain.usecases.errors import DomainError
from engram.core.models import AuditEvent, AuditResult, Organization, Project

_SCP_LIKE = re.compile(r'^[A-Za-z0-9._-]+@([^:/]+):(.+)$')


class RepositoryUrlRequiredError(DomainError):
    default_error_code = 'project_or_repository_required'


class ProjectNotFoundError(DomainError):
    default_error_code = 'project_not_found'
    default_status_code = 404


def canonicalize_repository_url(url: str) -> str:
    raw = (url or '').strip()
    if not raw:
        return ''

    host = ''
    path = ''
    scp = _SCP_LIKE.match(raw)
    if scp and '://' not in raw:
        host = scp.group(1)
        path = scp.group(2)
    else:
        parsed = urlparse(raw)
        if parsed.scheme and parsed.hostname:
            host = parsed.hostname
            path = parsed.path
        else:
            head, _, tail = raw.partition('/')
            if tail and '.' in head:
                host = head
                path = tail

    host = host.strip().strip('/').lower()
    path = path.strip().strip('/').lower()
    if path.endswith('.git'):
        path = path[:-4]
    if not host or not path:
        return ''

    return f'git@{host}:{path}.git'


def resolve_or_create_project(
    *,
    organization: Organization,
    repository_url: str,
    repository_root: str = '',
) -> Project:
    canonical = canonicalize_repository_url(repository_url)
    if not canonical:
        raise RepositoryUrlRequiredError('a resolvable repository_url is required to route memory')

    existing = _match_project(organization, canonical)
    if existing is not None:
        return existing

    with transaction.atomic():
        existing = _match_project(organization, canonical)
        if existing is not None:
            return existing

        return Project.objects.create(
            organization=organization,
            name=_repository_path(canonical),
            slug=_unique_slug(organization, _slug_base(canonical)),
            repository_url=canonical,
            repository_root=repository_root,
            default_branch='',
        )


def resolve_project_for_scope(
    *,
    scope: EffectiveScope,
    project_id: uuid.UUID | None,
    repository_url: str,
    allow_create: bool = False,
    repository_root: str = '',
    request_id: str = '',
    correlation_id: str = '',
) -> Project:
    if project_id is not None:
        try:
            project = Project.objects.get(organization_id=scope.organization_id, id=project_id)
        except Project.DoesNotExist:
            raise ProjectNotFoundError('project was not found in the requesting organization') from None

    else:
        canonical = canonicalize_repository_url(repository_url)
        if not canonical:
            raise RepositoryUrlRequiredError('a resolvable repository_url is required to route memory')

        organization = Organization.objects.get(id=scope.organization_id)
        matched = _match_project(organization, canonical)
        if matched is not None:
            project = matched
        elif allow_create and _unbound_agent_capability(scope):
            project = resolve_or_create_project(
                organization=organization,
                repository_url=canonical,
                repository_root=repository_root,
            )
        else:
            raise ProjectNotFoundError('no project matches this repository in the requesting organization')

    _authorize_resolved_project(scope, project, request_id=request_id, correlation_id=correlation_id)

    return project


def _unbound_agent_capability(scope: EffectiveScope) -> bool:
    return scope.actor_type == 'api_key' and not scope.project_bound and 'projects:agent' in scope.capabilities


def _authorize_resolved_project(
    scope: EffectiveScope,
    project: Project,
    *,
    request_id: str = '',
    correlation_id: str = '',
) -> None:
    allowed = project.id in scope.project_ids or _unbound_agent_capability(scope)
    if allowed:
        return

    AuditEvent.objects.create(
        organization_id=scope.organization_id,
        project_id=project.id,
        event_type='AccessScopeResolved',
        actor_type=scope.actor_type,
        actor_id=scope.actor_id,
        target_type='project',
        target_id=str(project.id),
        result=AuditResult.DENIED,
        request_id=request_id,
        correlation_id=correlation_id,
        metadata={
            'reason': 'project_scope_denied',
            'resolved_project_id': str(project.id),
            'api_key_id': str(scope.api_key_id),
            'effective_capabilities': list(scope.capabilities),
        },
    )
    raise AccessDeniedError('project_scope_denied', 'scope cannot access the resolved project')


def _match_project(organization: Organization, canonical: str) -> Project | None:
    exact = Project.objects.filter(organization=organization, repository_url=canonical).first()
    if exact is not None:
        return exact

    for project in Project.objects.filter(organization=organization).exclude(repository_url=''):
        if canonicalize_repository_url(project.repository_url) == canonical:
            return project

    return None


def _repository_path(canonical: str) -> str:
    path = canonical.split(':', 1)[1]
    if path.endswith('.git'):
        path = path[:-4]

    return path


def _slug_base(canonical: str) -> str:
    slug = re.sub(r'[^a-z0-9]+', '-', _repository_path(canonical)).strip('-')

    return slug[:110] or 'project'


def _unique_slug(organization: Organization, base: str) -> str:
    slug = base
    suffix = 2
    while Project.objects.filter(organization=organization, slug=slug).exists():
        slug = f'{base[:106]}-{suffix}'
        suffix += 1

    return slug
