from __future__ import annotations

import datetime
import hashlib
import uuid

import pytest
import structlog
from django.contrib.auth.models import User
from django.utils import timezone
from django_celery_outbox.models import CeleryOutbox
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from engram.access.auth_services import external_id_for_user
from engram.access.models import (
    Capability,
    Identity,
    IdentityType,
    OrganizationMembership,
    ProjectGrant,
    Role,
    RoleCapability,
)
from engram.core.models import (
    AuditEvent,
    Memory,
    MemoryStatus,
    MemoryVersion,
    Organization,
    Project,
    RetrievalDocument,
    Team,
    VisibilityScope,
    WorkflowRun,
    WorkflowRunType,
    WorkflowSubjectType,
    WorkflowWork,
    WorkflowWorkDisposition,
    WorkflowWorkResolutionReason,
    WorkflowWorkType,
)
from engram.memory.digest_work import digest_output_identity
from engram.memory.services import weekly_digest_content_hash

_WEEKLY_TASK_NAME = 'engram.memory.generate_weekly_digest_work_v1'


def _make_user(username: str) -> User:
    return User.objects.create_user(username=username, password='test-pass-123')  # noqa: S106


def _make_identity(user: User, organization: Organization) -> Identity:
    identity, _ = Identity.objects.get_or_create(
        organization=organization,
        identity_type=IdentityType.USER,
        external_id=external_id_for_user(user),
        defaults={'display_name': user.get_username()},
    )

    return identity


def _ensure_capability(code: str) -> Capability:
    capability, _ = Capability.objects.get_or_create(
        code=code,
        defaults={'description': code},
    )

    return capability


def _make_role_with_capabilities(code: str, capability_codes: tuple[str, ...]) -> Role:
    role, _ = Role.objects.get_or_create(code=code, defaults={'name': code})

    for cap_code in capability_codes:
        capability = _ensure_capability(cap_code)
        RoleCapability.objects.get_or_create(role=role, capability=capability)

    return role


def _make_client(token: str, organization: Organization) -> APIClient:
    client = APIClient()

    client.credentials(
        HTTP_AUTHORIZATION=f'Token {token}',
        HTTP_X_ENGRAM_ORGANIZATION=str(organization.id),
    )

    return client


def _current_weekly_window(weeks_back: int) -> tuple[datetime.datetime, datetime.datetime]:
    today = timezone.now().date()
    current_monday = today - datetime.timedelta(days=today.isoweekday() - 1)
    anchor_monday = current_monday - datetime.timedelta(weeks=weeks_back)
    tzinfo = timezone.get_current_timezone()
    window_end = datetime.datetime.combine(anchor_monday, datetime.time.min, tzinfo=tzinfo)
    window_start = datetime.datetime.combine(
        anchor_monday - datetime.timedelta(days=7),
        datetime.time.min,
        tzinfo=tzinfo,
    )

    return window_start, window_end


def _make_source_memory(org: Organization, project: Project, created_at: datetime.datetime) -> Memory:
    memory = Memory.objects.create(
        organization=org,
        project=project,
        title='weekly source memory',
        body='source body',
        status=MemoryStatus.APPROVED,
        visibility_scope=VisibilityScope.PROJECT,
    )
    MemoryVersion.objects.create(
        organization=org,
        project=project,
        memory=memory,
        version=memory.current_version,
        body='source body',
        content_hash=hashlib.sha256(b'source body').hexdigest(),
    )
    Memory.objects.filter(id=memory.id).update(created_at=created_at, updated_at=created_at)

    return memory


def _make_weekly_work(
    org: Organization,
    project: Project,
    *,
    occurrence_key: str = 'weekly:2026-W28',
) -> WorkflowWork:
    snapshot: dict[str, object] = {
        'schema': 'weekly_digest_input/v1',
        'project_id': str(project.id),
        'team_id': None,
        'schedule_key': occurrence_key,
        'window_start': '2026-05-11T00:00:00Z',
        'window_end': '2026-05-18T00:00:00Z',
        'visibility_policy': 'digest_visibility/v1',
        'allowed_team_ids': [],
        'output_visibility_scope': 'project',
        'output_team_id': None,
        'input_digest': 'a' * 64,
        'changes': [],
    }

    return WorkflowWork.objects.create(
        organization=org,
        project=project,
        work_type=WorkflowWorkType.WEEKLY_DIGEST,
        subject_type=WorkflowSubjectType.PROJECT,
        subject_id=project.id,
        contract_version=1,
        occurrence_key=occurrence_key,
        input_fingerprint='0' * 64,
        input_snapshot=snapshot,
    )


