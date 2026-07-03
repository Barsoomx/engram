from __future__ import annotations

import threading
import time
import uuid
from unittest import mock

import pytest
import structlog
from django.db import connection
from django.test.utils import CaptureQueriesContext

from engram.access.models import Capability, Identity, IdentityType, Role
from engram.access.services import EffectiveScope
from engram.console.exceptions import (
    MemberAlreadyInvitedError,
    ProjectSlugTakenError,
    TeamSlugTakenError,
)
from engram.console.services import (
    MemoryReviewError,
    _lock_candidate_or_404,
    _lock_memory_or_404,
    activate_member,
    approve_memory_candidate,
    archive_memory,
    archive_project,
    archive_team,
    audit_admin_action,
    create_project,
    create_team,
    edit_memory_body,
    invite_member,
    issue_api_key,
    narrow_memory,
    reject_review_item,
    revoke_api_key,
    supersede_memory,
)
from engram.context.services import authorized_retrieval_documents
from engram.core.models import (
    CandidateStatus,
    Memory,
    MemoryCandidate,
    MemoryStatus,
    Organization,
    Project,
    RetrievalDocument,
    VisibilityScope,
)

postgres_only = pytest.mark.skipif(
    connection.vendor != 'postgresql',
    reason='select_for_update FOR UPDATE locking can only be observed against postgres',
)


@pytest.fixture
def f_organization() -> Organization:
    return Organization.objects.create(name='Acme', slug='acme')


@pytest.fixture
def f_foreign_organization() -> Organization:
    return Organization.objects.create(name='Globex', slug='globex')


@pytest.fixture
def f_project(f_organization: Organization) -> Project:
    return Project.objects.create(organization=f_organization, name='Eng', slug='eng')


@pytest.fixture
def f_actor_identity(f_organization: Organization) -> Identity:
    return Identity.objects.create(
        organization=f_organization,
        identity_type=IdentityType.USER,
        external_id='actor-1',
        display_name='Actor',
    )


@pytest.fixture
def f_role() -> Role:
    role, _ = Role.objects.get_or_create(code='developer', defaults={'name': 'developer'})

    return role


def _make_candidate(
    organization: Organization,
    project: Project,
    *,
    status: str = CandidateStatus.PROPOSED,
) -> MemoryCandidate:
    counter = MemoryCandidate.objects.count()

    return MemoryCandidate.objects.create(
        organization=organization,
        project=project,
        title=f'Candidate {counter}',
        body=f'Body {counter}',
        status=status,
        visibility_scope=VisibilityScope.PROJECT,
        content_hash=f'hash-c-{counter}',
        confidence='0.500',
    )


def _make_memory(
    organization: Organization,
    project: Project,
    *,
    status: str = MemoryStatus.CONFLICT,
) -> Memory:
    counter = Memory.objects.count()

    return Memory.objects.create(
        organization=organization,
        project=project,
        title=f'Memory {counter}',
        body='memory body',
        status=status,
        visibility_scope=VisibilityScope.PROJECT,
        confidence='0.500',
    )


def _read_scope(organization: Organization, project: Project) -> EffectiveScope:
    return EffectiveScope(
        organization_id=organization.id,
        identity_id=uuid.uuid4(),
        api_key_id=uuid.uuid4(),
        project_ids=(project.id,),
        team_ids=(),
        capabilities=(),
        actor_type='user',
        actor_id='reader',
        project_bound=True,
    )


def _select_sql(queries: CaptureQueriesContext, table_fragment: str) -> str:
    return next(
        query['sql']
        for query in queries.captured_queries
        if table_fragment in query['sql'].lower() and query['sql'].strip().upper().startswith('SELECT')
    )


@pytest.mark.django_db
def test_lock_candidate_or_404_returns_candidate_without_join(
    f_organization: Organization,
    f_project: Project,
) -> None:
    candidate = _make_candidate(f_organization, f_project)

    with CaptureQueriesContext(connection) as queries:
        locked = _lock_candidate_or_404(f_organization, candidate.id)

    sql = _select_sql(queries, 'core_memorycandidate')

    assert locked.id == candidate.id

    assert 'JOIN' not in sql.upper()


@pytest.mark.django_db
def test_lock_candidate_or_404_raises_not_found_for_foreign_organization(
    f_organization: Organization,
    f_foreign_organization: Organization,
    f_project: Project,
) -> None:
    candidate = _make_candidate(f_organization, f_project)

    with pytest.raises(MemoryReviewError) as error:
        _lock_candidate_or_404(f_foreign_organization, candidate.id)

    assert error.value.status == 404


