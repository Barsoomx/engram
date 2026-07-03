from __future__ import annotations

import uuid

import pytest

from engram.access.access_scope_tests import (
    RAW_KEY,
    create_owner,
    create_project_scope,
    create_scoped_api_key,
)
from engram.access.services import AccessDeniedError, EffectiveScope, ResolveApiKeyScope
from engram.core.models import AuditEvent, AuditResult, Organization, Project
from engram.core.repository import (
    ProjectNotFoundError,
    RepositoryUrlRequiredError,
    canonicalize_repository_url,
    resolve_or_create_project,
    resolve_project_for_scope,
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


def _scope(
    organization: Organization,
    *,
    project_ids: tuple[uuid.UUID, ...] = (),
    capabilities: tuple[str, ...] = (),
    actor_type: str = 'api_key',
    project_bound: bool = False,
) -> EffectiveScope:
    return EffectiveScope(
        organization_id=organization.id,
        identity_id=uuid.uuid4(),
        api_key_id=uuid.uuid4(),
        project_ids=project_ids,
        team_ids=(),
        capabilities=capabilities,
        actor_type=actor_type,
        actor_id='actor-1',
        project_bound=project_bound,
    )


@pytest.mark.django_db
def test_resolve_project_for_scope_returns_project_by_id_within_scope(f_org: Organization) -> None:
    project = Project.objects.create(organization=f_org, name='p', slug='p', repository_url=CANONICAL)
    scope = _scope(f_org, project_ids=(project.id,))

    resolved = resolve_project_for_scope(scope=scope, project_id=project.id, repository_url='')

    assert resolved.id == project.id


@pytest.mark.django_db
def test_resolve_project_for_scope_matches_by_canonical_repository_url(f_org: Organization) -> None:
    project = Project.objects.create(organization=f_org, name='p', slug='p', repository_url=CANONICAL)
    scope = _scope(f_org, project_ids=(project.id,))

    resolved = resolve_project_for_scope(
        scope=scope,
        project_id=None,
        repository_url='https://github.com/Barsoomx/Engram.git',
    )

    assert resolved.id == project.id


@pytest.mark.django_db
def test_resolve_project_for_scope_raises_not_found_for_unknown_repository(f_org: Organization) -> None:
    scope = _scope(f_org, capabilities=('projects:agent',))

    with pytest.raises(ProjectNotFoundError) as excinfo:
        resolve_project_for_scope(
            scope=scope,
            project_id=None,
            repository_url='https://github.com/other/unknown.git',
        )

    assert excinfo.value.error_code == 'project_not_found'
    assert excinfo.value.status_code == 404


@pytest.mark.django_db
def test_resolve_project_for_scope_requires_project_id_or_repository_url(f_org: Organization) -> None:
    scope = _scope(f_org)

    with pytest.raises(RepositoryUrlRequiredError) as excinfo:
        resolve_project_for_scope(scope=scope, project_id=None, repository_url='')

    assert excinfo.value.error_code == 'project_or_repository_required'


@pytest.mark.django_db
def test_resolve_project_for_scope_denies_project_outside_scope_and_audits(f_org: Organization) -> None:
    project = Project.objects.create(organization=f_org, name='p', slug='p', repository_url=CANONICAL)
    other_project = Project.objects.create(organization=f_org, name='other', slug='other')
    scope = _scope(f_org, project_ids=(other_project.id,))

    with pytest.raises(AccessDeniedError) as excinfo:
        resolve_project_for_scope(scope=scope, project_id=None, repository_url=CANONICAL)

    assert excinfo.value.code == 'project_scope_denied'
    audit = AuditEvent.objects.get(project_id=project.id, event_type='AccessScopeResolved')
    assert audit.result == AuditResult.DENIED
    assert audit.metadata['resolved_project_id'] == str(project.id)


@pytest.mark.django_db
def test_resolve_project_for_scope_allows_unbound_agent_key_via_capability_branch(f_org: Organization) -> None:
    project = Project.objects.create(organization=f_org, name='p', slug='p', repository_url=CANONICAL)
    scope = _scope(f_org, project_ids=(), capabilities=('projects:agent',))

    resolved = resolve_project_for_scope(scope=scope, project_id=None, repository_url=CANONICAL)

    assert resolved.id == project.id


@pytest.mark.django_db
def test_resolve_project_for_scope_allow_create_admits_just_created_project_via_capability(
    f_org: Organization,
) -> None:
    scope = _scope(f_org, project_ids=(), capabilities=('projects:agent',))

    resolved = resolve_project_for_scope(
        scope=scope,
        project_id=None,
        repository_url='https://gitlab.com/team/newly-created.git',
        allow_create=True,
    )

    assert resolved.organization_id == f_org.id
    assert Project.objects.filter(organization=f_org, id=resolved.id).exists()


@pytest.mark.django_db
def test_resolve_project_for_scope_session_scope_never_uses_capability_branch(f_org: Organization) -> None:
    project = Project.objects.create(organization=f_org, name='p', slug='p', repository_url=CANONICAL)
    scope = _scope(f_org, project_ids=(), capabilities=('projects:agent',), actor_type='user')

    with pytest.raises(AccessDeniedError) as excinfo:
        resolve_project_for_scope(scope=scope, project_id=None, repository_url=CANONICAL)

    assert excinfo.value.code == 'project_scope_denied'
    assert project.id not in scope.project_ids


@pytest.mark.django_db
def test_resolve_project_for_scope_cross_organization_url_is_not_found(f_org: Organization) -> None:
    other_org = Organization.objects.create(name='Globex', slug='globex')
    Project.objects.create(organization=other_org, name='shared', slug='shared', repository_url=CANONICAL)
    scope = _scope(f_org, capabilities=('projects:agent',))

    with pytest.raises(ProjectNotFoundError):
        resolve_project_for_scope(scope=scope, project_id=None, repository_url=CANONICAL)

    assert not Project.objects.filter(organization=f_org).exists()


@pytest.mark.django_db
def test_resolve_project_for_scope_allow_create_never_reuses_foreign_org_project(f_org: Organization) -> None:
    other_org = Organization.objects.create(name='Globex', slug='globex')
    foreign = Project.objects.create(organization=other_org, name='shared', slug='shared', repository_url=CANONICAL)
    scope = _scope(f_org, capabilities=('projects:agent',))

    resolved = resolve_project_for_scope(
        scope=scope,
        project_id=None,
        repository_url=CANONICAL,
        allow_create=True,
    )

    assert resolved.id != foreign.id
    assert resolved.organization_id == f_org.id


@pytest.mark.django_db
def test_resolve_project_for_scope_project_id_from_other_organization_is_not_found(f_org: Organization) -> None:
    other_org = Organization.objects.create(name='Globex', slug='globex')
    foreign = Project.objects.create(organization=other_org, name='shared', slug='shared')
    scope = _scope(f_org, project_ids=(foreign.id,))

    with pytest.raises(ProjectNotFoundError):
        resolve_project_for_scope(scope=scope, project_id=foreign.id, repository_url='')


@pytest.mark.django_db
def test_resolve_project_for_scope_bound_key_with_agent_capability_denied_for_foreign_project() -> None:
    organization, team, project = create_project_scope()
    foreign_project = Project.objects.create(
        organization=organization,
        name='foreign',
        slug='foreign',
        repository_url=CANONICAL,
    )
    owner = create_owner(organization, role_code='organization_admin')
    create_scoped_api_key(
        organization,
        team,
        project,
        owner,
        capabilities=('observations:write', 'projects:agent'),
    )

    scope = ResolveApiKeyScope().execute(
        raw_key=RAW_KEY,
        required_capability='observations:write',
        requested_project_id=project.id,
        request_id='request-bound-agent-foreign-1',
    )

    assert scope.project_bound is True

    with pytest.raises(AccessDeniedError) as excinfo:
        resolve_project_for_scope(scope=scope, project_id=None, repository_url=CANONICAL)

    assert excinfo.value.code == 'project_scope_denied'
    assert foreign_project.id != project.id


@pytest.mark.django_db
def test_resolve_project_for_scope_bound_key_with_agent_capability_allowed_for_own_project() -> None:
    organization, team, project = create_project_scope()
    project.repository_url = CANONICAL
    project.save(update_fields=['repository_url'])
    owner = create_owner(organization, role_code='organization_admin')
    create_scoped_api_key(
        organization,
        team,
        project,
        owner,
        capabilities=('observations:write', 'projects:agent'),
    )

    scope = ResolveApiKeyScope().execute(
        raw_key=RAW_KEY,
        required_capability='observations:write',
        requested_project_id=project.id,
        request_id='request-bound-agent-own-1',
    )

    assert scope.project_bound is True

    resolved = resolve_project_for_scope(scope=scope, project_id=None, repository_url=CANONICAL)

    assert resolved.id == project.id


@pytest.mark.django_db
def test_resolve_project_for_scope_bound_key_never_creates_a_project_it_cannot_pass_the_guard_for() -> None:
    organization, team, project = create_project_scope()
    owner = create_owner(organization, role_code='organization_admin')
    create_scoped_api_key(
        organization,
        team,
        project,
        owner,
        capabilities=('observations:write', 'projects:agent'),
    )

    scope = ResolveApiKeyScope().execute(
        raw_key=RAW_KEY,
        required_capability='observations:write',
        requested_project_id=project.id,
        request_id='request-bound-agent-nonexistent-1',
    )

    assert scope.project_bound is True
    project_count_before = Project.objects.filter(organization=organization).count()

    with pytest.raises(ProjectNotFoundError):
        resolve_project_for_scope(
            scope=scope,
            project_id=None,
            repository_url='https://gitlab.com/team/never-created.git',
            allow_create=True,
        )

    assert Project.objects.filter(organization=organization).count() == project_count_before