def _attach_proven_digest(
    org: Organization,
    project: Project,
    work: WorkflowWork,
    *,
    digest_kind: str = 'weekly_structured',
) -> Memory:
    WorkflowWork.objects.filter(id=work.id).update(
        disposition=WorkflowWorkDisposition.COMPLETE,
        resolution_reason=WorkflowWorkResolutionReason.SUCCEEDED,
        resolved_at=timezone.now(),
    )
    work.refresh_from_db()
    snapshot = work.input_snapshot

    memory = Memory.objects.create(
        organization=org,
        project=project,
        title='Weekly Structured Digest 2026-06-23 to 2026-06-30',
        body='digest body',
        status=MemoryStatus.APPROVED,
        visibility_scope=VisibilityScope.PROJECT,
        metadata={
            'kind': 'digest',
            'digest_kind': digest_kind,
            'ready': False,
            'reviewed_at': None,
            'digest_visibility': {
                'schema': 'digest_visibility/v1',
                'workflow_work_id': str(work.id),
                'input_digest': snapshot['input_digest'],
                'output_identity': digest_output_identity(work),
                'allowed_team_ids': list(snapshot['allowed_team_ids']),
                'output_visibility_scope': snapshot['output_visibility_scope'],
                'output_team_id': snapshot['output_team_id'],
            },
        },
    )
    version = MemoryVersion.objects.create(
        organization=org,
        project=project,
        memory=memory,
        version=1,
        body='digest body',
        content_hash=hashlib.sha256(b'digest body').hexdigest(),
    )
    RetrievalDocument.objects.create(
        organization=org,
        project=project,
        memory=memory,
        memory_version=version,
        visibility_scope=VisibilityScope.PROJECT,
        full_text='digest body',
    )

    return memory


def _make_unproven_digest(
    org: Organization,
    project: Project,
    *,
    content_hash: str = 'legacy-hash',
    digest_kind: str = 'weekly_structured',
    ready: bool = False,
) -> Memory:
    memory = Memory.objects.create(
        organization=org,
        project=project,
        title='Weekly Structured Digest 2026-06-23 to 2026-06-30',
        body='Structured weekly digest.',
        status=MemoryStatus.APPROVED,
        visibility_scope=VisibilityScope.PROJECT,
        metadata={
            'kind': 'digest',
            'digest_kind': digest_kind,
            'window_start': '2026-06-23T00:00:00+00:00',
            'window_end': '2026-06-30T00:00:00+00:00',
            'window_days': 7,
            'memory_changes': {
                'refuted': [],
                'retired': [],
                'superseded': [],
                'merged': [],
                'added': [{'id': str(uuid.uuid4()), 'title': 'mem', 'at': '2026-06-25T12:00:00+00:00'}],
            },
            'counts': {'refuted': 0, 'retired': 0, 'superseded': 0, 'merged': 0, 'added': 1},
            'content_hash': content_hash,
            'ready': ready,
            'reviewed_at': None,
        },
    )
    version = MemoryVersion.objects.create(
        organization=org,
        project=project,
        memory=memory,
        version=1,
        body='Structured weekly digest.',
        content_hash=hashlib.sha256(b'Structured weekly digest.').hexdigest(),
    )
    RetrievalDocument.objects.create(
        organization=org,
        project=project,
        memory=memory,
        memory_version=version,
        visibility_scope=VisibilityScope.PROJECT,
        full_text='Structured weekly digest.',
    )

    return memory


@pytest.fixture
def f_org() -> Organization:
    return Organization.objects.create(name='Digest View Org', slug='digest-view-org')


@pytest.fixture
def f_project(f_org: Organization) -> Project:
    return Project.objects.create(
        organization=f_org,
        name='digest-view-project',
        slug='digest-view-project',
    )


@pytest.fixture
def f_other_org() -> Organization:
    return Organization.objects.create(name='Other Digest Org', slug='other-digest-org')


@pytest.fixture
def f_other_project(f_other_org: Organization) -> Project:
    return Project.objects.create(
        organization=f_other_org,
        name='other-digest-project',
        slug='other-digest-project',
    )


@pytest.fixture
def f_read_client(f_org: Organization, f_project: Project) -> APIClient:
    user = _make_user('digest-read-user')
    identity = _make_identity(user, f_org)
    role = _make_role_with_capabilities('digest_read_role', ('memories:read',))
    OrganizationMembership.objects.create(organization=f_org, identity=identity, role=role)
    ProjectGrant.objects.create(organization=f_org, project=f_project, identity=identity, role=role)
    token = Token.objects.create(user=user).key

    return _make_client(token, f_org)


