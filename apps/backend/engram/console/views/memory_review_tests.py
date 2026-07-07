from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from django.contrib.auth.models import User
from django.utils import timezone
from rest_framework.test import APIClient

from engram.access.auth_services import external_id_for_user
from engram.access.models import (
    Capability,
    Identity,
    IdentityType,
    OrganizationMembership,
    Role,
    RoleCapability,
)
from engram.console.views.memory_review import PAGE_SIZE
from engram.core.models import (
    AuditEvent,
    CandidateStatus,
    LinkType,
    Memory,
    MemoryCandidate,
    MemoryLink,
    MemoryStatus,
    MemoryVersion,
    Observation,
    ObservationSource,
    Organization,
    Project,
    Team,
    VisibilityScope,
)


def _make_user(username: str = 'alice') -> User:
    return User.objects.create_user(username=username, password='strong-secret-123')  # noqa: S106


def _make_role_with_capabilities(code: str, capability_codes: tuple[str, ...]) -> Role:
    role, _ = Role.objects.get_or_create(code=code, defaults={'name': code})

    for raw_code in capability_codes:
        capability, _ = Capability.objects.get_or_create(
            code=raw_code,
            defaults={'description': raw_code},
        )

        RoleCapability.objects.get_or_create(role=role, capability=capability)

    return role


def _make_identity(user: User, organization: Organization) -> Identity:
    identity, _ = Identity.objects.get_or_create(
        organization=organization,
        identity_type=IdentityType.USER,
        external_id=external_id_for_user(user),
        defaults={'display_name': user.get_username()},
    )

    return identity


def _auth_client(token: str, org: Organization) -> APIClient:
    client = APIClient()

    client.credentials(
        HTTP_AUTHORIZATION=f'Token {token}',
        HTTP_X_ENGRAM_ORGANIZATION=str(org.id),
    )

    return client


@pytest.fixture
def f_admin_token() -> str:
    user = _make_user('admin')
    org = Organization.objects.create(name='Acme', slug='acme')
    identity = _make_identity(user, org)

    role = _make_role_with_capabilities(
        'memory_admin',
        ('memories:review', 'memories:admin'),
    )

    OrganizationMembership.objects.create(organization=org, identity=identity, role=role)

    from rest_framework.authtoken.models import Token

    return Token.objects.get_or_create(user=user)[0].key


@pytest.fixture
def f_admin_org() -> Organization:
    return Organization.objects.get(slug='acme')


@pytest.fixture
def f_reviewer_token() -> str:
    user = _make_user('reviewer')

    other_org = Organization.objects.create(name='Reviewerco', slug='reviewerco')

    identity = _make_identity(user, other_org)

    role = _make_role_with_capabilities(
        'memory_reviewer',
        ('memories:review',),
    )

    OrganizationMembership.objects.create(organization=other_org, identity=identity, role=role)

    from rest_framework.authtoken.models import Token

    return Token.objects.get_or_create(user=user)[0].key


@pytest.fixture
def f_reviewer_org() -> Organization:
    return Organization.objects.get(slug='reviewerco')


@pytest.fixture
def f_foreign_org() -> Organization:
    return Organization.objects.create(name='Globex', slug='globex')


@pytest.fixture
def f_project(f_admin_org: Organization) -> Project:
    return Project.objects.create(
        organization=f_admin_org,
        name='Eng',
        slug='eng',
    )


@pytest.fixture
def f_team(f_admin_org: Organization) -> Team:
    return Team.objects.create(organization=f_admin_org, name='Core', slug='core')


def _make_observation(organization: Organization, project: Project) -> Observation:
    from engram.core.models import Agent, AgentSession

    agent = Agent.objects.create(
        organization=organization,
        external_id='agent-' + str(Agent.objects.count()),
    )

    session = AgentSession.objects.create(
        organization=organization,
        project=project,
        agent=agent,
        external_session_id='session-' + str(AgentSession.objects.count()),
    )

    return Observation.objects.create(
        organization=organization,
        project=project,
        agent=agent,
        session=session,
        title='Obs title',
        body='Obs body',
        observation_type='tool_use',
        content_hash='hash-obs-' + str(Observation.objects.count()),
    )