@pytest.mark.django_db
def test_lock_candidate_or_404_raises_not_found_for_missing_id(
    f_organization: Organization,
) -> None:
    with pytest.raises(MemoryReviewError) as error:
        _lock_candidate_or_404(f_organization, uuid.uuid4())

    assert error.value.status == 404


@pytest.mark.django_db
def test_lock_memory_or_404_returns_memory_without_join(
    f_organization: Organization,
    f_project: Project,
) -> None:
    memory = _make_memory(f_organization, f_project)

    with CaptureQueriesContext(connection) as queries:
        locked = _lock_memory_or_404(f_organization, memory.id)

    sql = _select_sql(queries, 'core_memory"')

    assert locked.id == memory.id

    assert 'JOIN' not in sql.upper()


@pytest.mark.django_db
def test_lock_memory_or_404_raises_not_found_for_foreign_organization(
    f_organization: Organization,
    f_foreign_organization: Organization,
    f_project: Project,
) -> None:
    memory = _make_memory(f_organization, f_project)

    with pytest.raises(MemoryReviewError) as error:
        _lock_memory_or_404(f_foreign_organization, memory.id)

    assert error.value.status == 404


@pytest.mark.django_db
def test_lock_memory_or_404_raises_not_found_for_missing_id(
    f_organization: Organization,
) -> None:
    with pytest.raises(MemoryReviewError) as error:
        _lock_memory_or_404(f_organization, uuid.uuid4())

    assert error.value.status == 404


@pytest.mark.django_db
def test_approve_memory_candidate_promotes_candidate(
    f_organization: Organization,
    f_project: Project,
    f_actor_identity: Identity,
) -> None:
    candidate = _make_candidate(f_organization, f_project)

    memory = approve_memory_candidate(f_organization, f_actor_identity, candidate, 'reason')

    candidate.refresh_from_db()

    assert candidate.status == CandidateStatus.PROMOTED

    assert candidate.promoted_memory_id == memory.id


@pytest.mark.django_db
def test_approve_memory_candidate_creates_indexed_retrieval_document(
    f_organization: Organization,
    f_project: Project,
    f_actor_identity: Identity,
) -> None:
    candidate = _make_candidate(f_organization, f_project)

    memory = approve_memory_candidate(f_organization, f_actor_identity, candidate, 'reason')

    document = RetrievalDocument.objects.get(memory=memory)

    assert document.full_text.startswith(memory.title)

    authorized = authorized_retrieval_documents(f_organization, f_project, _read_scope(f_organization, f_project))

    assert memory.id in [authorized_document.memory_id for authorized_document in authorized]


@pytest.mark.django_db
def test_edit_memory_body_creates_new_version(
    f_organization: Organization,
    f_project: Project,
    f_actor_identity: Identity,
) -> None:
    memory = _make_memory(f_organization, f_project)

    version = edit_memory_body(f_organization, f_actor_identity, memory, 'new body', 'reason')

    memory.refresh_from_db()

    assert memory.body == 'new body'

    assert memory.current_version == version.version


@pytest.mark.django_db
def test_edit_memory_body_reindexes_retrieval_document(
    f_organization: Organization,
    f_project: Project,
    f_actor_identity: Identity,
) -> None:
    memory = _make_memory(f_organization, f_project, status=MemoryStatus.APPROVED)

    version = edit_memory_body(f_organization, f_actor_identity, memory, 'updated body text', 'reason')

    document = RetrievalDocument.objects.get(memory_version=version)

    assert 'updated body text' in document.full_text


@pytest.mark.django_db
def test_narrow_memory_creates_link(
    f_organization: Organization,
    f_project: Project,
    f_actor_identity: Identity,
) -> None:
    memory = _make_memory(f_organization, f_project)

    target = _make_memory(f_organization, f_project)

    link = narrow_memory(f_organization, f_actor_identity, memory, target.id, 'reason')

    assert link.target == str(target.id)


@pytest.mark.django_db
def test_supersede_memory_marks_stale_and_creates_link(
    f_organization: Organization,
    f_project: Project,
    f_actor_identity: Identity,
) -> None:
    memory = _make_memory(f_organization, f_project)

    target = _make_memory(f_organization, f_project)

    link = supersede_memory(f_organization, f_actor_identity, memory, target.id, 'reason')

    memory.refresh_from_db()

    assert memory.stale is True

    assert link.target == str(target.id)