@pytest.fixture
def f_review_client(f_org: Organization, f_project: Project) -> APIClient:
    user = _make_user('digest-review-user')
    identity = _make_identity(user, f_org)
    role = _make_role_with_capabilities('digest_review_role', ('memories:read', 'memories:review'))
    OrganizationMembership.objects.create(organization=f_org, identity=identity, role=role)
    ProjectGrant.objects.create(organization=f_org, project=f_project, identity=identity, role=role)
    token = Token.objects.create(user=user).key

    return _make_client(token, f_org)


@pytest.fixture
def f_no_cap_client(f_org: Organization) -> APIClient:
    user = _make_user('digest-no-cap-user')
    identity = _make_identity(user, f_org)
    role = _make_role_with_capabilities('digest_no_cap_role', ())
    OrganizationMembership.objects.create(organization=f_org, identity=identity, role=role)
    token = Token.objects.create(user=user).key

    return _make_client(token, f_org)


@pytest.mark.django_db
def test_get_weekly_current_enqueues_work_and_initial_signal_built_false(
    f_read_client: APIClient,
    f_org: Organization,
    f_project: Project,
) -> None:
    window_start, _window_end = _current_weekly_window(0)
    _make_source_memory(f_org, f_project, window_start + datetime.timedelta(days=1))

    response = f_read_client.get(
        '/v1/admin/digests/weekly',
        {'project_id': str(f_project.id)},
    )

    assert response.status_code == 200

    assert response.data['built'] is False

    assert response.data['digest_memory_id'] is None

    work = WorkflowWork.objects.get(
        organization=f_org,
        project=f_project,
        work_type=WorkflowWorkType.WEEKLY_DIGEST,
    )

    assert work.disposition == WorkflowWorkDisposition.REQUIRED

    assert WorkflowRun.objects.filter(run_type=WorkflowRunType.WEEKLY_DIGEST).count() == 0

    outbox = CeleryOutbox.objects.get()

    assert outbox.task_name == _WEEKLY_TASK_NAME

    assert outbox.args == [str(work.id)]

    assert outbox.kwargs == {}

    assert outbox.task_id == f'workflow-work:{work.id}'


@pytest.mark.django_db
def test_get_weekly_current_returns_built_true_only_for_proven_output(
    f_read_client: APIClient,
    f_org: Organization,
    f_project: Project,
) -> None:
    first = f_read_client.get(
        '/v1/admin/digests/weekly',
        {'project_id': str(f_project.id)},
    )

    assert first.status_code == 200

    assert first.data['built'] is False

    work = WorkflowWork.objects.get(work_type=WorkflowWorkType.WEEKLY_DIGEST)

    proven = _attach_proven_digest(f_org, f_project, work)

    second = f_read_client.get(
        '/v1/admin/digests/weekly',
        {'project_id': str(f_project.id)},
    )

    assert second.status_code == 200

    assert second.data['built'] is True

    assert second.data['digest_memory_id'] == str(proven.id)

    assert WorkflowWork.objects.filter(work_type=WorkflowWorkType.WEEKLY_DIGEST).count() == 1


@pytest.mark.django_db
def test_get_weekly_current_denies_team_outside_caller_scope_without_work(
    f_read_client: APIClient,
    f_org: Organization,
    f_project: Project,
) -> None:
    team = Team.objects.create(organization=f_org, name='Unscoped', slug='unscoped')

    response = f_read_client.get(
        '/v1/admin/digests/weekly',
        {'project_id': str(f_project.id), 'team_id': str(team.id)},
    )

    assert response.status_code in (400, 403, 404)

    assert WorkflowWork.objects.count() == 0

    assert CeleryOutbox.objects.count() == 0


@pytest.mark.django_db
def test_get_weekly_historical_read_only_quarantines_unproven_digest(
    f_read_client: APIClient,
    f_org: Organization,
    f_project: Project,
) -> None:
    window_start, window_end = _current_weekly_window(3)
    content_hash = weekly_digest_content_hash(f_project.id, window_start, window_end, None)
    digest = _make_unproven_digest(f_org, f_project, content_hash=content_hash)

    response = f_read_client.get(
        '/v1/admin/digests/weekly',
        {'project_id': str(f_project.id), 'weeks_back': '3'},
    )

    assert response.status_code == 200

    assert response.data['built'] is False

    assert response.data['digest_memory_id'] is None

    assert WorkflowWork.objects.count() == 0

    assert CeleryOutbox.objects.count() == 0

    digest.refresh_from_db()

    assert digest.metadata['ready'] is False