def _make_candidate(
    organization: Organization,
    project: Project,
    *,
    team: Team | None = None,
    status: str = CandidateStatus.PROPOSED,
    confidence: str = '0.500',
    visibility_scope: str = VisibilityScope.PROJECT,
    evidence: list | None = None,
    source_observation: Observation | None = None,
    created_at: datetime | None = None,
) -> MemoryCandidate:
    counter = MemoryCandidate.objects.count()

    candidate = MemoryCandidate.objects.create(
        organization=organization,
        project=project,
        team=team,
        title=f'Candidate {counter}',
        body=f'Body {counter}',
        status=status,
        visibility_scope=visibility_scope,
        evidence=evidence if evidence is not None else [],
        content_hash='hash-c-' + str(counter),
        confidence=confidence,
        source_observation=source_observation,
    )

    if created_at is not None:
        MemoryCandidate.objects.filter(id=candidate.id).update(created_at=created_at)

        candidate.refresh_from_db()

    return candidate


def _make_memory(
    organization: Organization,
    project: Project,
    *,
    team: Team | None = None,
    status: str = MemoryStatus.APPROVED,
    confidence: str = '0.900',
    visibility_scope: str = VisibilityScope.PROJECT,
    body: str = 'memory body',
    title: str = 'memory',
    created_at: datetime | None = None,
) -> Memory:
    counter = Memory.objects.count()

    memory = Memory.objects.create(
        organization=organization,
        project=project,
        team=team,
        title=title if title != 'memory' else f'Memory {counter}',
        body=body,
        status=status,
        visibility_scope=visibility_scope,
        confidence=confidence,
    )

    if created_at is not None:
        Memory.objects.filter(id=memory.id).update(created_at=created_at)

        memory.refresh_from_db()

    return memory


def _make_version(memory: Memory, version: int, body: str) -> MemoryVersion:
    import hashlib

    return MemoryVersion.objects.create(
        organization=memory.organization,
        project=memory.project,
        memory=memory,
        version=version,
        body=body,
        content_hash=hashlib.sha256(f'{memory.id}:{version}:{body}'.encode()).hexdigest(),
    )


@pytest.mark.django_db
def test_queue_returns_proposed_candidates_and_reviewable_memories(
    f_admin_token: str,
    f_admin_org: Organization,
    f_project: Project,
) -> None:
    _make_candidate(f_admin_org, f_project, status=CandidateStatus.PROPOSED)

    _make_memory(f_admin_org, f_project, status=MemoryStatus.CONFLICT)

    _make_memory(f_admin_org, f_project, status=MemoryStatus.REFUTED)

    _make_memory(f_admin_org, f_project, status=MemoryStatus.APPROVED, confidence='0.100')

    client = _auth_client(f_admin_token, f_admin_org)

    response = client.get('/v1/admin/memory-review/')

    assert response.status_code == 200

    items = response.data['results']

    assert len(items) == 4

    types = {item['type'] for item in items}

    assert 'candidate' in types

    assert 'memory' in types


@pytest.mark.django_db
def test_queue_orders_mixed_items_by_created_at_desc(
    f_admin_token: str,
    f_admin_org: Organization,
    f_project: Project,
) -> None:
    now = timezone.now()

    oldest = _make_candidate(
        f_admin_org,
        f_project,
        status=CandidateStatus.PROPOSED,
        created_at=now - timedelta(days=3),
    )

    middle = _make_memory(
        f_admin_org,
        f_project,
        status=MemoryStatus.CONFLICT,
        created_at=now - timedelta(days=2),
    )

    newest = _make_candidate(
        f_admin_org,
        f_project,
        status=CandidateStatus.PROPOSED,
        created_at=now - timedelta(days=1),
    )

    client = _auth_client(f_admin_token, f_admin_org)

    response = client.get('/v1/admin/memory-review/')

    assert response.status_code == 200

    ordered_ids = [item['id'] for item in response.data['results']]

    assert ordered_ids == [str(newest.id), str(middle.id), str(oldest.id)]

    assert response.data['count'] == 3


@pytest.mark.django_db
def test_queue_paginates_across_pages_by_created_at(
    f_admin_token: str,
    f_admin_org: Organization,
    f_project: Project,
) -> None:
    now = timezone.now()

    candidates = [
        _make_candidate(
            f_admin_org,
            f_project,
            status=CandidateStatus.PROPOSED,
            created_at=now - timedelta(minutes=index),
        )
        for index in range(PAGE_SIZE + 5)
    ]

    client = _auth_client(f_admin_token, f_admin_org)

    first = client.get('/v1/admin/memory-review/')

    assert first.status_code == 200
    assert len(first.data['results']) == PAGE_SIZE
    assert first.data['count'] == PAGE_SIZE + 5
    assert first.data['next'] is not None
    assert first.data['previous'] is None
    assert [item['id'] for item in first.data['results']] == [str(c.id) for c in candidates[:PAGE_SIZE]]

    second = client.get('/v1/admin/memory-review/?page=2')

    assert second.status_code == 200
    assert len(second.data['results']) == 5
    assert second.data['next'] is None
    assert second.data['previous'] is not None
    assert [item['id'] for item in second.data['results']] == [str(c.id) for c in candidates[PAGE_SIZE:]]


