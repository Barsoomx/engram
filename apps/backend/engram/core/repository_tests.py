from __future__ import annotations

import pytest

from engram.core.models import Organization, Project
from engram.core.repository import (
    RepositoryUrlRequiredError,
    canonicalize_repository_url,
    resolve_or_create_project,
)

CANONICAL = 'git@github.com:barsoomx/engram.git'


@pytest.mark.parametrize(
    'raw',
    [
        'https://github.com/Barsoomx/Engram.git',
        'https://github.com/barsoomx/engram',
        'https://github.com/barsoomx/engram/',
        'http://github.com/barsoomx/engram.git',
        'git@github.com:Barsoomx/Engram.git',
        'git@github.com:barsoomx/engram',
        'ssh://git@github.com/barsoomx/engram.git',
        'https://user:token@github.com/barsoomx/engram.git',
    ],
)
def test_canonicalize_collapses_every_form_to_one(raw: str) -> None:
    assert canonicalize_repository_url(raw) == CANONICAL


@pytest.mark.parametrize('raw', ['', '   ', 'not-a-url', 'owner/repo'])
def test_canonicalize_returns_empty_for_unroutable(raw: str) -> None:
    assert canonicalize_repository_url(raw) == ''


@pytest.fixture
def f_org() -> Organization:
    return Organization.objects.create(name='Acme', slug='acme')


@pytest.mark.django_db
def test_resolve_matches_existing_project_across_url_formats(f_org: Organization) -> None:
    project = Project.objects.create(
        organization=f_org,
        name='barsoomx/engram',
        slug='barsoomx-engram',
        repository_url=CANONICAL,
    )

    resolved = resolve_or_create_project(
        organization=f_org,
        repository_url='https://github.com/Barsoomx/Engram.git',
    )

    assert resolved.id == project.id
    assert Project.objects.filter(organization=f_org).count() == 1


@pytest.mark.django_db
def test_resolve_matches_legacy_noncanonical_stored_url(f_org: Organization) -> None:
    project = Project.objects.create(
        organization=f_org,
        name='legacy',
        slug='legacy',
        repository_url='https://github.com/barsoomx/engram',
    )

    resolved = resolve_or_create_project(
        organization=f_org,
        repository_url='git@github.com:barsoomx/engram.git',
    )

    assert resolved.id == project.id


@pytest.mark.django_db
def test_resolve_auto_creates_project_with_canonical_url(f_org: Organization) -> None:
    resolved = resolve_or_create_project(
        organization=f_org,
        repository_url='https://gitlab.com/team/service.git',
        repository_root='/home/dev/service',
    )

    assert resolved.repository_url == 'git@gitlab.com:team/service.git'
    assert resolved.slug == 'team-service'
    assert resolved.repository_root == '/home/dev/service'
    assert Project.objects.filter(organization=f_org, id=resolved.id).exists()


@pytest.mark.django_db
def test_resolve_is_organization_scoped(f_org: Organization) -> None:
    other = Organization.objects.create(name='Globex', slug='globex')
    Project.objects.create(
        organization=other,
        name='shared',
        slug='shared',
        repository_url=CANONICAL,
    )

    resolved = resolve_or_create_project(organization=f_org, repository_url=CANONICAL)

    assert resolved.organization_id == f_org.id
    assert Project.objects.filter(organization=f_org).count() == 1
    assert Project.objects.filter(organization=other).count() == 1


@pytest.mark.django_db
def test_resolve_disambiguates_slug_collision(f_org: Organization) -> None:
    Project.objects.create(
        organization=f_org,
        name='team/service',
        slug='team-service',
        repository_url='git@github.com:team/service.git',
    )

    resolved = resolve_or_create_project(
        organization=f_org,
        repository_url='git@gitlab.com:team/service.git',
    )

    assert resolved.slug == 'team-service-2'


@pytest.mark.django_db
def test_resolve_rejects_unroutable_url(f_org: Organization) -> None:
    with pytest.raises(RepositoryUrlRequiredError):
        resolve_or_create_project(organization=f_org, repository_url='')