@pytest.mark.django_db
def test_get_weekly_digest_past_week_never_built_returns_not_built_without_writing(
    f_read_client: APIClient,
    f_org: Organization,
    f_project: Project,
) -> None:
    response = f_read_client.get(
        '/v1/admin/digests/weekly',
        {'project_id': str(f_project.id), 'weeks_back': '3'},
    )

    assert response.status_code == 200

    assert response.data['built'] is False

    assert response.data['digest_memory_id'] is None

    assert response.data['window_start'] is not None

    assert Memory.objects.filter(organization=f_org).count() == 0

    assert WorkflowWork.objects.count() == 0

    assert CeleryOutbox.objects.count() == 0


@pytest.mark.django_db
def test_get_weekly_digest_rejects_invalid_team_id(
    f_read_client: APIClient,
    f_org: Organization,
    f_project: Project,
) -> None:
    response = f_read_client.get(
        '/v1/admin/digests/weekly',
        {'project_id': str(f_project.id), 'team_id': 'not-a-uuid'},
    )

    assert response.status_code == 400


@pytest.mark.django_db
def test_get_weekly_digest_requires_project_id(
    f_read_client: APIClient,
    f_org: Organization,
) -> None:
    response = f_read_client.get('/v1/admin/digests/weekly')

    assert response.status_code == 400


@pytest.mark.django_db
def test_get_weekly_digest_requires_memories_read_capability(
    f_no_cap_client: APIClient,
    f_project: Project,
) -> None:
    response = f_no_cap_client.get(
        '/v1/admin/digests/weekly',
        {'project_id': str(f_project.id)},
    )

    assert response.status_code == 403


@pytest.mark.django_db
def test_get_weekly_digest_requires_authentication(
    f_project: Project,
) -> None:
    client = APIClient()

    response = client.get(
        '/v1/admin/digests/weekly',
        {'project_id': str(f_project.id)},
    )

    assert response.status_code == 401


@pytest.mark.django_db
def test_get_weekly_digest_returns_404_for_unknown_project(
    f_read_client: APIClient,
    f_org: Organization,
) -> None:
    response = f_read_client.get(
        '/v1/admin/digests/weekly',
        {'project_id': str(uuid.uuid4())},
    )

    assert response.status_code == 404


@pytest.mark.django_db
def test_post_digest_review_flips_proven_digest_ready_to_true(
    f_review_client: APIClient,
    f_org: Organization,
    f_project: Project,
) -> None:
    work = _make_weekly_work(f_org, f_project)

    digest = _attach_proven_digest(f_org, f_project, work)

    response = f_review_client.post(f'/v1/admin/digests/{digest.id}/review')

    assert response.status_code == 200

    assert response.data['memory_id'] == str(digest.id)

    assert response.data['reviewed'] is True

    assert response.data['ready'] is True

    digest.refresh_from_db()

    assert digest.metadata['ready'] is True

    assert digest.metadata['reviewed_at'] is not None


@pytest.mark.django_db
def test_post_digest_review_unproven_digest_not_found_and_not_mutated(
    f_review_client: APIClient,
    f_org: Organization,
    f_project: Project,
) -> None:
    digest = _make_unproven_digest(f_org, f_project)

    response = f_review_client.post(f'/v1/admin/digests/{digest.id}/review')

    assert response.status_code == 404

    assert response.data['code'] == 'digest_not_found'

    digest.refresh_from_db()

    assert digest.metadata['ready'] is False

    assert digest.metadata['reviewed_at'] is None

    assert not AuditEvent.objects.filter(
        organization=f_org,
        event_type='DigestReviewed',
        target_id=str(digest.id),
    ).exists()


@pytest.mark.django_db
def test_post_digest_review_writes_audit_event(
    f_review_client: APIClient,
    f_org: Organization,
    f_project: Project,
) -> None:
    work = _make_weekly_work(f_org, f_project)
    digest = _attach_proven_digest(f_org, f_project, work)

    f_review_client.post(f'/v1/admin/digests/{digest.id}/review')

    event = AuditEvent.objects.filter(
        organization=f_org,
        event_type='DigestReviewed',
        target_id=str(digest.id),
    ).first()

    assert event is not None

    assert event.target_type == 'memory'