@pytest.mark.django_db
def test_queue_issues_bounded_queries_for_large_backlog(
    f_admin_token: str,
    f_admin_org: Organization,
    f_project: Project,
) -> None:
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    for _ in range(PAGE_SIZE * 2 + 10):
        _make_candidate(f_admin_org, f_project, status=CandidateStatus.PROPOSED)

    client = _auth_client(f_admin_token, f_admin_org)

    with CaptureQueriesContext(connection) as ctx:
        response = client.get('/v1/admin/memory-review/')

    assert response.status_code == 200
    assert len(response.data['results']) == PAGE_SIZE
    assert response.data['count'] == PAGE_SIZE * 2 + 10

    candidate_selects = [
        query['sql']
        for query in ctx.captured_queries
        if 'memorycandidate' in query['sql'].lower()
        and query['sql'].lstrip().lower().startswith('select')
        and 'count(' not in query['sql'].lower()
    ]

    assert candidate_selects
    assert all(f'LIMIT {PAGE_SIZE}' in sql for sql in candidate_selects)


@pytest.mark.django_db
def test_queue_excludes_approved_high_confidence_memories_and_other_org(
    f_admin_token: str,
    f_admin_org: Organization,
    f_project: Project,
    f_foreign_org: Organization,
) -> None:
    foreign_project = Project.objects.create(
        organization=f_foreign_org,
        name='Foreign',
        slug='foreign',
    )

    _make_memory(f_foreign_org, foreign_project, status=MemoryStatus.CONFLICT)

    _make_memory(f_admin_org, f_project, status=MemoryStatus.APPROVED, confidence='0.900')

    client = _auth_client(f_admin_token, f_admin_org)

    response = client.get('/v1/admin/memory-review/')

    assert response.status_code == 200

    assert response.data['results'] == []

    assert response.data['count'] == 0


@pytest.mark.django_db
def test_queue_filters_by_confidence_range(
    f_admin_token: str,
    f_admin_org: Organization,
    f_project: Project,
) -> None:
    _make_memory(f_admin_org, f_project, status=MemoryStatus.CONFLICT, confidence='0.100')

    _make_memory(f_admin_org, f_project, status=MemoryStatus.CONFLICT, confidence='0.500')

    _make_memory(f_admin_org, f_project, status=MemoryStatus.CONFLICT, confidence='0.950')

    client = _auth_client(f_admin_token, f_admin_org)

    response = client.get('/v1/admin/memory-review/?confidence__gte=0.300&confidence__lte=0.700')

    assert response.status_code == 200

    items = response.data['results']

    assert len(items) == 1

    assert items[0]['confidence'] == '0.500'


@pytest.mark.django_db
def test_queue_filters_by_visibility_scope(
    f_admin_token: str,
    f_admin_org: Organization,
    f_project: Project,
    f_team: Team,
) -> None:
    _make_memory(
        f_admin_org,
        f_project,
        team=f_team,
        status=MemoryStatus.CONFLICT,
        visibility_scope=VisibilityScope.TEAM,
    )

    _make_memory(
        f_admin_org,
        f_project,
        status=MemoryStatus.CONFLICT,
        visibility_scope=VisibilityScope.PROJECT,
    )

    client = _auth_client(f_admin_token, f_admin_org)

    response = client.get('/v1/admin/memory-review/?visibility_scope=team')

    assert response.status_code == 200

    items = response.data['results']

    assert len(items) == 1

    assert items[0]['visibility_scope'] == 'team'


@pytest.mark.django_db
def test_queue_filters_by_status(
    f_admin_token: str,
    f_admin_org: Organization,
    f_project: Project,
) -> None:
    _make_candidate(f_admin_org, f_project, status=CandidateStatus.PROPOSED)

    _make_memory(f_admin_org, f_project, status=MemoryStatus.CONFLICT)

    _make_memory(f_admin_org, f_project, status=MemoryStatus.REFUTED)

    client = _auth_client(f_admin_token, f_admin_org)

    response = client.get('/v1/admin/memory-review/?status=conflict')

    assert response.status_code == 200

    items = response.data['results']

    assert len(items) == 1

    assert items[0]['status'] == 'conflict'


