from __future__ import annotations

import uuid

import pytest
import structlog
from django.db import transaction

from engram.access.models import Identity, IdentityType
from engram.console.services import MemoryReviewError
from engram.console.usecases.review_action import ReviewActionInput, ReviewActionUseCase
from engram.core.models import (
    CandidateStatus,
    Memory,
    MemoryCandidate,
    MemoryStatus,
    Organization,
    Project,
    VisibilityScope,
)


@pytest.fixture
def f_organization() -> Organization:
    return Organization.objects.create(name='Acme', slug='acme')


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


def _execute(input_dto: ReviewActionInput) -> dict:
    output = ReviewActionUseCase(user=None, transaction=transaction.atomic()).execute(input_dto)

    return output.result


@pytest.mark.django_db
def test_approve_action_promotes_candidate_and_returns_ids(
    f_organization: Organization,
    f_project: Project,
    f_actor_identity: Identity,
) -> None:
    candidate = _make_candidate(f_organization, f_project)

    result = _execute(
        ReviewActionInput(
            organization=f_organization,
            actor_identity=f_actor_identity,
            item_id=candidate.id,
            action_name='approve',
            reason='looks good',
        ),
    )

    candidate.refresh_from_db()

    assert candidate.status == CandidateStatus.PROMOTED

    assert result['action'] == 'approve'

    assert result['candidate_id'] == str(candidate.id)

    assert result['memory_id'] == str(candidate.promoted_memory_id)


@pytest.mark.django_db
def test_edit_action_creates_new_version(
    f_organization: Organization,
    f_project: Project,
    f_actor_identity: Identity,
) -> None:
    memory = _make_memory(f_organization, f_project)

    result = _execute(
        ReviewActionInput(
            organization=f_organization,
            actor_identity=f_actor_identity,
            item_id=memory.id,
            action_name='edit',
            reason='clarify',
            body='edited body',
        ),
    )

    memory.refresh_from_db()

    assert memory.body == 'edited body'

    assert result['action'] == 'edit'

    assert result['version'] == memory.current_version


@pytest.mark.django_db
def test_edit_action_without_body_raises_body_required(
    f_organization: Organization,
    f_project: Project,
    f_actor_identity: Identity,
) -> None:
    memory = _make_memory(f_organization, f_project)

    with pytest.raises(MemoryReviewError) as error:
        _execute(
            ReviewActionInput(
                organization=f_organization,
                actor_identity=f_actor_identity,
                item_id=memory.id,
                action_name='edit',
                reason='clarify',
            ),
        )

    assert error.value.code == 'body_required'


@pytest.mark.django_db
def test_narrow_action_creates_link(
    f_organization: Organization,
    f_project: Project,
    f_actor_identity: Identity,
) -> None:
    memory = _make_memory(f_organization, f_project)

    target = _make_memory(f_organization, f_project)

    result = _execute(
        ReviewActionInput(
            organization=f_organization,
            actor_identity=f_actor_identity,
            item_id=memory.id,
            action_name='narrow',
            reason='specific case',
            target_memory_id=target.id,
        ),
    )

    assert result['action'] == 'narrow'

    assert result['link_id'] is not None


@pytest.mark.django_db
def test_narrow_action_without_target_raises_target_required(
    f_organization: Organization,
    f_project: Project,
    f_actor_identity: Identity,
) -> None:
    memory = _make_memory(f_organization, f_project)

    with pytest.raises(MemoryReviewError) as error:
        _execute(
            ReviewActionInput(
                organization=f_organization,
                actor_identity=f_actor_identity,
                item_id=memory.id,
                action_name='narrow',
                reason='specific case',
            ),
        )

    assert error.value.code == 'target_required'