@pytest.mark.django_db
def test_post_digest_review_accepts_daily_digest_kind(
    f_review_client: APIClient,
    f_org: Organization,
    f_project: Project,
) -> None:
    work = _make_weekly_work(f_org, f_project)
    digest = _attach_proven_digest(f_org, f_project, work, digest_kind='daily_structured')

    response = f_review_client.post(
        f'/v1/admin/digests/{digest.id}/review',
        {'digest_kind': 'daily_structured'},
        format='json',
    )

    assert response.status_code == 200

    assert response.data['ready'] is True

    digest.refresh_from_db()

    assert digest.metadata['digest_kind'] == 'daily_structured'

    event = AuditEvent.objects.get(
        organization=f_org,
        event_type='DigestReviewed',
        target_id=str(digest.id),
    )

    assert event.metadata['digest_kind'] == 'daily_structured'


@pytest.mark.django_db
def test_post_digest_review_logs_digest_reviewed_event(
    f_review_client: APIClient,
    f_org: Organization,
    f_project: Project,
) -> None:
    work = _make_weekly_work(f_org, f_project)
    digest = _attach_proven_digest(f_org, f_project, work)

    with structlog.testing.capture_logs() as captured_logs:
        f_review_client.post(f'/v1/admin/digests/{digest.id}/review')

    events = [entry for entry in captured_logs if entry['event'] == 'digest_reviewed']

    assert len(events) == 1

    assert events[0]['organization_id'] == str(f_org.id)

    assert events[0]['memory_id'] == str(digest.id)

    assert events[0]['digest_kind'] == 'weekly_structured'


@pytest.mark.django_db
def test_post_digest_review_requires_memories_review_capability(
    f_read_client: APIClient,
    f_org: Organization,
    f_project: Project,
) -> None:
    digest = _make_unproven_digest(f_org, f_project)

    response = f_read_client.post(f'/v1/admin/digests/{digest.id}/review')

    assert response.status_code == 403


@pytest.mark.django_db
def test_post_digest_review_requires_authentication(
    f_org: Organization,
    f_project: Project,
) -> None:
    digest = _make_unproven_digest(f_org, f_project)

    client = APIClient()

    response = client.post(f'/v1/admin/digests/{digest.id}/review')

    assert response.status_code == 401


@pytest.mark.django_db
def test_post_digest_review_tenant_isolation(
    f_review_client: APIClient,
    f_other_org: Organization,
    f_other_project: Project,
) -> None:
    other_digest = _make_unproven_digest(f_other_org, f_other_project)

    response = f_review_client.post(f'/v1/admin/digests/{other_digest.id}/review')

    assert response.status_code == 404


@pytest.mark.django_db
def test_post_digest_review_returns_404_for_nonexistent_memory(
    f_review_client: APIClient,
    f_org: Organization,
) -> None:
    response = f_review_client.post(f'/v1/admin/digests/{uuid.uuid4()}/review')

    assert response.status_code == 404


@pytest.mark.django_db
def test_post_digest_review_returns_404_for_non_digest_memory(
    f_review_client: APIClient,
    f_org: Organization,
    f_project: Project,
) -> None:
    plain_memory = Memory.objects.create(
        organization=f_org,
        project=f_project,
        title='plain memory',
        body='body',
        status=MemoryStatus.APPROVED,
    )

    response = f_review_client.post(f'/v1/admin/digests/{plain_memory.id}/review')

    assert response.status_code == 404


@pytest.mark.django_db
def test_post_digest_review_404_uses_domain_error_shape(
    f_review_client: APIClient,
    f_org: Organization,
) -> None:
    response = f_review_client.post(f'/v1/admin/digests/{uuid.uuid4()}/review')

    assert response.status_code == 404

    assert response.data['code'] == 'digest_not_found'

    assert response.data['error_code'] == 'digest_not_found'


@pytest.mark.django_db
def test_post_digest_review_digest_kind_mismatch_returns_404(
    f_review_client: APIClient,
    f_org: Organization,
    f_project: Project,
) -> None:
    digest = _make_unproven_digest(f_org, f_project, digest_kind='weekly_structured')

    response = f_review_client.post(
        f'/v1/admin/digests/{digest.id}/review',
        {'digest_kind': 'daily_structured'},
        format='json',
    )

    assert response.status_code == 404


@pytest.mark.django_db
def test_post_digest_review_rejects_invalid_digest_kind(
    f_review_client: APIClient,
    f_org: Organization,
    f_project: Project,
) -> None:
    digest = _make_unproven_digest(f_org, f_project)

    response = f_review_client.post(
        f'/v1/admin/digests/{digest.id}/review',
        {'digest_kind': 'monthly_structured'},
        format='json',
    )

    assert response.status_code == 400
