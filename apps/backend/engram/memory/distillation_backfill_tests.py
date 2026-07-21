from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from django.utils import timezone

from engram.core.models import (
    AgentSession,
    Observation,
    Organization,
    Project,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowWork,
    WorkflowWorkType,
)
from engram.memory.distillation_backfill import (
    DEFAULT_FAILURE_CODES,
    select_targets,
)
from engram.memory.observation_work_tests import create_scope
from engram.memory.session_lifecycle import EndSession
from engram.memory.work_execution import (
    ClaimResult,
    claim_work,
    execution_configuration_fingerprint,
    fail_work_claim,
    finish_work_claim,
)
from engram.memory.work_failures import CONFIGURATION, ClassifiedWorkFailure

SessionScope = tuple[Organization, Project, AgentSession]

_LEASE = timedelta(seconds=720)
_RETRY_STEP = timedelta(seconds=1800)


def _seed(session: AgentSession, *, sequence: int, event_type: str = 'post_tool_use') -> Observation:
    return Observation.objects.create(
        organization=session.organization,
        project=session.project,
        team=session.team,
        agent=session.agent,
        session=session,
        observation_type='tool_use',
        title=f'observation {sequence}',
        content_hash=f'content-{session.id}-{sequence}',
        session_sequence=sequence,
        source_metadata={'event_type': event_type},
    )


def _end(scope: SessionScope) -> object:
    organization, project, session = scope

    return EndSession().execute(
        organization_id=organization.id,
        project_id=project.id,
        session_id=session.id,
        ended_at=timezone.now(),
        source='explicit',
    )


def _current_work(scope: SessionScope, *, sequence: int) -> WorkflowWork:
    _seed(scope[2], sequence=sequence)
    result = _end(scope)

    return WorkflowWork.objects.get(id=result.work_id)


def _claim(work: WorkflowWork, *, now: datetime) -> ClaimResult:
    return claim_work(
        work_id=work.id,
        expected_work_type=WorkflowWorkType.SESSION_DISTILLATION,
        lease_owner=f'host:backfill:{uuid.uuid4()}',
        now=now,
        lease_for=_LEASE,
    )


def _fail_work(
    work: WorkflowWork,
    *,
    code: str,
    failure_class: str,
    times: int,
    now: datetime,
) -> None:
    current = now
    for _index in range(times):
        claimed = _claim(work, now=current)
        fingerprint = ''
        if failure_class == CONFIGURATION:
            fingerprint = execution_configuration_fingerprint(WorkflowWork.objects.get(id=work.id))
        fail_work_claim(
            claim=claimed.claim,
            now=current,
            failure=ClassifiedWorkFailure(
                failure_class=failure_class,
                code=code,
                configuration_fingerprint=fingerprint,
            ),
        )
        current = current + _RETRY_STEP


def _succeed(work: WorkflowWork, *, now: datetime) -> None:
    claimed = _claim(work, now=now)
    finish_work_claim(claim=claimed.claim, now=now, completion='product_succeeded')


def _latest_run(work: WorkflowWork) -> WorkflowRun | None:
    return (
        WorkflowRun.objects.filter(work_id=work.id, execution_contract_version=1)
        .order_by('-created_at', '-id')
        .first()
    )


@pytest.fixture
def f_scope() -> SessionScope:
    return create_scope('backfill-redrive')


@pytest.mark.django_db
def test_select_targets_picks_latest_failed_in_set() -> None:
    now = timezone.now()
    scope_a = create_scope('backfill-select-a')
    work_a = _current_work(scope_a, sequence=1)
    _fail_work(work_a, code='provider_output_malformed', failure_class='provider_transient', times=1, now=now)

    scope_b = create_scope('backfill-select-b')
    work_b = _current_work(scope_b, sequence=1)
    _fail_work(work_b, code='provider_output_malformed', failure_class='provider_transient', times=1, now=now)
    _succeed(work_b, now=now + timedelta(hours=1))

    scope_c = create_scope('backfill-select-c')
    work_c = _current_work(scope_c, sequence=1)
    _fail_work(work_c, code='provider_timeout', failure_class='provider_transient', times=1, now=now)

    targets = select_targets(failure_codes=DEFAULT_FAILURE_CODES, limit=100)

    assert {target.work_id for target in targets} == {work_a.id}
    target = targets[0]
    assert target.session_id == work_a.subject_id
    assert target.failure_code == 'provider_output_malformed'
    assert _latest_run(work_c) is not None


@pytest.mark.django_db
def test_select_targets_scope_and_limit() -> None:
    now = timezone.now()
    scope_1 = create_scope('backfill-scope-1')
    work_1 = _current_work(scope_1, sequence=1)
    _fail_work(work_1, code='provider_output_malformed', failure_class='provider_transient', times=1, now=now)

    scope_2 = create_scope('backfill-scope-2')
    work_2 = _current_work(scope_2, sequence=1)
    _fail_work(work_2, code='provider_output_malformed', failure_class='provider_transient', times=1, now=now)

    WorkflowWork.objects.filter(id=work_1.id).update(created_at=now - timedelta(hours=1))

    scoped = select_targets(
        failure_codes=DEFAULT_FAILURE_CODES,
        limit=100,
        organization_id=scope_1[0].id,
        project_id=scope_1[1].id,
    )
    assert {target.work_id for target in scoped} == {work_1.id}

    limited = select_targets(failure_codes=DEFAULT_FAILURE_CODES, limit=1)
    assert len(limited) == 1
    assert limited[0].work_id == work_1.id
    assert {work_1.id, work_2.id} == {
        target.work_id
        for target in select_targets(failure_codes=DEFAULT_FAILURE_CODES, limit=100)
    }
    assert WorkflowRun.objects.filter(work_id=work_2.id, status=WorkflowRunStatus.FAILED).exists()
