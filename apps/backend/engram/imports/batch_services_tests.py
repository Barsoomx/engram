from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.test import override_settings
from django.utils import timezone

from engram.core.models import AuditEvent, Organization, Project
from engram.imports.batch_services import (
    ApplyImportBatch,
    ApplyImportBatchInput,
    CancelImportJob,
    CancelImportJobInput,
    CreateImportJob,
    CreateImportJobInput,
    ExpireStaleImportJobs,
    FinalizeImportJob,
    FinalizeImportJobInput,
    ImportJobConflictError,
    ImportJobNotFoundError,
    ImportJobStateError,
)
from engram.imports.models import ImportJob, ImportJobStatus
from engram.imports.services import ClaudeMemImporter, empty_batch_report


@dataclass(frozen=True)
class BatchScope:
    organization: Organization
    project: Project


@pytest.fixture
def f_batch_scope() -> BatchScope:
    organization = Organization.objects.create(name='Batch Org', slug='batch-org')
    project = Project.objects.create(
        organization=organization,
        name='Batch Project',
        slug='batch-project',
        repository_root='/workspace/example-repo',
    )

    return BatchScope(organization=organization, project=project)


@pytest.fixture
def m_monkeypatch(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    return monkeypatch


def _manifest(tables: dict[str, int]) -> dict[str, Any]:
    return {'schema_version_head': 1, 'tables': tables}


def _create_job(
    scope: BatchScope,
    *,
    store: str = 'store-batch',
    tables: dict[str, int] | None = None,
) -> ImportJob:
    return CreateImportJob().execute(
        CreateImportJobInput(
            organization=scope.organization,
            project=scope.project,
            team=None,
            source_store_id=store,
            manifest=_manifest(tables if tables is not None else {'sdk_sessions': 1}),
            api_key_id=None,
            identity_id=None,
        ),
    )


def _apply_sessions(scope: BatchScope, job: ImportJob, rows: list[dict[str, Any]], seq: int = 0) -> Any:
    return ApplyImportBatch().execute(
        ApplyImportBatchInput(
            organization=scope.organization,
            import_id=job.id,
            seq=seq,
            table='sdk_sessions',
            rows=rows,
            api_key_id=None,
        ),
    )


def _session_row(index: int = 1) -> dict[str, Any]:
    return {
        'id': index,
        'content_session_id': f'content-batch-{index:03d}',
        'memory_session_id': f'memory-batch-{index:03d}',
        'project': '/workspace/example-repo',
        'platform_source': 'codex',
        'started_at': '2026-06-25T09:00:00Z',
        'completed_at': '2026-06-25T09:10:00Z',
        'status': 'completed',
        'prompt_counter': 1,
    }


@pytest.mark.django_db
@pytest.mark.parametrize(
    'raised',
    [
        ValidationError({'platform_source': ['too long']}),
        KeyError('content_session_id'),
        TypeError('int() argument must not be None'),
        ValueError('invalid literal'),
        LookupError('missing row key'),
    ],
)
def test_apply_batch_unexpected_row_error_marks_job_failed_with_safe_reason(
    f_batch_scope: BatchScope,
    m_monkeypatch: pytest.MonkeyPatch,
    raised: Exception,
) -> None:
    job = _create_job(f_batch_scope)

    def m_import_batch(self: object, *args: object, **kwargs: object) -> None:
        raise raised

    m_monkeypatch.setattr(ClaudeMemImporter, 'import_batch', m_import_batch)

    with pytest.raises(ImportJobStateError):
        _apply_sessions(f_batch_scope, job, [_session_row()])

    job.refresh_from_db()
    assert job.status == ImportJobStatus.FAILED
    assert job.failure_reason == 'batch_apply_error'
    audit = AuditEvent.objects.get(
        organization=f_batch_scope.organization,
        event_type='ImportFailed',
        target_id=str(job.id),
    )
    assert audit.metadata['reason'] == 'batch_apply_error'
    assert str(raised) not in job.failure_reason


@pytest.mark.django_db
def test_failed_job_frees_store_for_replacement_job(
    f_batch_scope: BatchScope,
    m_monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _create_job(f_batch_scope, store='store-wedge')

    def m_import_batch(self: object, *args: object, **kwargs: object) -> None:
        raise ValidationError({'platform_source': ['value too long']})

    m_monkeypatch.setattr(ClaudeMemImporter, 'import_batch', m_import_batch)

    with pytest.raises(ImportJobStateError):
        _apply_sessions(f_batch_scope, job, [_session_row()])

    replacement = _create_job(f_batch_scope, store='store-wedge')

    assert replacement.id != job.id
    assert replacement.status == ImportJobStatus.CREATED


@pytest.mark.django_db
def test_finalize_mismatch_marks_job_failed_with_discrepancies(f_batch_scope: BatchScope) -> None:
    job = _create_job(f_batch_scope, tables={'sdk_sessions': 3})
    _apply_sessions(f_batch_scope, job, [_session_row(1)])

    finalized = FinalizeImportJob().execute(
        FinalizeImportJobInput(
            organization=f_batch_scope.organization,
            import_id=job.id,
            client_row_counts={'sdk_sessions': 3},
            api_key_id=None,
        ),
    )

    assert finalized.status == ImportJobStatus.FAILED
    assert finalized.failure_reason == 'incomplete_stream'
    assert finalized.report['discrepancies'] == {'sdk_sessions': {'expected': 3, 'received': 1}}
    assert finalized.report['counts'] == {'sdk_sessions': {'client_rows': 3}}
    assert AuditEvent.objects.filter(
        organization=f_batch_scope.organization,
        event_type='ImportFailed',
        target_id=str(job.id),
        metadata__reason='incomplete_stream',
    ).exists()


@pytest.mark.django_db
def test_finalize_matching_counts_succeeds(f_batch_scope: BatchScope) -> None:
    job = _create_job(f_batch_scope, tables={'sdk_sessions': 2})
    _apply_sessions(f_batch_scope, job, [_session_row(1), _session_row(2)])

    finalized = FinalizeImportJob().execute(
        FinalizeImportJobInput(
            organization=f_batch_scope.organization,
            import_id=job.id,
            client_row_counts={'sdk_sessions': 2},
            api_key_id=None,
        ),
    )

    assert finalized.status == ImportJobStatus.SUCCEEDED
    assert 'discrepancies' not in finalized.report


@pytest.mark.django_db
def test_finalize_counts_duplicates_and_skips_as_received(f_batch_scope: BatchScope) -> None:
    job = _create_job(f_batch_scope, tables={'sdk_sessions': 2})
    _apply_sessions(f_batch_scope, job, [_session_row(1), _session_row(1)])

    finalized = FinalizeImportJob().execute(
        FinalizeImportJobInput(
            organization=f_batch_scope.organization,
            import_id=job.id,
            client_row_counts={'sdk_sessions': 2},
            api_key_id=None,
        ),
    )

    assert finalized.status == ImportJobStatus.SUCCEEDED


@pytest.mark.django_db
def test_create_conflict_carries_active_import_id(f_batch_scope: BatchScope) -> None:
    active = _create_job(f_batch_scope, store='store-conflict')

    with pytest.raises(ImportJobConflictError) as exc_info:
        _create_job(f_batch_scope, store='store-conflict')

    assert exc_info.value.error_code == 'import_job_conflict'
    assert exc_info.value.active_import_id == str(active.id)


@pytest.mark.django_db
def test_cancel_marks_created_job_failed_with_reason_canceled(f_batch_scope: BatchScope) -> None:
    job = _create_job(f_batch_scope, store='store-cancel')

    canceled = CancelImportJob().execute(
        CancelImportJobInput(
            organization=f_batch_scope.organization,
            import_id=job.id,
            actor_id=None,
        ),
    )

    assert canceled.status == ImportJobStatus.FAILED
    assert canceled.failure_reason == 'canceled'
    assert AuditEvent.objects.filter(
        organization=f_batch_scope.organization,
        event_type='ImportFailed',
        target_id=str(job.id),
        metadata__reason='canceled',
    ).exists()


@pytest.mark.django_db
def test_cancel_receiving_job_frees_store_for_replacement(f_batch_scope: BatchScope) -> None:
    job = _create_job(f_batch_scope, store='store-replace')
    _apply_sessions(f_batch_scope, job, [_session_row(1)])
    job.refresh_from_db()
    assert job.status == ImportJobStatus.RECEIVING

    CancelImportJob().execute(
        CancelImportJobInput(
            organization=f_batch_scope.organization,
            import_id=job.id,
            actor_id=None,
        ),
    )
    replacement = _create_job(f_batch_scope, store='store-replace')

    assert replacement.id != job.id
    assert replacement.status == ImportJobStatus.CREATED


@pytest.mark.django_db
def test_cancel_already_terminal_job_raises_state_error(f_batch_scope: BatchScope) -> None:
    job = _create_job(f_batch_scope, store='store-terminal')
    CancelImportJob().execute(
        CancelImportJobInput(
            organization=f_batch_scope.organization,
            import_id=job.id,
            actor_id=None,
        ),
    )

    with pytest.raises(ImportJobStateError) as exc_info:
        CancelImportJob().execute(
            CancelImportJobInput(
                organization=f_batch_scope.organization,
                import_id=job.id,
                actor_id=None,
            ),
        )

    assert exc_info.value.error_code == 'import_job_state'


@pytest.mark.django_db
def test_cancel_unknown_job_raises_not_found(f_batch_scope: BatchScope) -> None:
    with pytest.raises(ImportJobNotFoundError):
        CancelImportJob().execute(
            CancelImportJobInput(
                organization=f_batch_scope.organization,
                import_id=uuid.uuid4(),
                actor_id=None,
            ),
        )


@pytest.mark.django_db
@override_settings(ENGRAM_IMPORT_JOB_TTL_HOURS=24)
def test_expire_stale_import_jobs_expires_only_stale_non_terminal_jobs(f_batch_scope: BatchScope) -> None:
    stale = _create_job(f_batch_scope, store='store-stale')
    fresh = _create_job(f_batch_scope, store='store-fresh')
    done = ImportJob.objects.create(
        organization=f_batch_scope.organization,
        project=f_batch_scope.project,
        source_store_id='store-done',
        status=ImportJobStatus.SUCCEEDED,
        report=empty_batch_report(),
    )
    old = timezone.now() - timedelta(hours=25)
    ImportJob.objects.filter(id__in=[stale.id, done.id]).update(updated_at=old)

    result = ExpireStaleImportJobs().execute()

    stale.refresh_from_db()
    fresh.refresh_from_db()
    done.refresh_from_db()
    assert result == {'expired': 1}
    assert stale.status == ImportJobStatus.EXPIRED
    assert stale.failure_reason == 'expired'
    assert fresh.status == ImportJobStatus.CREATED
    assert done.status == ImportJobStatus.SUCCEEDED
    assert AuditEvent.objects.filter(
        organization=f_batch_scope.organization,
        event_type='ImportFailed',
        target_id=str(stale.id),
        metadata__reason='expired',
    ).exists()


@pytest.mark.django_db
@override_settings(ENGRAM_IMPORT_JOB_TTL_HOURS=24)
def test_expired_job_frees_store_for_new_job(f_batch_scope: BatchScope) -> None:
    stale = _create_job(f_batch_scope, store='store-reuse')
    ImportJob.objects.filter(id=stale.id).update(updated_at=timezone.now() - timedelta(hours=25))

    ExpireStaleImportJobs().execute()
    replacement = _create_job(f_batch_scope, store='store-reuse')

    assert replacement.status == ImportJobStatus.CREATED
