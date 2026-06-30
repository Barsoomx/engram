from __future__ import annotations

import uuid

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from engram.access.models import Identity, IdentityType
from engram.console.services import (
    MemoryReviewError,
    _lock_candidate_or_404,
    _lock_memory_or_404,
    approve_memory_candidate,
    archive_memory,
    edit_memory_body,
    narrow_memory,
    reject_review_item,
    supersede_memory,
)
from engram.core.models import (
    CandidateStatus,
    Memory,
    MemoryCandidate,
    MemoryStatus,
    Organization,
    Project,
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