@pytest.mark.django_db
def test_queue_filters_by_age_days(
    f_admin_token: str,
    f_admin_org: Organization,
    f_project: Project,
) -> None:
    old_created = timezone.now() - timedelta(days=10)

    _make_memory(
        f_admin_org,
        f_project,
        status=MemoryStatus.CONFLICT,
        created_at=old_created,
    )

    _make_memory(f_admin_org, f_project, status=MemoryStatus.CONFLICT)

    client = _auth_client(f_admin_token, f_admin_org)

    response = client.get('/v1/admin/memory-review/?age_days__gte=5')

    assert response.status_code == 200

    items = response.data['results']

    assert len(items) == 1


@pytest.mark.django_db
def test_queue_filters_by_team_and_project(
    f_admin_token: str,
    f_admin_org: Organization,
    f_project: Project,
    f_team: Team,
) -> None:
    other_project = Project.objects.create(
        organization=f_admin_org,
        name='Other',
        slug='other',
    )

    _make_memory(
        f_admin_org,
        f_project,
        team=f_team,
        status=MemoryStatus.CONFLICT,
    )

    _make_memory(f_admin_org, other_project, status=MemoryStatus.CONFLICT)

    client = _auth_client(f_admin_token, f_admin_org)

    response = client.get(f'/v1/admin/memory-review/?project_id={f_project.id}')

    assert response.status_code == 200

    items = response.data['results']

    assert len(items) == 1

    response_team = client.get(f'/v1/admin/memory-review/?team_id={f_team.id}')

    assert response_team.status_code == 200

    assert len(response_team.data['results']) == 1


@pytest.mark.django_db
def test_queue_filters_by_invalid_team_id_returns_400(
    f_admin_token: str,
    f_admin_org: Organization,
    f_project: Project,
) -> None:
    _make_memory(f_admin_org, f_project, status=MemoryStatus.CONFLICT)

    client = _auth_client(f_admin_token, f_admin_org)

    response = client.get('/v1/admin/memory-review/?team_id=not-a-uuid')

    assert response.status_code == 400

    assert response.data['code'] == 'invalid_filter'


@pytest.mark.django_db
def test_queue_filters_by_invalid_project_id_returns_400(
    f_admin_token: str,
    f_admin_org: Organization,
    f_project: Project,
) -> None:
    _make_memory(f_admin_org, f_project, status=MemoryStatus.CONFLICT)

    client = _auth_client(f_admin_token, f_admin_org)

    response = client.get('/v1/admin/memory-review/?project_id=not-a-uuid')

    assert response.status_code == 400

    assert response.data['code'] == 'invalid_filter'


@pytest.mark.django_db
def test_queue_filters_by_search_matches_title_and_body(
    f_admin_token: str,
    f_admin_org: Organization,
    f_project: Project,
) -> None:
    _make_candidate(f_admin_org, f_project, status=CandidateStatus.PROPOSED)

    target = _make_memory(
        f_admin_org,
        f_project,
        status=MemoryStatus.CONFLICT,
        title='Authentication flow notes',
        body='describes the login handshake',
    )

    _make_memory(
        f_admin_org,
        f_project,
        status=MemoryStatus.CONFLICT,
        title='Billing notes',
        body='unrelated billing details',
    )

    client = _auth_client(f_admin_token, f_admin_org)

    response = client.get('/v1/admin/memory-review/?search=authentication')

    assert response.status_code == 200

    items = response.data['results']

    assert len(items) == 1

    assert items[0]['id'] == str(target.id)


@pytest.mark.django_db
def test_queue_filters_by_source_type(
    f_admin_token: str,
    f_admin_org: Organization,
    f_project: Project,
) -> None:
    file_observation = _make_observation(f_admin_org, f_project)

    ObservationSource.objects.create(
        organization=f_admin_org,
        project=f_project,
        observation=file_observation,
        source_type='file',
        source_id='src/app.py',
    )

    web_observation = _make_observation(f_admin_org, f_project)

    ObservationSource.objects.create(
        organization=f_admin_org,
        project=f_project,
        observation=web_observation,
        source_type='web',
        source_id='https://example.com',
    )

    file_candidate = _make_candidate(
        f_admin_org,
        f_project,
        status=CandidateStatus.PROPOSED,
        source_observation=file_observation,
    )

    _make_candidate(
        f_admin_org,
        f_project,
        status=CandidateStatus.PROPOSED,
        source_observation=web_observation,
    )

    client = _auth_client(f_admin_token, f_admin_org)

    response = client.get('/v1/admin/memory-review/?source_type=file')

    assert response.status_code == 200

    items = response.data['results']

    assert len(items) == 1

    assert items[0]['id'] == str(file_candidate.id)


