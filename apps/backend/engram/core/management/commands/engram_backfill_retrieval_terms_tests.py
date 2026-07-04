from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command

from engram.core.models import (
    Memory,
    MemoryStatus,
    MemoryVersion,
    Organization,
    Project,
    RetrievalDocument,
)

_TITLE = 'Scope resolver gotcha'
_BODY = '`resolve_scope()` raises AccessDeniedError when ENGRAM_MODE is unset.'


@pytest.fixture
def f_scope() -> tuple[Organization, Project]:
    organization = Organization.objects.create(name='Engram', slug='engram')
    project = Project.objects.create(organization=organization, name='Backend', slug='backend')

    return organization, project


def _make_document(
    organization: Organization,
    project: Project,
    *,
    sequence: int,
    title: str = _TITLE,
    body: str = _BODY,
) -> RetrievalDocument:
    memory = Memory.objects.create(
        organization=organization,
        project=project,
        title=title,
        body=body,
        status=MemoryStatus.APPROVED,
    )
    version = MemoryVersion.objects.create(
        organization=organization,
        project=project,
        memory=memory,
        version=1,
        body=body,
        content_hash=f'hash-{sequence}',
    )
    document = RetrievalDocument(
        organization=organization,
        project=project,
        memory=memory,
        memory_version=version,
        full_text=f'{title}\n\n{body}',
    )
    document.save()

    return document


@pytest.mark.django_db
def test_backfill_populates_symbols_and_exact_terms(
    f_scope: tuple[Organization, Project],
) -> None:
    organization, project = f_scope
    document = _make_document(organization, project, sequence=1)
    assert document.symbols == []
    assert document.exact_terms == []

    call_command('engram_backfill_retrieval_terms')

    document.refresh_from_db()
    assert 'resolve_scope' in document.symbols
    assert 'accessdeniederror' in document.exact_terms


@pytest.mark.django_db
def test_backfill_second_run_reports_no_changes(
    f_scope: tuple[Organization, Project],
) -> None:
    organization, project = f_scope
    _make_document(organization, project, sequence=1)

    call_command('engram_backfill_retrieval_terms')

    out = StringIO()
    call_command('engram_backfill_retrieval_terms', stdout=out)

    assert 'changed=0' in out.getvalue()
    assert 'failed=0' in out.getvalue()
    assert 'scanned=1' in out.getvalue()


@pytest.mark.django_db
def test_backfill_dry_run_makes_no_writes(
    f_scope: tuple[Organization, Project],
) -> None:
    organization, project = f_scope
    document = _make_document(organization, project, sequence=1)

    out = StringIO()
    call_command('engram_backfill_retrieval_terms', '--dry-run', stdout=out)

    document.refresh_from_db()
    assert document.symbols == []
    assert document.exact_terms == []
    assert 'would_change=1' in out.getvalue()
    assert 'scanned=1' in out.getvalue()


@pytest.mark.django_db
def test_backfill_organization_filter_skips_other_organizations(
    f_scope: tuple[Organization, Project],
) -> None:
    organization, project = f_scope
    other_organization = Organization.objects.create(name='Globex', slug='globex')
    other_project = Project.objects.create(organization=other_organization, name='Other', slug='other')

    target = _make_document(organization, project, sequence=1)
    other = _make_document(other_organization, other_project, sequence=2)

    call_command('engram_backfill_retrieval_terms', '--organization', str(organization.id))

    target.refresh_from_db()
    other.refresh_from_db()
    assert 'resolve_scope' in target.symbols
    assert other.symbols == []


@pytest.mark.django_db
def test_backfill_isolates_row_level_failures(
    f_scope: tuple[Organization, Project],
) -> None:
    organization, project = f_scope
    healthy = _make_document(organization, project, sequence=1)

    broken_memory = Memory.objects.create(
        organization=organization,
        project=project,
        title=_TITLE,
        body=_BODY,
        status=MemoryStatus.APPROVED,
    )
    broken_version = MemoryVersion.objects.create(
        organization=organization,
        project=project,
        memory=broken_memory,
        version=1,
        body=_BODY,
        content_hash='hash-broken',
    )
    broken = RetrievalDocument(
        organization=organization,
        project=project,
        memory=broken_memory,
        memory_version=broken_version,
        full_text='',
    )
    RetrievalDocument.objects.bulk_create([broken])

    out = StringIO()
    call_command('engram_backfill_retrieval_terms', stdout=out)

    healthy.refresh_from_db()
    broken.refresh_from_db()
    assert 'resolve_scope' in healthy.symbols
    assert broken.symbols == []
    assert 'changed=1' in out.getvalue()
    assert 'failed=1' in out.getvalue()
    assert 'scanned=2' in out.getvalue()


@pytest.mark.django_db
def test_backfill_project_filter_skips_other_projects(
    f_scope: tuple[Organization, Project],
) -> None:
    organization, project = f_scope
    other_project = Project.objects.create(organization=organization, name='Other', slug='other')

    target = _make_document(organization, project, sequence=1)
    other = _make_document(organization, other_project, sequence=2)

    call_command('engram_backfill_retrieval_terms', '--project', str(project.id))

    target.refresh_from_db()
    other.refresh_from_db()
    assert 'resolve_scope' in target.symbols
    assert other.symbols == []
