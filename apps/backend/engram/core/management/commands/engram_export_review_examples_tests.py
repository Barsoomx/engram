from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command

from engram.core.models import MemoryReviewExample, Organization, Project

_SNAPSHOT = {'title': 'redacted', 'body': 'redacted', 'status': 'approved'}


@pytest.fixture
def f_scope() -> tuple[Organization, Project]:
    organization = Organization.objects.create(name='Engram', slug='engram')
    project = Project.objects.create(organization=organization, name='Backend', slug='backend')

    return organization, project


def _make_example(
    organization: Organization,
    project: Project,
    *,
    action: str = 'approve',
    item_id: str = 'memory-1',
) -> MemoryReviewExample:
    return MemoryReviewExample.objects.create(
        organization=organization,
        project=project,
        item_type='memory',
        item_id=item_id,
        action=action,
        snapshot=_SNAPSHOT,
        curator_context={'route': 'passthrough'},
        reason='reason text',
        actor_id='actor-1',
    )


@pytest.mark.django_db
def test_export_writes_one_valid_json_line_per_example(
    f_scope: tuple[Organization, Project],
) -> None:
    organization, project = f_scope
    _make_example(organization, project, item_id='memory-1')
    _make_example(organization, project, item_id='memory-2')

    out = StringIO()
    call_command('engram_export_review_examples', '--organization', str(organization.id), stdout=out)

    lines = [line for line in out.getvalue().splitlines() if line]
    assert len(lines) == 2

    for line in lines:
        row = json.loads(line)
        assert set(row.keys()) == {
            'id',
            'created_at',
            'item_type',
            'item_id',
            'action',
            'reason',
            'snapshot',
            'curator_context',
            'actor_id',
        }


@pytest.mark.django_db
def test_export_scopes_to_requested_organization(
    f_scope: tuple[Organization, Project],
) -> None:
    organization, project = f_scope
    other_organization = Organization.objects.create(name='Globex', slug='globex')
    other_project = Project.objects.create(organization=other_organization, name='Other', slug='other')

    _make_example(organization, project, item_id='memory-1')
    _make_example(other_organization, other_project, item_id='memory-2')

    out = StringIO()
    call_command('engram_export_review_examples', '--organization', str(organization.id), stdout=out)

    rows = [json.loads(line) for line in out.getvalue().splitlines() if line]
    assert [row['item_id'] for row in rows] == ['memory-1']


@pytest.mark.django_db
def test_export_project_filter_narrows_to_one_project(
    f_scope: tuple[Organization, Project],
) -> None:
    organization, project = f_scope
    other_project = Project.objects.create(organization=organization, name='Other', slug='other')

    _make_example(organization, project, item_id='memory-1')
    _make_example(organization, other_project, item_id='memory-2')

    out = StringIO()
    call_command(
        'engram_export_review_examples',
        '--organization',
        str(organization.id),
        '--project',
        str(project.id),
        stdout=out,
    )

    rows = [json.loads(line) for line in out.getvalue().splitlines() if line]
    assert [row['item_id'] for row in rows] == ['memory-1']


@pytest.mark.django_db
def test_export_stdout_mode_reports_count_on_stderr(
    f_scope: tuple[Organization, Project],
) -> None:
    organization, project = f_scope
    _make_example(organization, project)

    out = StringIO()
    err = StringIO()
    call_command('engram_export_review_examples', '--organization', str(organization.id), stdout=out, stderr=err)

    assert 'exported=1' in err.getvalue()
    assert json.loads(out.getvalue().strip())['item_id'] == 'memory-1'


@pytest.mark.django_db
def test_export_file_mode_writes_jsonl_to_disk(
    f_scope: tuple[Organization, Project],
    tmp_path: Path,
) -> None:
    organization, project = f_scope
    _make_example(organization, project, item_id='memory-1')
    _make_example(organization, project, item_id='memory-2')
    output_path = tmp_path / 'examples.jsonl'

    err = StringIO()
    call_command(
        'engram_export_review_examples',
        '--organization',
        str(organization.id),
        '--output',
        str(output_path),
        stderr=err,
    )

    lines = [line for line in output_path.read_text(encoding='utf-8').splitlines() if line]
    assert len(lines) == 2
    for line in lines:
        json.loads(line)

    assert 'exported=2' in err.getvalue()


@pytest.mark.django_db
def test_export_empty_result_reports_zero(
    f_scope: tuple[Organization, Project],
) -> None:
    organization, _project = f_scope

    out = StringIO()
    err = StringIO()
    call_command('engram_export_review_examples', '--organization', str(organization.id), stdout=out, stderr=err)

    assert out.getvalue() == ''
    assert 'exported=0' in err.getvalue()