@pytest.mark.django_db
def test_queue_serializer_includes_provenance_and_citations(
    f_admin_token: str,
    f_admin_org: Organization,
    f_project: Project,
) -> None:
    observation = _make_observation(f_admin_org, f_project)

    ObservationSource.objects.create(
        organization=f_admin_org,
        project=f_project,
        observation=observation,
        source_type='file',
        source_id='src/app.py',
        citation='L10',
    )

    candidate = _make_candidate(
        f_admin_org,
        f_project,
        status=CandidateStatus.PROPOSED,
        evidence=[{'provider_call_id': 'pc-1', 'provider': 'anthropic', 'model': 'claude-x'}],
        source_observation=observation,
    )

    memory = _make_memory(f_admin_org, f_project, status=MemoryStatus.CONFLICT)

    MemoryLink.objects.create(
        organization=f_admin_org,
        project=f_project,
        memory=memory,
        link_type=LinkType.FILE,
        target='src/app.py',
        label='main module',
    )

    client = _auth_client(f_admin_token, f_admin_org)

    response = client.get('/v1/admin/memory-review/')

    assert response.status_code == 200

    by_id = {item['id']: item for item in response.data['results']}

    candidate_item = by_id[str(candidate.id)]

    assert candidate_item['type'] == 'candidate'

    assert candidate_item['evidence'] == [
        {'provider_call_id': 'pc-1', 'provider': 'anthropic', 'model': 'claude-x'},
    ]

    assert candidate_item['source_observation']['title'] == 'Obs title'

    memory_item = by_id[str(memory.id)]

    assert memory_item['type'] == 'memory'

    assert len(memory_item['citations']) == 1

    assert memory_item['citations'][0]['link_type'] == 'file'


@pytest.mark.django_db
def test_diff_returns_from_and_to_versions(
    f_admin_token: str,
    f_admin_org: Organization,
    f_project: Project,
) -> None:
    memory = _make_memory(f_admin_org, f_project, status=MemoryStatus.CONFLICT)

    _make_version(memory, 1, 'first body')

    _make_version(memory, 2, 'second body')

    client = _auth_client(f_admin_token, f_admin_org)

    response = client.get(
        f'/v1/admin/memory-review/{memory.id}/diff/?from_version=1&to_version=2',
    )

    assert response.status_code == 200

    assert response.data['from']['version'] == 1

    assert response.data['from']['body'] == 'first body'

    assert response.data['to']['version'] == 2

    assert response.data['to']['body'] == 'second body'


@pytest.mark.django_db
def test_diff_requires_review_capability(
    f_reviewer_token: str,
    f_reviewer_org: Organization,
) -> None:
    project = Project.objects.create(organization=f_reviewer_org, name='P', slug='p')

    memory = _make_memory(f_reviewer_org, project, status=MemoryStatus.CONFLICT)

    _make_version(memory, 1, 'b')

    client = _auth_client(f_reviewer_token, f_reviewer_org)

    response = client.get(
        f'/v1/admin/memory-review/{memory.id}/diff/?from_version=1&to_version=1',
    )

    assert response.status_code == 200


@pytest.mark.django_db
def test_action_approve_promotes_candidate(
    f_admin_token: str,
    f_admin_org: Organization,
    f_project: Project,
) -> None:
    candidate = _make_candidate(f_admin_org, f_project, status=CandidateStatus.PROPOSED)

    client = _auth_client(f_admin_token, f_admin_org)

    response = client.post(
        f'/v1/admin/memory-review/{candidate.id}/action/',
        {'action': 'approve', 'reason': 'looks good'},
    )

    assert response.status_code == 200

    candidate.refresh_from_db()

    assert candidate.status == CandidateStatus.PROMOTED

    assert candidate.promoted_memory_id is not None

    assert AuditEvent.objects.filter(
        organization=f_admin_org,
        event_type='MemoryReviewed',
        target_id=str(candidate.id),
        metadata__action='approve',
    ).exists()