@pytest.mark.django_db
def test_supersede_memory_marks_loser_retrieval_document_stale(
    f_organization: Organization,
    f_project: Project,
    f_actor_identity: Identity,
) -> None:
    memory = approve_memory_candidate(f_organization, f_actor_identity, _make_candidate(f_organization, f_project), 'r')

    target = approve_memory_candidate(f_organization, f_actor_identity, _make_candidate(f_organization, f_project), 'r')

    supersede_memory(f_organization, f_actor_identity, memory, target.id, 'reason')

    document = RetrievalDocument.objects.get(memory=memory)

    assert document.stale is True


@pytest.mark.django_db
def test_reject_review_item_rejects_candidate(
    f_organization: Organization,
    f_project: Project,
    f_actor_identity: Identity,
) -> None:
    candidate = _make_candidate(f_organization, f_project)

    reject_review_item(f_organization, f_actor_identity, candidate, 'reason')

    candidate.refresh_from_db()

    assert candidate.status == CandidateStatus.REJECTED


@pytest.mark.django_db
def test_reject_review_item_refutes_memory(
    f_organization: Organization,
    f_project: Project,
    f_actor_identity: Identity,
) -> None:
    memory = _make_memory(f_organization, f_project)

    reject_review_item(f_organization, f_actor_identity, memory, 'reason')

    memory.refresh_from_db()

    assert memory.status == MemoryStatus.REFUTED


@pytest.mark.django_db
def test_reject_review_item_refutes_memory_and_syncs_retrieval_document(
    f_organization: Organization,
    f_project: Project,
    f_actor_identity: Identity,
) -> None:
    memory = approve_memory_candidate(f_organization, f_actor_identity, _make_candidate(f_organization, f_project), 'r')

    reject_review_item(f_organization, f_actor_identity, memory, 'reason')

    memory.refresh_from_db()

    document = RetrievalDocument.objects.get(memory=memory)

    assert memory.refuted is True

    assert document.refuted is True

    authorized = authorized_retrieval_documents(f_organization, f_project, _read_scope(f_organization, f_project))

    assert memory.id not in [authorized_document.memory_id for authorized_document in authorized]


@pytest.mark.django_db
def test_archive_memory_sets_archived(
    f_organization: Organization,
    f_project: Project,
    f_actor_identity: Identity,
) -> None:
    memory = _make_memory(f_organization, f_project)

    archive_memory(f_organization, f_actor_identity, memory, 'reason')

    memory.refresh_from_db()

    assert memory.status == MemoryStatus.ARCHIVED


@pytest.mark.django_db
@postgres_only
def test_approve_memory_candidate_locks_candidate_row_for_update(
    f_organization: Organization,
    f_project: Project,
    f_actor_identity: Identity,
) -> None:
    candidate = _make_candidate(f_organization, f_project)

    with CaptureQueriesContext(connection) as queries:
        approve_memory_candidate(f_organization, f_actor_identity, candidate, 'reason')

    sql = _select_sql(queries, 'core_memorycandidate')

    assert 'FOR UPDATE' in sql.upper()


@pytest.mark.django_db
@postgres_only
def test_edit_memory_body_locks_memory_row_for_update(
    f_organization: Organization,
    f_project: Project,
    f_actor_identity: Identity,
) -> None:
    memory = _make_memory(f_organization, f_project)

    with CaptureQueriesContext(connection) as queries:
        edit_memory_body(f_organization, f_actor_identity, memory, 'new body', 'reason')

    sql = _select_sql(queries, 'core_memory"')

    assert 'FOR UPDATE' in sql.upper()


@pytest.mark.django_db
@postgres_only
def test_narrow_memory_locks_memory_row_for_update(
    f_organization: Organization,
    f_project: Project,
    f_actor_identity: Identity,
) -> None:
    memory = _make_memory(f_organization, f_project)

    target = _make_memory(f_organization, f_project)

    with CaptureQueriesContext(connection) as queries:
        narrow_memory(f_organization, f_actor_identity, memory, target.id, 'reason')

    sql = _select_sql(queries, 'core_memory"')

    assert 'FOR UPDATE' in sql.upper()


@pytest.mark.django_db
@postgres_only
def test_supersede_memory_locks_memory_row_for_update(
    f_organization: Organization,
    f_project: Project,
    f_actor_identity: Identity,
) -> None:
    memory = _make_memory(f_organization, f_project)

    target = _make_memory(f_organization, f_project)

    with CaptureQueriesContext(connection) as queries:
        supersede_memory(f_organization, f_actor_identity, memory, target.id, 'reason')

    sql = _select_sql(queries, 'core_memory"')

    assert 'FOR UPDATE' in sql.upper()


