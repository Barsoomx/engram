from __future__ import annotations

from dataclasses import dataclass

import pytest
from django.db import IntegrityError, transaction

from engram.core.models import Organization, Project, Team
from engram.imports.models import ImportJob, ImportJobStatus


@dataclass(frozen=True)
class JobScope:
    organization: Organization
    project: Project
    team: Team


@pytest.fixture
def f_job_scope() -> JobScope:
    organization = Organization.objects.create(name='Import Org', slug='import-org')
    project = Project.objects.create(
        organization=organization,
        name='Import Project',
        slug='import-project',
        repository_root='/workspace/import-repo',
    )
    team = Team.objects.create(organization=organization, name='Import Team', slug='import-team')

    return JobScope(organization=organization, project=project, team=team)


@pytest.mark.django_db
def test_non_terminal_job_is_unique_per_store(f_job_scope: JobScope) -> None:
    ImportJob.objects.create(
        organization=f_job_scope.organization,
        project=f_job_scope.project,
        source_store_id='store-a',
        status=ImportJobStatus.RECEIVING,
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        ImportJob.objects.create(
            organization=f_job_scope.organization,
            project=f_job_scope.project,
            source_store_id='store-a',
            status=ImportJobStatus.CREATED,
        )


@pytest.mark.django_db
def test_terminal_job_does_not_block_new_non_terminal_job(f_job_scope: JobScope) -> None:
    ImportJob.objects.create(
        organization=f_job_scope.organization,
        project=f_job_scope.project,
        source_store_id='store-b',
        status=ImportJobStatus.SUCCEEDED,
    )

    job = ImportJob.objects.create(
        organization=f_job_scope.organization,
        project=f_job_scope.project,
        source_store_id='store-b',
        status=ImportJobStatus.CREATED,
    )

    assert job.status == ImportJobStatus.CREATED
    assert not job.is_terminal


@pytest.mark.django_db
def test_is_terminal_reflects_status(f_job_scope: JobScope) -> None:
    job = ImportJob.objects.create(
        organization=f_job_scope.organization,
        project=f_job_scope.project,
        source_store_id='store-c',
        status=ImportJobStatus.CREATED,
    )

    assert not job.is_terminal

    job.status = ImportJobStatus.FAILED
    job.save(update_fields=['status', 'updated_at'])

    assert job.is_terminal