@pytest.mark.django_db
def test_action_edit_creates_new_version(
    f_admin_token: str,
    f_admin_org: Organization,
    f_project: Project,
) -> None:
    memory = _make_memory(f_admin_org, f_project, status=MemoryStatus.APPROVED, confidence='0.100')

    _make_version(memory, 1, 'original')

    client = _auth_client(f_admin_token, f_admin_org)

    response = client.post(
        f'/v1/admin/memory-review/{memory.id}/action/',
        {'action': 'edit', 'reason': 'clarify', 'body': 'edited body'},
    )

    assert response.status_code == 200

    memory.refresh_from_db()

    assert memory.body == 'edited body'

    assert memory.current_version == 2

    assert MemoryVersion.objects.filter(memory=memory, version=2, body='edited body').exists()


@pytest.mark.django_db
def test_action_narrow_creates_narrowed_by_link(
    f_admin_token: str,
    f_admin_org: Organization,
    f_project: Project,
) -> None:
    memory = _make_memory(f_admin_org, f_project, status=MemoryStatus.APPROVED, confidence='0.100')

    target = _make_memory(f_admin_org, f_project, status=MemoryStatus.APPROVED, body='general')

    client = _auth_client(f_admin_token, f_admin_org)

    response = client.post(
        f'/v1/admin/memory-review/{memory.id}/action/',
        {
            'action': 'narrow',
            'reason': 'specific case',
            'target_memory_id': str(target.id),
        },
    )

    assert response.status_code == 200

    assert MemoryLink.objects.filter(
        memory=memory,
        link_type=LinkType.NARROWED_BY,
        target=str(target.id),
    ).exists()


@pytest.mark.django_db
def test_action_supersede_creates_link_and_marks_stale(
    f_admin_token: str,
    f_admin_org: Organization,
    f_project: Project,
) -> None:
    memory = _make_memory(f_admin_org, f_project, status=MemoryStatus.APPROVED, confidence='0.100')

    replacement = _make_memory(f_admin_org, f_project, status=MemoryStatus.APPROVED, body='new')

    client = _auth_client(f_admin_token, f_admin_org)

    response = client.post(
        f'/v1/admin/memory-review/{memory.id}/action/',
        {
            'action': 'supersede',
            'reason': 'outdated',
            'target_memory_id': str(replacement.id),
        },
    )

    assert response.status_code == 200

    memory.refresh_from_db()

    assert memory.stale is True

    assert MemoryLink.objects.filter(
        memory=memory,
        link_type=LinkType.SUPERSEDED_BY,
        target=str(replacement.id),
    ).exists()


@pytest.mark.django_db
def test_action_reject_candidate_sets_rejected(
    f_admin_token: str,
    f_admin_org: Organization,
    f_project: Project,
) -> None:
    candidate = _make_candidate(f_admin_org, f_project, status=CandidateStatus.PROPOSED)

    client = _auth_client(f_admin_token, f_admin_org)

    response = client.post(
        f'/v1/admin/memory-review/{candidate.id}/action/',
        {'action': 'reject', 'reason': 'duplicate'},
    )

    assert response.status_code == 200

    candidate.refresh_from_db()

    assert candidate.status == CandidateStatus.REJECTED


@pytest.mark.django_db
def test_action_reject_memory_sets_refuted(
    f_admin_token: str,
    f_admin_org: Organization,
    f_project: Project,
) -> None:
    memory = _make_memory(f_admin_org, f_project, status=MemoryStatus.CONFLICT)

    client = _auth_client(f_admin_token, f_admin_org)

    response = client.post(
        f'/v1/admin/memory-review/{memory.id}/action/',
        {'action': 'reject', 'reason': 'wrong'},
    )

    assert response.status_code == 200

    memory.refresh_from_db()

    assert memory.status == MemoryStatus.REFUTED


@pytest.mark.django_db
def test_action_archive_sets_archived(
    f_admin_token: str,
    f_admin_org: Organization,
    f_project: Project,
) -> None:
    memory = _make_memory(f_admin_org, f_project, status=MemoryStatus.APPROVED, confidence='0.100')

    client = _auth_client(f_admin_token, f_admin_org)

    response = client.post(
        f'/v1/admin/memory-review/{memory.id}/action/',
        {'action': 'archive', 'reason': 'stale topic'},
    )

    assert response.status_code == 200

    memory.refresh_from_db()

    assert memory.status == MemoryStatus.ARCHIVED