@pytest.mark.django_db
@postgres_only
def test_reject_review_item_locks_candidate_row_for_update(
    f_organization: Organization,
    f_project: Project,
    f_actor_identity: Identity,
) -> None:
    candidate = _make_candidate(f_organization, f_project)

    with CaptureQueriesContext(connection) as queries:
        reject_review_item(f_organization, f_actor_identity, candidate, 'reason')

    sql = _select_sql(queries, 'core_memorycandidate')

    assert 'FOR UPDATE' in sql.upper()


@pytest.mark.django_db
@postgres_only
def test_reject_review_item_locks_memory_row_for_update(
    f_organization: Organization,
    f_project: Project,
    f_actor_identity: Identity,
) -> None:
    memory = _make_memory(f_organization, f_project)

    with CaptureQueriesContext(connection) as queries:
        reject_review_item(f_organization, f_actor_identity, memory, 'reason')

    sql = _select_sql(queries, 'core_memory"')

    assert 'FOR UPDATE' in sql.upper()


@pytest.mark.django_db
@postgres_only
def test_archive_memory_locks_memory_row_for_update(
    f_organization: Organization,
    f_project: Project,
    f_actor_identity: Identity,
) -> None:
    memory = _make_memory(f_organization, f_project)

    with CaptureQueriesContext(connection) as queries:
        archive_memory(f_organization, f_actor_identity, memory, 'reason')

    sql = _select_sql(queries, 'core_memory"')

    assert 'FOR UPDATE' in sql.upper()


@pytest.mark.django_db
def test_create_team_raises_team_slug_taken_on_duplicate_slug(
    f_organization: Organization,
) -> None:
    create_team(organization=f_organization, name='Eng', slug='eng')

    with pytest.raises(TeamSlugTakenError) as error:
        create_team(organization=f_organization, name='Eng Two', slug='eng')

    assert error.value.error_code == 'team_slug_taken'


@pytest.mark.django_db
def test_create_project_raises_project_slug_taken_on_duplicate_slug(
    f_organization: Organization,
) -> None:
    create_project(organization=f_organization, name='Eng', slug='eng')

    with pytest.raises(ProjectSlugTakenError) as error:
        create_project(organization=f_organization, name='Eng Two', slug='eng')

    assert error.value.error_code == 'project_slug_taken'


@pytest.mark.django_db
def test_invite_member_raises_member_already_invited_on_duplicate_external_id(
    f_organization: Organization,
    f_role: Role,
) -> None:
    invite_member(
        organization=f_organization,
        external_id='dup-user',
        display_name='Dup User',
        email='dup@example.com',
        role=f_role,
    )

    with pytest.raises(MemberAlreadyInvitedError) as error:
        invite_member(
            organization=f_organization,
            external_id='dup-user',
            display_name='Dup User Two',
            email='dup2@example.com',
            role=f_role,
        )

    assert error.value.error_code == 'member_already_invited'


@pytest.mark.django_db
def test_create_team_and_archive_team_log_events(
    f_organization: Organization,
) -> None:
    with structlog.testing.capture_logs() as captured_logs:
        team = create_team(organization=f_organization, name='Eng', slug='eng')
        archive_team(team)

    created = [entry for entry in captured_logs if entry['event'] == 'team_created']

    assert len(created) == 1

    assert created[0]['organization_id'] == str(f_organization.id)

    assert created[0]['team_id'] == str(team.id)

    assert created[0]['slug'] == 'eng'

    archived = [entry for entry in captured_logs if entry['event'] == 'team_archived']

    assert len(archived) == 1

    assert archived[0]['team_id'] == str(team.id)


@pytest.mark.django_db
def test_create_project_and_archive_project_log_events(
    f_organization: Organization,
) -> None:
    with structlog.testing.capture_logs() as captured_logs:
        project = create_project(organization=f_organization, name='Eng', slug='eng')
        archive_project(project)

    created = [entry for entry in captured_logs if entry['event'] == 'project_created']

    assert len(created) == 1

    assert created[0]['organization_id'] == str(f_organization.id)

    assert created[0]['project_id'] == str(project.id)

    assert created[0]['slug'] == 'eng'

    archived = [entry for entry in captured_logs if entry['event'] == 'project_archived']

    assert len(archived) == 1

    assert archived[0]['project_id'] == str(project.id)