@pytest.mark.django_db
def test_supersede_action_marks_stale_and_creates_link(
    f_organization: Organization,
    f_project: Project,
    f_actor_identity: Identity,
) -> None:
    memory = _make_memory(f_organization, f_project)

    target = _make_memory(f_organization, f_project)

    result = _execute(
        ReviewActionInput(
            organization=f_organization,
            actor_identity=f_actor_identity,
            item_id=memory.id,
            action_name='supersede',
            reason='outdated',
            target_memory_id=target.id,
        ),
    )

    memory.refresh_from_db()

    assert memory.stale is True

    assert result['action'] == 'supersede'


@pytest.mark.django_db
def test_reject_action_on_candidate_rejects_it(
    f_organization: Organization,
    f_project: Project,
    f_actor_identity: Identity,
) -> None:
    candidate = _make_candidate(f_organization, f_project)

    result = _execute(
        ReviewActionInput(
            organization=f_organization,
            actor_identity=f_actor_identity,
            item_id=candidate.id,
            action_name='reject',
            reason='duplicate',
        ),
    )

    candidate.refresh_from_db()

    assert candidate.status == CandidateStatus.REJECTED

    assert result == {'action': 'reject', 'candidate_id': str(candidate.id)}


@pytest.mark.django_db
def test_reject_action_on_memory_refutes_it(
    f_organization: Organization,
    f_project: Project,
    f_actor_identity: Identity,
) -> None:
    memory = _make_memory(f_organization, f_project)

    result = _execute(
        ReviewActionInput(
            organization=f_organization,
            actor_identity=f_actor_identity,
            item_id=memory.id,
            action_name='reject',
            reason='wrong',
        ),
    )

    memory.refresh_from_db()

    assert memory.status == MemoryStatus.REFUTED

    assert result == {'action': 'reject', 'memory_id': str(memory.id)}


@pytest.mark.django_db
def test_archive_action_sets_archived(
    f_organization: Organization,
    f_project: Project,
    f_actor_identity: Identity,
) -> None:
    memory = _make_memory(f_organization, f_project, status=MemoryStatus.APPROVED)

    result = _execute(
        ReviewActionInput(
            organization=f_organization,
            actor_identity=f_actor_identity,
            item_id=memory.id,
            action_name='archive',
            reason='stale topic',
        ),
    )

    memory.refresh_from_db()

    assert memory.status == MemoryStatus.ARCHIVED

    assert result == {'action': 'archive', 'memory_id': str(memory.id)}


@pytest.mark.django_db
def test_unknown_action_raises_memory_review_error(
    f_organization: Organization,
    f_project: Project,
    f_actor_identity: Identity,
) -> None:
    memory = _make_memory(f_organization, f_project)

    with pytest.raises(MemoryReviewError) as error:
        _execute(
            ReviewActionInput(
                organization=f_organization,
                actor_identity=f_actor_identity,
                item_id=memory.id,
                action_name='teleport',
                reason='nope',
            ),
        )

    assert error.value.code == 'unknown_action'


@pytest.mark.django_db
def test_approve_action_logs_memory_review_action_applied(
    f_organization: Organization,
    f_project: Project,
    f_actor_identity: Identity,
) -> None:
    candidate = _make_candidate(f_organization, f_project)

    with structlog.testing.capture_logs() as captured_logs:
        _execute(
            ReviewActionInput(
                organization=f_organization,
                actor_identity=f_actor_identity,
                item_id=candidate.id,
                action_name='approve',
                reason='looks good',
            ),
        )

    events = [entry for entry in captured_logs if entry['event'] == 'memory_review_action_applied']

    assert len(events) == 1

    assert events[0]['action'] == 'approve'

    assert events[0]['item_id'] == str(candidate.id)

    assert events[0]['item_type'] == 'candidate'

    assert events[0]['organization_id'] == str(f_organization.id)


