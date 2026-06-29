from __future__ import annotations

import pytest

from engram.context.context_api_tests import create_project_scope
from engram.core.models import WorkflowRun, WorkflowRunStatus, WorkflowRunType
from engram.memory.memory_digest_tests import (
    create_digest_policy,
    create_source_memory,
)
from engram.memory.services import run_daily_digest_with_tracking


@pytest.mark.django_db
def test_tracking_records_succeeded_run_with_result_and_provider_call() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()

    create_digest_policy(organization, team, project)

    source = create_source_memory(organization, team, project, title='Tracked source')

    result = run_daily_digest_with_tracking(
        organization_id=organization.id,
        project_id=project.id,
        memory_ids=(source.id,),
        request_id='track-1',
    )

    run = WorkflowRun.objects.get(organization=organization, request_id='track-1')

    assert run.run_type == WorkflowRunType.DAILY_DIGEST

    assert run.status == WorkflowRunStatus.SUCCEEDED

    assert run.started_at is not None

    assert run.finished_at is not None

    assert run.result_memory_id == result.memory.id

    assert [str(result.provider_call_id)] == run.provider_call_ids

    assert run.input_snapshot == {
        'memory_ids': [str(source.id)],
        'window_days': 7,
    }


@pytest.mark.django_db
def test_tracking_records_failed_run_and_re_raises() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()

    create_digest_policy(organization, team, project)

    with pytest.raises(Exception, match='no approved source memories'):
        run_daily_digest_with_tracking(
            organization_id=organization.id,
            project_id=project.id,
            memory_ids=(__import__('uuid').uuid4(),),
            request_id='track-fail',
        )

    run = WorkflowRun.objects.get(organization=organization, request_id='track-fail')

    assert run.status == WorkflowRunStatus.FAILED

    assert run.finished_at is not None

    assert 'no approved source memories' in run.failure_reason

    assert run.result_memory_id is None

    assert run.provider_call_ids == []


@pytest.mark.django_db
def test_tracking_transitions_queued_then_running() -> None:
    organization, team, project, _owner, _api_key = create_project_scope()

    create_digest_policy(organization, team, project)

    source = create_source_memory(organization, team, project, title='Transition source')

    run_daily_digest_with_tracking(
        organization_id=organization.id,
        project_id=project.id,
        memory_ids=(source.id,),
        request_id='track-transition',
    )

    run = WorkflowRun.objects.get(organization=organization, request_id='track-transition')

    assert run.status == WorkflowRunStatus.SUCCEEDED