@pytest.mark.django_db
def test_invite_member_and_activate_member_log_events(
    f_organization: Organization,
    f_actor_identity: Identity,
    f_role: Role,
) -> None:
    with structlog.testing.capture_logs() as captured_logs:
        membership = invite_member(
            organization=f_organization,
            external_id='invitee-1',
            display_name='Invitee',
            email='invitee@example.com',
            role=f_role,
        )
        activate_member(
            organization=f_organization,
            actor_identity=f_actor_identity,
            membership_id=membership.id,
        )

    invited = [entry for entry in captured_logs if entry['event'] == 'member_invited']

    assert len(invited) == 1

    assert invited[0]['organization_id'] == str(f_organization.id)

    assert invited[0]['identity_id'] == str(membership.identity_id)

    assert invited[0]['role'] == f_role.code

    activated = [entry for entry in captured_logs if entry['event'] == 'member_activated']

    assert len(activated) == 1

    assert activated[0]['identity_id'] == str(membership.identity_id)


@pytest.mark.django_db
def test_issue_api_key_and_revoke_api_key_log_events(
    f_organization: Organization,
    f_actor_identity: Identity,
) -> None:
    Capability.objects.get_or_create(
        code='observations:write',
        defaults={'description': 'observations:write'},
    )

    with structlog.testing.capture_logs() as captured_logs:
        api_key, _plaintext = issue_api_key(
            organization=f_organization,
            owner_identity=f_actor_identity,
            name='Agent key',
            capabilities=['observations:write'],
        )
        revoke_api_key(api_key)

    issued = [entry for entry in captured_logs if entry['event'] == 'api_key_issued']

    assert len(issued) == 1

    assert issued[0]['organization_id'] == str(f_organization.id)

    assert issued[0]['key_id'] == str(api_key.id)

    assert issued[0]['capabilities'] == ['observations:write']

    revoked = [entry for entry in captured_logs if entry['event'] == 'api_key_revoked']

    assert len(revoked) == 1

    assert revoked[0]['key_id'] == str(api_key.id)


@pytest.mark.django_db(transaction=True)
@postgres_only
def test_concurrent_approve_and_reject_on_same_candidate_serializes_transition(
    f_organization: Organization,
    f_project: Project,
    f_actor_identity: Identity,
) -> None:
    candidate = _make_candidate(f_organization, f_project)

    candidate_id = candidate.id

    approve_holds_lock = threading.Event()

    reject_attempted = threading.Event()

    outcomes: dict[str, object] = {}

    real_audit_admin_action = audit_admin_action

    def blocking_audit_admin_action(*args: object, **kwargs: object) -> object:
        approve_holds_lock.set()

        reject_attempted.wait(timeout=10)

        time.sleep(0.2)

        return real_audit_admin_action(*args, **kwargs)

    def approve_worker() -> None:
        try:
            with mock.patch(
                'engram.console.services.audit_admin_action',
                side_effect=blocking_audit_admin_action,
            ):
                candidate_ref = MemoryCandidate.objects.get(id=candidate_id)

                memory = approve_memory_candidate(f_organization, f_actor_identity, candidate_ref, 'approve wins race')

                outcomes['memory_id'] = memory.id

        except BaseException as error:  # noqa: BLE001
            outcomes['approve_error'] = error

        finally:
            connection.close()

    def reject_worker() -> None:
        try:
            approve_holds_lock.wait(timeout=10)

            reject_attempted.set()

            candidate_ref = MemoryCandidate.objects.get(id=candidate_id)

            reject_review_item(f_organization, f_actor_identity, candidate_ref, 'reject too late')

            outcomes['reject_completed_without_error'] = True

        except MemoryReviewError as error:
            outcomes['reject_error'] = error

        finally:
            connection.close()

    threads = [threading.Thread(target=approve_worker), threading.Thread(target=reject_worker)]

    for started in threads:
        started.start()

    for finished in threads:
        finished.join(timeout=30)

    assert 'approve_error' not in outcomes, outcomes.get('approve_error')

    assert 'memory_id' in outcomes

    candidate.refresh_from_db()

    assert candidate.promoted_memory_id == outcomes['memory_id']

    assert 'reject_error' in outcomes, (
        'reject_review_item must reject an already-promoted candidate with an '
        'invalid_state MemoryReviewError instead of silently overwriting the '
        f'approved transition; observed final status={candidate.status!r}'
    )

    assert outcomes['reject_error'].code == 'invalid_state'

    assert candidate.status == CandidateStatus.PROMOTED