@pytest.mark.django_db
def test_edit_action_logs_item_type_memory(
    f_organization: Organization,
    f_project: Project,
    f_actor_identity: Identity,
) -> None:
    memory = _make_memory(f_organization, f_project)

    with structlog.testing.capture_logs() as captured_logs:
        _execute(
            ReviewActionInput(
                organization=f_organization,
                actor_identity=f_actor_identity,
                item_id=memory.id,
                action_name='edit',
                reason='clarify',
                body='edited body',
            ),
        )

    events = [entry for entry in captured_logs if entry['event'] == 'memory_review_action_applied']

    assert len(events) == 1

    assert events[0]['item_type'] == 'memory'


@pytest.mark.django_db
def test_approve_action_on_missing_candidate_raises_not_found(
    f_organization: Organization,
    f_actor_identity: Identity,
) -> None:
    with pytest.raises(MemoryReviewError) as error:
        _execute(
            ReviewActionInput(
                organization=f_organization,
                actor_identity=f_actor_identity,
                item_id=uuid.uuid4(),
                action_name='approve',
                reason='looks good',
            ),
        )

    assert error.value.code == 'not_found'


@pytest.mark.django_db
def test_restore_action_reactivates_archived_memory(
    f_organization: Organization,
    f_project: Project,
    f_actor_identity: Identity,
) -> None:
    candidate = _make_candidate(f_organization, f_project)

    result = _execute(
        ReviewActionInput(
            organization=f_organization,
            actor_identity=f_actor_identity,
            item_id=candidate.id,
            action_name='approve',
            reason='looks good',
        ),
    )

    memory = Memory.objects.get(id=result['memory_id'])

    _execute(
        ReviewActionInput(
            organization=f_organization,
            actor_identity=f_actor_identity,
            item_id=memory.id,
            action_name='archive',
            reason='stale topic',
        ),
    )

    memory.refresh_from_db()

    assert memory.status == MemoryStatus.ARCHIVED

    result = _execute(
        ReviewActionInput(
            organization=f_organization,
            actor_identity=f_actor_identity,
            item_id=memory.id,
            action_name='restore',
            reason='undo archive',
        ),
    )

    memory.refresh_from_db()

    assert memory.status == MemoryStatus.APPROVED

    assert result == {'action': 'restore', 'memory_id': str(memory.id)}


@pytest.mark.django_db
def test_restore_action_on_candidate_raises_not_found(
    f_organization: Organization,
    f_project: Project,
    f_actor_identity: Identity,
) -> None:
    candidate = _make_candidate(f_organization, f_project)

    with pytest.raises(MemoryReviewError) as error:
        _execute(
            ReviewActionInput(
                organization=f_organization,
                actor_identity=f_actor_identity,
                item_id=candidate.id,
                action_name='restore',
                reason='nope',
            ),
        )

    assert error.value.code == 'not_found'


@pytest.mark.django_db
def test_restore_action_logs_item_type_memory(
    f_organization: Organization,
    f_project: Project,
    f_actor_identity: Identity,
) -> None:
    candidate = _make_candidate(f_organization, f_project)

    approved = _execute(
        ReviewActionInput(
            organization=f_organization,
            actor_identity=f_actor_identity,
            item_id=candidate.id,
            action_name='approve',
            reason='looks good',
        ),
    )

    memory = Memory.objects.get(id=approved['memory_id'])

    archive_result = _execute(
        ReviewActionInput(
            organization=f_organization,
            actor_identity=f_actor_identity,
            item_id=memory.id,
            action_name='archive',
            reason='stale topic',
        ),
    )

    assert archive_result == {'action': 'archive', 'memory_id': str(memory.id)}

    with structlog.testing.capture_logs() as captured_logs:
        _execute(
            ReviewActionInput(
                organization=f_organization,
                actor_identity=f_actor_identity,
                item_id=memory.id,
                action_name='restore',
                reason='undo archive',
            ),
        )

    events = [entry for entry in captured_logs if entry['event'] == 'memory_review_action_applied']

    assert len(events) == 1

    assert events[0]['action'] == 'restore'

    assert events[0]['item_type'] == 'memory'
