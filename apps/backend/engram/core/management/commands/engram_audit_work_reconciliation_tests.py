from __future__ import annotations

import json
from datetime import datetime, timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone
from django_celery_outbox.models import CeleryOutboxDeadLetter

from engram.core.models import WorkflowWork
from engram.memory.observation_work_tests import create_scope
from engram.memory.reconciler_test_support import ended_session_work

_COMMAND = 'engram_audit_work_reconciliation'
_NAIVE_AS_OF = '2026-07-10T12:00:00'
_AWARE_AS_OF = '2026-07-10T12:00:00+00:00'
_DISTILL_TASK = 'engram.memory.distill_session_work_v1'


def _run_json(organization_id: object, project_id: object, *extra: str) -> str:
    out = StringIO()
    call_command(
        _COMMAND,
        '--organization-id',
        str(organization_id),
        '--project-id',
        str(project_id),
        '--format',
        'json',
        *extra,
        stdout=out,
    )

    return out.getvalue()


@pytest.mark.django_db
def test_json_output_is_a_scoped_report_object() -> None:
    organization, project, _session = create_scope('audit-json')

    payload = json.loads(_run_json(organization.id, project.id))

    assert payload['organization_id'] == str(organization.id)
    assert payload['project_id'] == str(project.id)
    assert 'findings' in payload
    assert 'counts_by_code' in payload


@pytest.mark.django_db
def test_repeated_execution_at_same_as_of_is_byte_stable() -> None:
    organization, project, _session = create_scope('audit-stable')

    first = _run_json(organization.id, project.id, '--as-of', _AWARE_AS_OF)
    second = _run_json(organization.id, project.id, '--as-of', _AWARE_AS_OF)

    assert first == second


@pytest.mark.django_db
def test_byte_stable_snapshot_with_multiple_finding_codes() -> None:
    scope = create_scope('audit-multi-findings')
    organization, project, _session = scope
    work = ended_session_work(scope, sequence=1)
    as_of = datetime.fromisoformat(_AWARE_AS_OF)
    WorkflowWork.objects.filter(id=work.id).update(created_at=as_of - timedelta(days=3))
    now = timezone.now()
    CeleryOutboxDeadLetter.objects.create(
        task_id=f'workflow-work:{work.id}',
        task_name=_DISTILL_TASK,
        args=[str(work.id)],
        kwargs={},
        created_at=now,
        dead_at=now,
        failure_reason='provider secret leaked into transport failure reason',
    )

    first = _run_json(organization.id, project.id, '--as-of', _AWARE_AS_OF)
    second = _run_json(organization.id, project.id, '--as-of', _AWARE_AS_OF)

    payload = json.loads(first)
    codes = {finding['code'] for finding in payload['findings']}
    assert {'work_never_claimed', 'dead_letter_unsatisfied_work'} <= codes
    assert first == second


@pytest.mark.django_db
def test_missing_scope_arguments_are_rejected() -> None:
    with pytest.raises(CommandError, match='required'):
        call_command(_COMMAND, '--format', 'json')


@pytest.mark.django_db
def test_mismatched_scope_fails_before_other_reads() -> None:
    organization, _project, _session = create_scope('audit-scope-a')
    _other_org, foreign_project, _foreign_session = create_scope('audit-scope-b')

    with pytest.raises(CommandError, match='does not belong'):
        call_command(
            _COMMAND,
            '--organization-id',
            str(organization.id),
            '--project-id',
            str(foreign_project.id),
        )


@pytest.mark.django_db
def test_naive_as_of_is_rejected() -> None:
    organization, project, _session = create_scope('audit-naive')

    with pytest.raises(CommandError, match='timezone-aware'):
        call_command(
            _COMMAND,
            '--organization-id',
            str(organization.id),
            '--project-id',
            str(project.id),
            '--as-of',
            _NAIVE_AS_OF,
        )


@pytest.mark.django_db
def test_malformed_id_is_rejected() -> None:
    _organization, project, _session = create_scope('audit-malformed')

    with pytest.raises(CommandError, match='invalid'):
        call_command(
            _COMMAND,
            '--organization-id',
            'not-a-uuid',
            '--project-id',
            str(project.id),
        )


@pytest.mark.django_db
def test_mutation_style_option_is_rejected() -> None:
    organization, project, _session = create_scope('audit-no-mutation')

    with pytest.raises(CommandError, match='unrecognized'):
        call_command(
            _COMMAND,
            '--organization-id',
            str(organization.id),
            '--project-id',
            str(project.id),
            '--apply',
        )
