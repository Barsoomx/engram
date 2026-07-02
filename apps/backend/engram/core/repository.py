from __future__ import annotations

import re
from urllib.parse import urlparse

from django.db import transaction

from engram.core.models import Organization, Project

_SCP_LIKE = re.compile(r'^[A-Za-z0-9._-]+@([^:/]+):(.+)$')


class RepositoryUrlRequiredError(Exception):
    pass


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