@pytest.mark.django_db
def test_action_denied_without_admin_capability(
    f_reviewer_token: str,
    f_reviewer_org: Organization,
) -> None:
    project = Project.objects.create(organization=f_reviewer_org, name='P', slug='p')

    candidate = _make_candidate(f_reviewer_org, project, status=CandidateStatus.PROPOSED)

    client = _auth_client(f_reviewer_token, f_reviewer_org)

    response = client.post(
        f'/v1/admin/memory-review/{candidate.id}/action/',
        {'action': 'approve', 'reason': 'try'},
    )

    assert response.status_code == 403

    candidate.refresh_from_db()

    assert candidate.status == CandidateStatus.PROPOSED


@pytest.mark.django_db
def test_queue_denied_without_review_capability() -> None:
    user = _make_user('nobody')

    org = Organization.objects.create(name='Noacc', slug='noacc')

    identity = _make_identity(user, org)

    role = _make_role_with_capabilities('none', ('observations:read',))

    OrganizationMembership.objects.create(organization=org, identity=identity, role=role)

    from rest_framework.authtoken.models import Token

    token = Token.objects.get_or_create(user=user)[0].key

    client = _auth_client(token, org)

    response = client.get('/v1/admin/memory-review/')

    assert response.status_code == 403


@pytest.mark.django_db
def test_action_is_tenant_scoped(
    f_admin_token: str,
    f_admin_org: Organization,
    f_foreign_org: Organization,
) -> None:
    foreign_project = Project.objects.create(
        organization=f_foreign_org,
        name='FP',
        slug='fp',
    )

    candidate = _make_candidate(f_foreign_org, foreign_project, status=CandidateStatus.PROPOSED)

    client = _auth_client(f_admin_token, f_admin_org)

    response = client.post(
        f'/v1/admin/memory-review/{candidate.id}/action/',
        {'action': 'approve', 'reason': 'cross'},
    )

    assert response.status_code == 404

    candidate.refresh_from_db()

    assert candidate.status == CandidateStatus.PROPOSED


@pytest.mark.django_db
def test_bulk_archive_by_ids(
    f_admin_token: str,
    f_admin_org: Organization,
    f_project: Project,
) -> None:
    m1 = _make_memory(f_admin_org, f_project, status=MemoryStatus.CONFLICT)

    m2 = _make_memory(f_admin_org, f_project, status=MemoryStatus.REFUTED)

    client = _auth_client(f_admin_token, f_admin_org)

    response = client.post(
        '/v1/admin/memory-review/bulk-archive/',
        {'ids': [str(m1.id), str(m2.id)], 'reason': 'cleanup'},
    )

    assert response.status_code == 200

    assert response.data['archived_count'] == 2

    assert set(response.data['archived_ids']) == {str(m1.id), str(m2.id)}

    m1.refresh_from_db()

    m2.refresh_from_db()

    assert m1.status == MemoryStatus.ARCHIVED

    assert m2.status == MemoryStatus.ARCHIVED

    assert (
        AuditEvent.objects.filter(
            organization=f_admin_org,
            event_type='MemoryReviewed',
            metadata__action='archive',
        ).count()
        == 2
    )


@pytest.mark.django_db
def test_bulk_archive_by_confidence_threshold(
    f_admin_token: str,
    f_admin_org: Organization,
    f_project: Project,
) -> None:
    m1 = _make_memory(f_admin_org, f_project, status=MemoryStatus.CONFLICT, confidence='0.100')

    _make_memory(f_admin_org, f_project, status=MemoryStatus.CONFLICT, confidence='0.900')

    client = _auth_client(f_admin_token, f_admin_org)

    response = client.post(
        '/v1/admin/memory-review/bulk-archive/',
        {'confidence__lte': '0.300', 'reason': 'low confidence cleanup'},
    )

    assert response.status_code == 200

    assert response.data['archived_count'] == 1

    assert response.data['archived_ids'] == [str(m1.id)]

    m1.refresh_from_db()

    assert m1.status == MemoryStatus.ARCHIVED


@pytest.mark.django_db
def test_bulk_archive_requires_reason(
    f_admin_token: str,
    f_admin_org: Organization,
    f_project: Project,
) -> None:
    memory = _make_memory(f_admin_org, f_project, status=MemoryStatus.CONFLICT)

    client = _auth_client(f_admin_token, f_admin_org)

    response = client.post(
        '/v1/admin/memory-review/bulk-archive/',
        {'ids': [str(memory.id)]},
    )

    assert response.status_code == 400

    memory.refresh_from_db()

    assert memory.status == MemoryStatus.CONFLICT


