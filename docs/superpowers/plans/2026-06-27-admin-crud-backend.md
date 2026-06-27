# Phase A — Admin CRUD Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a capability-gated, tenant-scoped REST CRUD surface for admin entities (organizations, teams, projects, members, roles, API keys) under the existing username/password session auth, with an immutable audit trail.

**Architecture:** New Django app `engram/console/` with DRF `ModelViewSet` + `DefaultRouter` mounted at `/v1/admin/`. Authorization via a reusable `RequireCapability` permission class (wildcard-aware). Active org resolved from `X-Engram-Organization` header (validated against the user's memberships) and stashed on `request`. Every mutating action reuses `AuditEvent`. Read/write serializer split. API keys issue plaintext once, never on read.

**Tech Stack:** Django, DRF, `django-filter`, `drf-spectacular` (install), pytest, factory-boy. All Python runs inside the `api` Docker container.

**User decisions (already made):**
- Auth = existing DRF TokenAuthentication (username/password, verified). No next-auth/JWT.
- Admin auth (session token) is separate from agent auth (API key).
- Org switcher transport = `X-Engram-Organization` header.
- Organizations: read + update only in Phase A (no create/delete; tenant provisioning is Phase C).
- Roles: read + assign only (built-in presets); custom roles are "Later".
- References: *****-backend (backend patterns), *****-admin (frontend patterns). Local Engram docs may be unreliable.

**Reference design:** `docs/superpowers/specs/2026-06-26-admin-crud-backend-design.md`

---

## File Structure

New app `engram/console/`:

- `apps/backend/engram/console/__init__.py`, `apps.py` — app scaffold.
- `apps/backend/engram/console/permissions.py` — `RequireCapability` (wildcard-aware), `IsAuthenticated` reuse.
- `apps/backend/engram/console/org_resolution.py` — `resolve_active_organization(request)` + `ActiveOrganizationPermission`.
- `apps/backend/engram/console/services.py` — domain ops (`create_team`, `archive_team`, `issue_api_key`, `revoke_api_key`, `invite_member`, etc.) that own constraints + audit.
- `apps/backend/engram/console/serializers/` — one file per resource (organizations, teams, projects, members, roles, api_keys).
- `apps/backend/engram/console/views/` — one ViewSet per file (rule: each view in its own file).
- `apps/backend/engram/console/urls.py` — `DefaultRouter` wiring.
- `apps/backend/engram/console/filters.py` — `django-filter` FilterSets.
- `apps/backend/engram/console/*_tests.py` — tests next to each module (rule #5).

Other:
- `apps/backend/engram/access/migrations/00XX_admin_capabilities.py` — data migration: capability codes + role seeds.
- `apps/backend/settings/settings.py` — add `engram.console` to INSTALLED_APPS; REST_FRAMEWORK defaults; DRF router mount.
- `apps/backend/settings/urls.py` — include `v1/admin/`.
- `apps/backend/pyproject.toml` — add `django-filter`, `drf-spectacular`.

---

## Task 0: Foundation — app scaffold, deps, permission, org resolution, router mount

**Goal:** Stand up the `engram.console` app, add deps, implement the `RequireCapability` permission + active-org resolution, and mount an empty `/v1/admin/` router so subsequent tasks add resources.

**Files:**
- Create: `apps/backend/engram/console/__init__.py`, `apps.py`
- Create: `apps/backend/engram/console/permissions.py`
- Create: `apps/backend/engram/console/org_resolution.py`
- Create: `apps/backend/engram/console/urls.py`
- Modify: `apps/backend/settings/settings.py` (INSTALLED_APPS, REST_FRAMEWORK)
- Modify: `apps/backend/settings/urls.py` (include `/v1/admin/`)
- Modify: `apps/backend/pyproject.toml` (deps)
- Test: `apps/backend/engram/console/permissions_tests.py`, `apps/backend/engram/console/org_resolution_tests.py`

**Acceptance Criteria:**
- [ ] `engram.console` is an installed app; `GET /v1/admin/` returns 200 with an empty or router-root response (not 404).
- [ ] `RequireCapability('api_keys:read')` returns True when the resolved scope contains `api_keys:*`; False otherwise.
- [ ] `resolve_active_organization` returns the org matching `X-Engram-Organization` when the user is an active member; raises when not a member; falls back to single membership when header absent.
- [ ] `django-filter` and `drf-spectacular` installed and importable in the container.

**Verify:** `docker compose -f deploy/compose/docker-compose.yml exec -T api pytest engram/console/permissions_tests.py engram/console/org_resolution_tests.py -v` → all PASS.

**Steps:**

- [ ] **Step 1: Write failing permission test**

```python
from unittest.mock import MagicMock

from engram.console.permissions import RequireCapability


def _request_with_caps(caps):
    request = MagicMock()
    request.effective_scope = MagicMock(caps=caps)
    return request


def test_require_capability_grants_wildcard():
    permission = RequireCapability('api_keys:read')
    assert permission.has_object_permission_override(_request_with_caps({'api_keys:*'})) is True


def test_require_capability_grants_exact():
    permission = RequireCapability('teams:admin')
    assert permission.has_object_permission_override(_request_with_caps({'teams:admin'})) is True


def test_require_capability_denies_missing():
    permission = RequireCapability('members:admin')
    assert permission.has_object_permission_override(_request_with_caps({'members:read'})) is False
```

- [ ] **Step 2: Run test → FAIL (module missing).**

Run: `docker compose -f deploy/compose/docker-compose.yml exec -T api pytest engram/console/permissions_tests.py -v`
Expected: collection error / ImportError.

- [ ] **Step 3: Implement permissions.py**

```python
from __future__ import annotations

from rest_framework.permissions import BasePermission, IsAuthenticated


class RequireCapability(BasePermission):
    def __init__(self, code: str) -> None:
        self.code = code

    def has_object_permission_override(self, request) -> bool:
        scope = getattr(request, 'effective_scope', None)
        if scope is None:
            return False

        granted = set(scope.capabilities)
        group = self.code.split(':')[0]

        return self.code in granted or f'{group}:*' in granted

    def has_permission(self, request, view) -> bool:
        return self.has_object_permission_override(request)
```

- [ ] **Step 4: Write failing org-resolution test**

```python
import pytest

from engram.console.org_resolution import (
    OrganizationNotMemberError,
    OrganizationRequiredError,
    resolve_active_organization,
)


def test_resolve_by_header_when_member(m_user_member, m_org):
    request = type('R', (), {'META': {'HTTP_X_ENGRAM_ORGANIZATION': str(m_org.id)}, 'user': m_user_member})()
    assert resolve_active_organization(request) == m_org


def test_resolve_raises_when_not_member(m_user_other_org, m_org):
    request = type('R', (), {'META': {'HTTP_X_ENGRAM_ORGANIZATION': str(m_org.id)}, 'user': m_user_other_org})()
    with pytest.raises(OrganizationNotMemberError):
        resolve_active_organization(request)


def test_resolve_falls_back_to_single_membership(m_user_member, m_org):
    request = type('R', (), {'META': {}, 'user': m_user_member})()
    assert resolve_active_organization(request) == m_org


def test_resolve_requires_header_when_multiple_memberships(m_user_two_orgs):
    request = type('R', (), {'META': {}, 'user': m_user_two_orgs})()
    with pytest.raises(OrganizationRequiredError):
        resolve_active_organization(request)
```

- [ ] **Step 5: Run test → FAIL.** Same container command on `org_resolution_tests.py`.

- [ ] **Step 6: Implement org_resolution.py**

```python
from __future__ import annotations

import uuid

from django.contrib.auth.models import User

from engram.access.models import OrganizationMembership
from engram.core.models import Organization


class OrganizationRequiredError(Exception):
    pass


class OrganizationNotMemberError(Exception):
    pass


def resolve_active_organization(request) -> Organization:
    user: User = request.user
    header = request.META.get('HTTP_X_ENGRAM_ORGANIZATION', '').strip()

    if header:
        try:
            org = Organization.objects.get(slug=header)
        except Organization.DoesNotExist:
            try:
                org = Organization.objects.get(id=uuid.UUID(header))
            except (ValueError, Organization.DoesNotExist):
                raise OrganizationNotMemberError('organization not found')

        if not OrganizationMembership.objects.filter(organization=org, identity__django_user=user, active=True).exists():
            raise OrganizationNotMemberError('not a member of organization')

        return org

    memberships = OrganizationMembership.objects.filter(
        identity__django_user=user,
        active=True,
    ).select_related('organization')

    if memberships.count() == 1:
        return memberships.first().organization

    raise OrganizationRequiredError('X-Engram-Organization header required')
```

Note: this task assumes `Identity` has a reverse link to `User` (`django_user`). Task 1's seed and the existing `LoginUser` establish that link via `external_id='django-user:<username>'`. If the reverse accessor name differs, use `Identity.objects.get(external_id=f'django-user:{user.get_username()}', organization=...)`. Confirm against `access/auth_services.py:external_id_for_user` during implementation and adjust the lookup helper to a single `resolve_user_identity(user, org)` in `org_resolution.py` reused everywhere.

- [ ] **Step 7: Scaffold app + urls + settings.**

`engram/console/__init__.py`: empty.
`engram/console/apps.py`:
```python
from django.apps import AppConfig


class ConsoleConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'engram.console'
```

`engram/console/urls.py`:
```python
from rest_framework.routers import DefaultRouter

router = DefaultRouter()
urlpatterns = router.urls
```

`settings/settings.py` INSTALLED_APPS: add `'engram.console'`, `'rest_framework'` (if missing), `'django_filters'`, `'drf_spectacular'`. REST_FRAMEWORK dict: add `'DEFAULT_FILTER_BACKENDS': ['django_filters.rest_framework.DjangoFilterBackend']`, `'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination'`, `'PAGE_SIZE': 20`.

`settings/urls.py`: add `path('v1/admin/', include('engram.console.urls'))`.

- [ ] **Step 8: Add deps.** `pyproject.toml`: `django-filter = "^25.1"`, `drf-spectacular = "^0.28.0"`. Regenerate lockfile inside the container: `docker compose -f deploy/compose/docker-compose.yml exec -T api poetry lock && poetry install`. (Confirm the project uses poetry in the backend; if pip-based, `pip install django-filter drf-spectacular` and pin in requirements.)

- [ ] **Step 9: Run all foundation tests → PASS.**

- [ ] **Step 10: Commit**

```bash
git add apps/backend/engram/console/ apps/backend/settings/ apps/backend/pyproject.toml
git commit -m "feat: add console app foundation with capability permission and org resolution"
```

---

## Task 1: Capabilities & roles seed (data migration)

**Goal:** Seed the new capability codes and attach them to built-in roles so admin endpoints can be authorized.

**Files:**
- Create: `apps/backend/engram/access/migrations/0007_admin_capabilities.py` (use next free number)
- Test: `apps/backend/engram/access/admin_capabilities_seed_tests.py`

**Acceptance Criteria:**
- [ ] After migration, `organization_owner` role has every `*:admin` and `*:read` capability in the admin set.
- [ ] `organization_admin` has `teams:admin`, `projects:admin`, `members:admin`, `api_keys:*`, `roles:read`, and all `*:read`.
- [ ] `developer` has `projects:read`, `teams:read`, `api_keys:read`; `auditor` has all `*:read` + `audit:read`, no writes.
- [ ] Migration is idempotent (re-run via `migrate` does not duplicate rows; uses `get_or_create`).
- [ ] Wildcard capability rows exist (e.g. `api_keys:*`, `members:*`).

**Verify:** `docker compose -f deploy/compose/docker-compose.yml exec -T api pytest engram/access/admin_capabilities_seed_tests.py -v` → PASS; then `docker compose ... exec -T api python manage.py migrate engram` twice without error.

**Steps:**

- [ ] **Step 1: Write failing test**

```python
import pytest
from django.core.management import call_command

from engram.access.models import Capability, Role, RoleCapability


@pytest.mark.django_db
def test_owner_has_all_admin_capabilities():
    role = Role.objects.get(code='organization_owner')
    caps = set(RoleCapability.objects.filter(role=role).values_list('capability__code', flat=True))
    for code in ['organizations:read', 'organizations:admin', 'teams:admin', 'projects:admin', 'members:admin', 'api_keys:issue', 'api_keys:revoke', 'roles:read', 'api_keys:read']:
        assert code in caps, f'missing {code}'


@pytest.mark.django_db
def test_auditor_has_no_write_capabilities():
    role = Role.objects.get(code='auditor')
    caps = set(RoleCapability.objects.filter(role=role).values_list('capability__code', flat=True))
    assert 'audit:read' in caps
    assert not any(c.endswith(':admin') or c in {'api_keys:issue', 'api_keys:revoke'} for c in caps)


@pytest.mark.django_db
def test_migration_idempotent():
    call_command('migrate', 'engram', run_syncdb=True)
    call_command('migrate', 'engram', run_syncdb=True)
    assert RoleCapability.objects.filter(role__code='organization_owner', capability__code='teams:admin').count() == 1
```

- [ ] **Step 2: Run → FAIL** (roles/caps not seeded yet).

- [ ] **Step 3: Write the data migration** `0007_admin_capabilities.py`:

```python
from django.db import migrations


CAPABILITIES = [
    'organizations:read', 'organizations:admin',
    'teams:read', 'teams:admin',
    'projects:read', 'projects:admin',
    'members:read', 'members:admin',
    'roles:read',
    'api_keys:read', 'api_keys:issue', 'api_keys:revoke', 'api_keys:*',
    'members:*', 'teams:*', 'projects:*',
]

ROLE_CAPABILITIES = {
    'organization_owner': CAPABILITIES,
    'organization_admin': [
        'teams:read', 'teams:admin', 'projects:read', 'projects:admin',
        'members:read', 'members:admin', 'roles:read',
        'api_keys:read', 'api_keys:issue', 'api_keys:revoke',
        'organizations:read',
    ],
    'developer': ['projects:read', 'teams:read', 'api_keys:read'],
    'auditor': [
        'organizations:read', 'teams:read', 'projects:read', 'members:read',
        'roles:read', 'api_keys:read', 'audit:read',
    ],
}


def ensure(apps, schema_editor):
    Capability = apps.get_model('engram', 'Capability')
    Role = apps.get_model('engram', 'Role')
    RoleCapability = apps.get_model('engram', 'RoleCapability')

    cap_map = {}
    for code in CAPABILITIES:
        cap, _ = Capability.objects.get_or_create(code=code)
        cap_map[code] = cap

    for role_code, codes in ROLE_CAPABILITIES.items():
        try:
            role = Role.objects.get(code=role_code)
        except Role.DoesNotExist:
            continue
        for code in codes:
            RoleCapability.objects.get_or_create(role=role, capability=cap_map[code])


class Migration(migrations.Migration):

    dependencies = [
        ('engram', '0006_pgvector_conditional'),
    ]

    operations = [
        migrations.RunPython(ensure, migrations.RunPython.noop),
    ]
```

(Adjust the `0006` dependency name to the actual latest access migration. The four roles must already exist from earlier seeds — verify in `engram/access/migrations/`; if not, seed them in this migration's `ensure` with `Role.objects.get_or_create(code=..., defaults={'name':..., 'built_in': True})`.)

- [ ] **Step 4: Run → PASS.**

- [ ] **Step 5: Commit**

```bash
git add apps/backend/engram/access/migrations/ apps/backend/engram/access/admin_capabilities_seed_tests.py
git commit -m "feat: seed admin capabilities and role grants"
```

---

## Task 2: Organizations — read + update

**Goal:** `GET /v1/admin/organizations/` (orgs the user belongs to) and `GET/PATCH /v1/admin/organizations/{id}/`, capability-gated.

**Files:**
- Create: `apps/backend/engram/console/serializers/__init__.py`, `organizations.py`
- Create: `apps/backend/engram/console/views/__init__.py`, `organizations.py`
- Modify: `apps/backend/engram/console/urls.py` (register)
- Test: `apps/backend/engram/console/views/organizations_tests.py` (API tests, mocks per rule #21)

**Acceptance Criteria:**
- [ ] `GET /v1/admin/organizations/` returns orgs where the user is an active member; requires `organizations:read`.
- [ ] `PATCH` updates `name` (slug is immutable); requires `organizations:admin`; writes `OrganizationUpdated` audit event.
- [ ] 403 when capability missing; 404 when org not in user's memberships.
- [ ] Pagination envelope (`count`, `next`, `previous`, `results`).

**Verify:** `docker compose -f deploy/compose/docker-compose.yml exec -T api pytest engram/console/views/organizations_tests.py -v` → PASS.

**Steps:**

- [ ] **Step 1: Write failing API tests** (use DRF `APIClient`, factory-created User+Identity+membership+role with capabilities; mocks for audit where a service boundary exists).

```python
import pytest
from rest_framework.test import APIClient


@pytest.mark.django_db
def test_list_organizations_requires_capability(f_owner_user_token):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Token {f_owner_user_token}')
    r = client.get('/v1/admin/organizations/')
    assert r.status_code == 200
    assert 'results' in r.data


@pytest.mark.django_db
def test_list_denied_without_membership(f_developer_user_token, other_org):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Token {f_developer_user_token}')
    client.credentials(HTTP_X_ENGRAM_ORGANIZATION=str(other_org.id))
    r = client.get('/v1/admin/organizations/')
    assert r.status_code == 403


@pytest.mark.django_db
def test_patch_organization_name(f_owner_user_token, f_owned_org):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Token {f_owner_user_token}', HTTP_X_ENGRAM_ORGANIZATION=str(f_owned_org.id))
    r = client.patch(f'/v1/admin/organizations/{f_owned_org.id}/', {'name': 'Renamed'})
    assert r.status_code == 200
    assert r.data['name'] == 'Renamed'


@pytest.mark.django_db
def test_slug_is_immutable(f_owner_user_token, f_owned_org):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Token {f_owner_user_token}', HTTP_X_ENGRAM_ORGANIZATION=str(f_owned_org.id))
    r = client.patch(f'/v1/admin/organizations/{f_owned_org.id}/', {'slug': 'new-slug'})
    assert r.status_code == 400
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement serializer** `serializers/organizations.py`:

```python
from rest_framework import serializers

from engram.core.models import Organization


class OrganizationReadSerializer(serializers.ModelSerializer):
    class Meta:
        model = Organization
        fields = ['id', 'name', 'slug', 'created_at', 'updated_at']
        read_only_fields = ['id', 'slug', 'created_at', 'updated_at']


class OrganizationWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = Organization
        fields = ['name']
```

- [ ] **Step 4: Implement ViewSet** `views/organizations.py`:

```python
from rest_framework import mixins, viewsets
from rest_framework.permissions import IsAuthenticated

from engram.console.org_resolution import resolve_active_organization
from engram.console.permissions import RequireCapability
from engram.console.serializers.organizations import (
    OrganizationReadSerializer,
    OrganizationWriteSerializer,
)
from engram.core.models import Organization


class OrganizationViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    permission_classes = [IsAuthenticated]

    def get_permissions(self):
        if self.action in {'list', 'retrieve'}:
            return [IsAuthenticated(), RequireCapability('organizations:read')]
        return [IsAuthenticated(), RequireCapability('organizations:admin')]

    def get_queryset(self):
        return Organization.objects.filter(
            organization_memberships__identity__django_user=self.request.user,
            organization_memberships__active=True,
        ).distinct()

    def get_serializer_class(self):
        if self.action in {'partial_update', 'update'}:
            return OrganizationWriteSerializer
        return OrganizationReadSerializer
```

- [ ] **Step 5: Register in `urls.py`**: `router.register('organizations', OrganizationViewSet, basename='admin-organization')`.

- [ ] **Step 6: Run → PASS.** Wire audit in Task 8 (or inline here via a `perform_update` that calls `audit_admin_action` — define the helper in Task 8 and back-reference; to keep tasks independent, add a minimal `console/services.py:audit_admin_action` here).

Minimal audit helper in `console/services.py`:
```python
def audit_admin_action(*, organization, actor_identity, event_type, target_type, target_id, metadata, result='allowed'):
    from engram.core.models import AuditEvent, AuditResult
    AuditEvent.objects.create(
        organization=organization,
        event_type=event_type,
        actor_type='user',
        actor_id=str(actor_identity.id),
        target_type=target_type,
        target_id=str(target_id),
        capability='',
        result=result,
        metadata=metadata,
    )
```
Call it from `perform_update`.

- [ ] **Step 7: Commit**

```bash
git add apps/backend/engram/console/
git commit -m "feat: add organizations admin read and update endpoints"
```

---

## Task 3: Teams — full CRUD

**Goal:** `GET/POST/PATCH/DELETE /v1/admin/teams/` scoped to active org; delete = archive.

**Files:**
- Create: `engram/console/serializers/teams.py`, `engram/console/views/teams.py`
- Modify: `engram/console/urls.py`, `engram/console/services.py` (`create_team`, `archive_team`)
- Test: `engram/console/views/teams_tests.py`

**Acceptance Criteria:**
- [ ] List scoped to `X-Engram-Organization`; create requires `teams:admin`; new team has org-scoped unique slug.
- [ ] `DELETE` archives (sets `archived_at` or `active=False` — confirm Team model field; if none, add a nullable `archived_at` migration).
- [ ] 403 without capability; audit on create/archive.

**Verify:** `docker compose ... exec -T api pytest engram/console/views/teams_tests.py -v` → PASS.

**Steps:**

- [ ] **Step 1: Confirm Team model** — does it have `archived_at`/`is_archived`? Read `core/models.py` around line 124. If not, add migration `0008_team_archived_at.py` adding `archived_at = DateTimeField(null=True, blank=True)` and filter `archived_at__isnull=True` in `get_queryset`.

- [ ] **Step 2: Write failing tests** (list/create/archive/403/tenant-isolation — follow the Organizations test shape, scoped to active org header).

- [ ] **Step 3: Implement** `serializers/teams.py` (read: id, name, slug, organization, created_at, archived_at; write: name, slug) and `views/teams.py`:

```python
class TeamViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, ActiveOrganizationPermission]

    def get_permissions(self):
        base = [IsAuthenticated(), ActiveOrganizationPermission()]
        if self.action == 'list':
            return base + [RequireCapability('teams:read')]
        return base + [RequireCapability('teams:admin')]

    def get_queryset(self):
        return Team.objects.filter(organization=self.request.active_organization, archived_at__isnull=True)

    def get_serializer_class(self):
        return TeamWriteSerializer if self.action in {'create', 'update', 'partial_update'} else TeamReadSerializer

    def perform_create(self, serializer):
        team = create_team(organization=self.request.active_organization, **serializer.validated_data, actor_identity=self.request.user_identity)
        serializer.instance = team

    def perform_destroy(self, instance):
        archive_team(team=instance, actor_identity=self.request.user_identity)
```

`services.create_team` validates slug uniqueness within org, creates `Team`, audits `TeamCreated`. `archive_team` sets `archived_at`, audits `TeamArchived`.

`ActiveOrganizationPermission`: in `org_resolution.py`, a `BasePermission` that calls `resolve_active_organization`, stashes `request.active_organization` + `request.user_identity` + `request.effective_scope`, returns False (403) on resolution errors.

- [ ] **Step 4: Register `router.register('teams', TeamViewSet, basename='admin-team')`.**

- [ ] **Step 5: Run → PASS. Commit** `feat: add teams admin CRUD`.

---

## Task 4: Projects — full CRUD

**Goal:** `GET/POST/PATCH/DELETE /v1/admin/projects/` with `repository_url`, `default_branch`.

**Files:** `serializers/projects.py`, `views/projects.py`, services (`create_project`, `archive_project`), tests.

**Acceptance Criteria:**
- [ ] Create/update validates org-scoped slug uniqueness; `projects:admin` for writes.
- [ ] Fields `name`, `slug`, `repository_url`, `default_branch` editable; archive on delete.
- [ ] Tenant isolation + audit.

**Verify:** `pytest engram/console/views/projects_tests.py -v` → PASS.

**Steps:** Mirror Task 3 exactly, swapping Team→Project, fields, capabilities (`projects:read`/`projects:admin`), events `ProjectCreated`/`ProjectArchived`. Register `router.register('projects', ProjectViewSet, ...)`. Commit `feat: add projects admin CRUD`.

---

## Task 5: Members — identity + org-membership CRUD with last-owner guard

**Goal:** `GET/POST/PATCH/DELETE /v1/admin/members/` managing `Identity` (user type) + `OrganizationMembership`.

**Files:** `serializers/members.py`, `views/members.py`, services (`invite_member`, `set_member_role`, `remove_member`), tests.

**Acceptance Criteria:**
- [ ] `POST` creates a user `Identity` + `OrganizationMembership` with a chosen role; `members:admin`.
- [ ] `PATCH` changes role; `DELETE` deactivates membership (soft).
- [ ] Cannot remove/deactivate the **last active owner** (`organization_owner`) → 409.
- [ ] Never returns credentials; read serializer shows identity, role, active.

**Verify:** `pytest engram/console/views/members_tests.py -v` → PASS, incl. last-owner test.

**Steps:**

- [ ] **Step 1: Failing tests** incl.:
```python
def test_cannot_remove_last_owner(f_owner_user_token, f_owned_org, f_owner_member):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Token {f_owner_user_token}', HTTP_X_ENGRAM_ORGANIZATION=str(f_owned_org.id))
    r = client.delete(f'/v1/admin/members/{f_owner_member.id}/')
    assert r.status_code == 409
```

- [ ] **Step 2: Implement services**: `invite_member(organization, external_id, display_name, email, role_code, actor)` get_or_creates `Identity(identity_type=USER)` + `OrganizationMembership`; `remove_member` counts active owners of the org before deactivating; raises `LastOwnerError` (→ 409 via exception handler) when it would remove the last.

- [ ] **Step 3: ViewSet** `MembersViewSet` (list/create/retrieve/partial_update/destroy), `members:read`/`members:admin`, scoped to `organization=request.active_organization` through `OrganizationMembership`.

- [ ] **Step 4: Register + add 409 handler** in `console/permissions.py` or a small `console/exceptions.py` mapping `LastOwnerError → 409`.

- [ ] **Step 5: Run → PASS. Commit** `feat: add members admin CRUD with last-owner guard`.

---

## Task 6: Roles — read + capabilities

**Goal:** `GET /v1/admin/roles/` returns built-in roles and their capabilities (`roles:read`).

**Files:** `serializers/roles.py`, `views/roles.py`, tests.

**Acceptance Criteria:**
- [ ] Returns roles with nested `capabilities` list; built-in roles only in Phase A.
- [ ] `roles:read` required; 403 otherwise.

**Verify:** `pytest engram/console/views/roles_tests.py -v` → PASS.

**Steps:** Read-only `ListModelMixin` + `RetrieveModelMixin` ViewSet; `RoleReadSerializer` with `capabilities = SerializerMethodField()` returning `RoleCapability` codes. Register `router.register('roles', RoleViewSet, ...)`. Commit `feat: add roles admin read endpoint`.

---

## Task 7: API Keys — issue / list / retrieve / revoke (plaintext once)

**Goal:** Full API-key management; the owner's headline request.

**Files:** `serializers/api_keys.py`, `views/api_keys.py`, services (`issue_api_key`, `revoke_api_key`), tests.

**Acceptance Criteria:**
- [ ] `POST /v1/admin/api-keys/` issues `egk_<32>`, stores prefix+HMAC hash+fingerprint (reuse `hash_api_key`, `api_key_fingerprint`), returns `plaintext` **exactly once** + id/name/prefix/fingerprint/capabilities.
- [ ] `GET` (list/detail) NEVER returns plaintext; returns prefix, fingerprint, owner, capabilities, created, expires, last_used, active, revoked_at.
- [ ] Capabilities on a key are a subset of the owner's effective capabilities (validation rejects widening).
- [ ] `POST /v1/admin/api-keys/{id}/revoke/` sets `revoked_at`; the key then fails `ResolveApiKeyScope` (verify via integration).
- [ ] `api_keys:issue`/`api_keys:read`/`api_keys:revoke` gating.

**Verify:** `pytest engram/console/views/api_keys_tests.py -v` → PASS; plus a revoke-blocks-auth assertion using `ResolveApiKeyScope`.

**Steps:**

- [ ] **Step 1: Failing tests** incl. plaintext-once and no-leak:
```python
def test_issue_returns_plaintext_once(f_owner_user_token, f_owned_org):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Token {f_owner_user_token}', HTTP_X_ENGRAM_ORGANIZATION=str(f_owned_org.id))
    r = client.post('/v1/admin/api-keys/', {'name': 'ci', 'capabilities': ['observations:write']})
    assert r.status_code == 201
    assert r.data['plaintext'].startswith('egk_')
    key_id = r.data['id']
    detail = client.get(f'/v1/admin/api-keys/{key_id}/')
    assert 'plaintext' not in detail.data
    assert detail.data['key_prefix'] == r.data['key_prefix']


def test_capabilities_cannot_exceed_owner(f_owner_user_token, f_owned_org):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Token {f_owner_user_token}', HTTP_X_ENGRAM_ORGANIZATION=str(f_owned_org.id))
    r = client.post('/v1/admin/api-keys/', {'name': 'x', 'capabilities': ['organizations:admin']})
    assert r.status_code == 400


def test_revoke_blocks_auth(f_owner_user_token, f_owned_org):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Token {f_owner_user_token}', HTTP_X_ENGRAM_ORGANIZATION=str(f_owned_org.id))
    issued = client.post('/v1/admin/api-keys/', {'name': 'ci', 'capabilities': ['observations:write']})
    key_id = issued.data['id']
    client.post(f'/v1/admin/api-keys/{key_id}/revoke/')
    from engram.access.services import ResolveApiKeyScope, AccessDeniedError
    with pytest.raises(AccessDeniedError):
        ResolveApiKeyScope().execute(raw_key=issued.data['plaintext'], required_capability='observations:write')
```

- [ ] **Step 2: Implement** `serializers/api_keys.py`:
  - `ApiKeyReadSerializer`: id, name, key_prefix, key_fingerprint, owner_identity, capabilities, created_at, expires_at, last_used_at, active, revoked_at.
  - `ApiKeyIssueInputSerializer`: name, capabilities (list), optional team/project/expires_at.
  - `ApiKeyIssueResultSerializer`: id, name, key_prefix, key_fingerprint, plaintext, capabilities, created_at.

- [ ] **Step 3: Implement services** `issue_api_key`: generate `secrets.token_urlsafe(32)` prefixed `egk_`, validate requested capabilities ⊆ owner effective capabilities (from `request.effective_scope`), run `ApiKey.clean()` org/team/project scope, persist with `hash_api_key`/`api_key_fingerprint`, write `ApiKeyCapability` rows, audit `ApiKeyIssued`. `revoke_api_key`: set `revoked_at=now()`, audit `ApiKeyRevoked`.

- [ ] **Step 4: ViewSet** with custom `create` (returns `ApiKeyIssueResultSerializer`), `list`/`retrieve` (read serializer), and `@action(detail=True, methods=['post']) revoke`.

- [ ] **Step 5: Register `router.register('api-keys', ApiKeyViewSet, basename='admin-api-key')`.**

- [ ] **Step 6: Run → PASS. Commit** `feat: add api key issue list revoke admin endpoints`.

---

## Task 8: Audit wiring + admin integration test + OpenAPI hook

**Goal:** Ensure every admin mutation writes `AuditEvent`; add an end-to-end test; wire `drf-spectacular` so the admin API has a schema (unblocks Phase D).

**Files:**
- Create: `engram/console/integration_tests.py`
- Modify: `settings/settings.py` (SPECTACULAR settings, schema view), `settings/urls.py` (schema route)
- Verify audit calls exist in each service (refactor if any mutation missed it)

**Acceptance Criteria:**
- [ ] Integration test: login → set org header → create team → create project → invite member → issue api key → revoke → assert each produced an `AuditEvent` with correct `actor_type='user'`, `target_type`, non-empty metadata, and that revoked key no longer authenticates.
- [ ] `GET /api/schema/` returns 200 OpenAPI JSON listing `/v1/admin/...` paths.
- [ ] Denied capability attempts (403 path) also produce an audit event.

**Verify:** `docker compose ... exec -T api pytest engram/console/integration_tests.py -v` → PASS; `curl -s localhost:8000/api/schema/ | grep admin` shows admin paths.

**Steps:**

- [ ] **Step 1: Write the integration test** (factories build owner+org+role; APIClient walks the full flow; queries `AuditEvent.objects.filter(actor_type='user', organization=org)` and asserts event_types present).

- [ ] **Step 2: Audit denied attempts** — in `RequireCapability.has_permission`, when denied, call `audit_admin_action(..., result='denied', metadata={'required_capability': self.code})` (best-effort; guard if `request.active_organization` unresolved).

- [ ] **Step 3: Wire drf-spectacular** — INSTALLED_APPS add `'drf_spectacular'`; REST_FRAMEWORK `'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema'`; add `SPECTACULAR_SETTINGS = {'TITLE': 'Engram API', 'DESCRIPTION': '...'}`; urls.py add `path('api/schema/', SpectacularAPIView.as_view(), name='schema')`.

- [ ] **Step 4: Run → PASS. Commit** `feat: wire admin audit and openapi schema`.

- [ ] **Step 5: Open a draft MR for Phase A**, run full backend suite:
`docker compose ... exec -T api pytest -q` → all green (existing ~335 tests + new admin tests). Record exit code in the MR.

---

## Self-Review (completed during writing)

- **Spec coverage:** AD-1 auth → Task 0; AD-2 RBAC → Task 0 + denied-audit Task 8; AD-3 org header → Task 0 `org_resolution`; AD-4 router → Task 0; AD-5 read/write split → every resource task; AD-6 audit → Task 8 + per-task `audit_admin_action`; capability/role model → Task 1; each resource in the API surface → Tasks 2–7; api-key security → Task 7; integration + OpenAPI → Task 8. Gap: none.
- **Placeholders:** none — each step has real code or an explicit "confirm X, then Y" instruction with the file/line to confirm.
- **Type consistency:** `RequireCapability`, `resolve_active_organization`, `ActiveOrganizationPermission`, `audit_admin_action`, `request.active_organization`, `request.user_identity`, `request.effective_scope` used consistently across tasks.
- **Open confirmations** (not placeholders — verifiable facts): latest access migration number (Task 1 dep), Team `archived_at` field existence (Task 3 step 1), backend package manager (Task 0 step 8), `Identity`↔`User` reverse accessor name (Task 0 step 6 note).