@pytest.mark.django_db
def test_bulk_archive_denied_without_admin_capability(
    f_reviewer_token: str,
    f_reviewer_org: Organization,
) -> None:
    project = Project.objects.create(organization=f_reviewer_org, name='P', slug='p')

    memory = _make_memory(f_reviewer_org, project, status=MemoryStatus.CONFLICT)

    client = _auth_client(f_reviewer_token, f_reviewer_org)

    response = client.post(
        '/v1/admin/memory-review/bulk-archive/',
        {'ids': [str(memory.id)], 'reason': 'try'},
    )

    assert response.status_code == 403

    memory.refresh_from_db()

    assert memory.status == MemoryStatus.CONFLICT


@pytest.mark.django_db
def test_action_approbe_requires_reason(
    f_admin_token: str,
    f_admin_org: Organization,
    f_project: Project,
) -> None:
    candidate = _make_candidate(f_admin_org, f_project, status=CandidateStatus.PROPOSED)

    client = _auth_client(f_admin_token, f_admin_org)

    response = client.post(
        f'/v1/admin/memory-review/{candidate.id}/action/',
        {'action': 'approve'},
    )

    assert response.status_code == 400


@pytest.mark.django_db
def test_retrieve_candidate_by_id(
    f_admin_token: str,
    f_admin_org: Organization,
    f_project: Project,
) -> None:
    candidate = _make_candidate(f_admin_org, f_project, status=CandidateStatus.PROPOSED)

    client = _auth_client(f_admin_token, f_admin_org)

    response = client.get(f'/v1/admin/memory-review/{candidate.id}/')

    assert response.status_code == 200

    assert response.data['type'] == 'candidate'

    assert response.data['id'] == str(candidate.id)


@pytest.mark.django_db
def test_retrieve_memory_by_id(
    f_admin_token: str,
    f_admin_org: Organization,
    f_project: Project,
) -> None:
    memory = _make_memory(f_admin_org, f_project, status=MemoryStatus.CONFLICT)

    client = _auth_client(f_admin_token, f_admin_org)

    response = client.get(f'/v1/admin/memory-review/{memory.id}/')

    assert response.status_code == 200

    assert response.data['type'] == 'memory'

    assert response.data['id'] == str(memory.id)


@pytest.mark.django_db
def test_retrieve_unknown_id_returns_404(
    f_admin_token: str,
    f_admin_org: Organization,
) -> None:
    client = _auth_client(f_admin_token, f_admin_org)

    response = client.get('/v1/admin/memory-review/00000000-0000-0000-0000-000000000001/')

    assert response.status_code == 404


@pytest.mark.django_db
def test_retrieve_unknown_id_returns_domain_error_shape(
    f_admin_token: str,
    f_admin_org: Organization,
) -> None:
    client = _auth_client(f_admin_token, f_admin_org)

    response = client.get('/v1/admin/memory-review/00000000-0000-0000-0000-000000000001/')

    assert response.status_code == 404

    assert response.data['code'] == 'not_found'

    assert response.data['error_code'] == 'not_found'


@pytest.mark.django_db
def test_retrieve_foreign_org_item_returns_404(
    f_admin_token: str,
    f_admin_org: Organization,
    f_foreign_org: Organization,
) -> None:
    foreign_project = Project.objects.create(
        organization=f_foreign_org,
        name='FP2',
        slug='fp2',
    )

    candidate = _make_candidate(f_foreign_org, foreign_project, status=CandidateStatus.PROPOSED)

    client = _auth_client(f_admin_token, f_admin_org)

    response = client.get(f'/v1/admin/memory-review/{candidate.id}/')

    assert response.status_code == 404


@pytest.mark.django_db
def test_retrieve_denied_without_review_capability() -> None:
    user = _make_user('noreview')

    org = Organization.objects.create(name='Noreview', slug='noreview')

    identity = _make_identity(user, org)

    role = _make_role_with_capabilities('noreview_role', ('observations:read',))

    OrganizationMembership.objects.create(organization=org, identity=identity, role=role)

    from rest_framework.authtoken.models import Token

    token = Token.objects.get_or_create(user=user)[0].key

    project = Project.objects.create(organization=org, name='NR', slug='nr')

    candidate = _make_candidate(org, project, status=CandidateStatus.PROPOSED)

    client = _auth_client(token, org)

    response = client.get(f'/v1/admin/memory-review/{candidate.id}/')

    assert response.status_code == 403
