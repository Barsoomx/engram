from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta

import structlog
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from engram.core.domain.usecases.errors import DomainError
from engram.core.models import AuditEvent, AuditResult, Organization, Project, Team
from engram.core.redaction import redact_value
from engram.imports.models import NON_TERMINAL_IMPORT_STATUSES, ImportJob, ImportJobStatus
from engram.imports.services import (
    TABLE_PHASE,
    ClaudeMemImporter,
    ClaudeMemImportError,
    ImportContext,
    ImportReport,
    empty_batch_report,
)

logger = structlog.get_logger(__name__)

_BATCH_APPLY_ERRORS = (ClaudeMemImportError, ValidationError, LookupError, TypeError, ValueError)

_REJECTION_MESSAGES = {
    'import_job_terminal': 'import job is already finalized',
    'out_of_order_seq': 'batch seq is out of order',
    'unknown_table': 'unsupported import table',
    'table_order_violation': 'batch table is out of order',
}


class ImportJobConflictError(DomainError):
    default_error_code = 'import_job_conflict'
    default_status_code = 409


class ImportJobNotFoundError(DomainError):
    default_error_code = 'import_job_not_found'
    default_status_code = 404


class ImportBatchRejectedError(DomainError):
    default_error_code = 'import_batch_rejected'
    default_status_code = 409


class ImportJobStateError(DomainError):
    default_error_code = 'import_job_state'
    default_status_code = 409


class ImportPayloadTooLargeError(DomainError):
    default_error_code = 'import_payload_too_large'
    default_status_code = 413


def audit_batch_rejected(
    job: ImportJob,
    *,
    actor_id: uuid.UUID | None,
    reason: str,
    seq: int,
    table: str,
    rows: int,
    request_id: str = '',
) -> None:
    _emit_job_audit(
        job,
        event_type='ImportBatchRejected',
        result=AuditResult.ERROR,
        actor_id=actor_id,
        metadata={'reason': reason, 'seq': seq, 'table': table, 'rows': rows},
        request_id=request_id,
    )


@dataclass(frozen=True)
class CreateImportJobInput:
    organization: Organization
    project: Project
    team: Team | None
    allowed_team_ids: tuple[uuid.UUID, ...]
    source_store_id: str
    manifest: dict[str, object]
    api_key_id: uuid.UUID | None
    identity_id: uuid.UUID | None
    request_id: str = ''


@dataclass(frozen=True)
class ApplyImportBatchInput:
    organization: Organization
    import_id: uuid.UUID
    seq: int
    table: str
    rows: list[dict[str, object]]
    api_key_id: uuid.UUID | None
    request_id: str = ''


@dataclass(frozen=True)
class ApplyImportBatchResult:
    seq: int
    created: int
    duplicates: int
    skipped: int
    replayed: bool


@dataclass(frozen=True)
class FinalizeImportJobInput:
    organization: Organization
    import_id: uuid.UUID
    client_row_counts: dict[str, int]
    api_key_id: uuid.UUID | None
    request_id: str = ''


@dataclass(frozen=True)
class CancelImportJobInput:
    organization: Organization
    import_id: uuid.UUID
    actor_id: uuid.UUID | None
    request_id: str = ''


class CreateImportJob:
    def execute(self, data: CreateImportJobInput) -> ImportJob:
        try:
            with transaction.atomic():
                active = self._active_job(data)
                if active is not None:
                    raise self._conflict(active, data)

                job = ImportJob.objects.create(
                    organization=data.organization,
                    project=data.project,
                    team=data.team,
                    source_store_id=data.source_store_id,
                    status=ImportJobStatus.CREATED,
                    manifest=data.manifest,
                    report=empty_batch_report(),
                    created_by_api_key=data.api_key_id,
                    created_by_identity=data.identity_id,
                )
        except IntegrityError:
            raise self._conflict(self._active_job(data), data) from None

        _emit_job_audit(
            job,
            event_type='ImportStarted',
            result=AuditResult.RECORDED,
            actor_id=data.api_key_id,
            metadata={
                'source_store_id': job.source_store_id,
                'manifest_tables': sorted((data.manifest.get('tables') or {}).keys()),
            },
            request_id=data.request_id,
        )

        return job

    def _active_job(self, data: CreateImportJobInput) -> ImportJob | None:
        return ImportJob.objects.filter(
            organization=data.organization,
            project=data.project,
            source_store_id=data.source_store_id,
            status__in=NON_TERMINAL_IMPORT_STATUSES,
        ).first()

    def _conflict(self, active: ImportJob | None, data: CreateImportJobInput) -> ImportJobConflictError:
        error = ImportJobConflictError('an active import already exists for this source store')
        if active is not None and (not data.allowed_team_ids or active.team_id in data.allowed_team_ids):
            error.active_import_id = str(active.id)

        return error


class ApplyImportBatch:
    def execute(self, data: ApplyImportBatchInput) -> ApplyImportBatchResult:
        reason = ''
        failed = False
        job: ImportJob | None = None
        with transaction.atomic():
            job = self._lock_job(data.organization, data.import_id)
            recorded = job.applied_batches.get(str(data.seq))
            if recorded is not None:
                return self._replay_result(data.seq, recorded)

            reason = self._rejection_reason(job, data)
            if not reason:
                try:
                    with transaction.atomic():
                        self._apply(job, data)
                except _BATCH_APPLY_ERRORS:
                    logger.exception(
                        'import_batch_apply_failed',
                        import_id=str(data.import_id),
                        seq=data.seq,
                        table=data.table,
                    )
                    failed = True
                else:
                    return ApplyImportBatchResult(
                        seq=data.seq,
                        created=job.applied_batches[str(data.seq)]['created'],
                        duplicates=job.applied_batches[str(data.seq)]['duplicates'],
                        skipped=job.applied_batches[str(data.seq)]['skipped'],
                        replayed=False,
                    )

        if failed:
            self._fail_job(job, data)

            raise ImportJobStateError('import batch could not be applied')

        _emit_job_audit(
            job,
            event_type='ImportBatchRejected',
            result=AuditResult.ERROR,
            actor_id=data.api_key_id,
            metadata={'reason': reason, 'seq': data.seq, 'table': data.table, 'rows': len(data.rows)},
            request_id=data.request_id,
        )
        raise ImportBatchRejectedError(_REJECTION_MESSAGES[reason], error_code=reason)

    def _apply(self, job: ImportJob, data: ApplyImportBatchInput) -> None:
        context = ImportContext(
            source_store_id=job.source_store_id,
            organization=job.organization,
            project=job.project,
            team=job.team,
        )
        batch = ClaudeMemImporter().import_batch(context, data.table, data.rows, defer_embedding=True)
        _merge_report(job.report, batch.report)
        job.batches_applied += 1
        job.rows_created += batch.created
        job.rows_duplicate += batch.duplicates
        job.last_batch_seq = data.seq
        phase = TABLE_PHASE[data.table]
        if phase > job.max_table_phase:
            job.max_table_phase = phase
        job.applied_batches[str(data.seq)] = {
            'created': batch.created,
            'duplicates': batch.duplicates,
            'skipped': batch.skipped,
            'table': data.table,
        }
        if job.status == ImportJobStatus.CREATED:
            job.status = ImportJobStatus.RECEIVING
        job.save(
            update_fields=[
                'report',
                'batches_applied',
                'rows_created',
                'rows_duplicate',
                'last_batch_seq',
                'max_table_phase',
                'applied_batches',
                'status',
                'updated_at',
            ],
        )

    def _fail_job(self, job: ImportJob, data: ApplyImportBatchInput) -> None:
        with transaction.atomic():
            locked = ImportJob.objects.select_for_update().get(id=job.id)
            locked.status = ImportJobStatus.FAILED
            locked.failure_reason = 'batch_apply_error'
            locked.save(update_fields=['status', 'failure_reason', 'updated_at'])

        _emit_job_audit(
            job,
            event_type='ImportFailed',
            result=AuditResult.ERROR,
            actor_id=data.api_key_id,
            metadata={'reason': 'batch_apply_error', 'seq': data.seq, 'table': data.table},
            request_id=data.request_id,
        )

    def _rejection_reason(self, job: ImportJob, data: ApplyImportBatchInput) -> str:
        if job.is_terminal:
            return 'import_job_terminal'

        if data.table not in TABLE_PHASE:
            return 'unknown_table'

        if data.seq != job.last_batch_seq + 1:
            return 'out_of_order_seq'

        if TABLE_PHASE[data.table] < job.max_table_phase:
            return 'table_order_violation'

        return ''

    def _replay_result(self, seq: int, recorded: dict[str, object]) -> ApplyImportBatchResult:
        return ApplyImportBatchResult(
            seq=seq,
            created=int(recorded.get('created', 0)),
            duplicates=int(recorded.get('duplicates', 0)),
            skipped=int(recorded.get('skipped', 0)),
            replayed=True,
        )

    def _lock_job(self, organization: Organization, import_id: uuid.UUID) -> ImportJob:
        try:
            return ImportJob.objects.select_for_update().get(organization=organization, id=import_id)
        except ImportJob.DoesNotExist:
            raise ImportJobNotFoundError('import job was not found') from None


class FinalizeImportJob:
    def execute(self, data: FinalizeImportJobInput) -> ImportJob:
        with transaction.atomic():
            job = self._lock_job(data.organization, data.import_id)
            if job.status == ImportJobStatus.SUCCEEDED:
                return job

            if job.is_terminal:
                raise ImportJobStateError('import job is already finalized')

            job.report = self._build_report(job, data.client_row_counts)
            discrepancies = self._stream_discrepancies(job)
            if discrepancies:
                job.report['discrepancies'] = discrepancies
                job.status = ImportJobStatus.FAILED
                job.failure_reason = 'incomplete_stream'
                job.save(update_fields=['report', 'status', 'failure_reason', 'updated_at'])
            else:
                job.status = ImportJobStatus.SUCCEEDED
                job.save(update_fields=['report', 'status', 'updated_at'])

        if discrepancies:
            _emit_job_audit(
                job,
                event_type='ImportFailed',
                result=AuditResult.ERROR,
                actor_id=data.api_key_id,
                metadata={'reason': 'incomplete_stream', 'discrepancies': discrepancies},
                request_id=data.request_id,
            )

            return job

        _emit_job_audit(
            job,
            event_type='ImportCompleted',
            result=AuditResult.RECORDED,
            actor_id=data.api_key_id,
            metadata={
                'source_store_id': job.source_store_id,
                'batches_applied': job.batches_applied,
                'rows_created': job.rows_created,
                'rows_duplicate': job.rows_duplicate,
            },
            request_id=data.request_id,
        )

        return job

    def _build_report(self, job: ImportJob, client_row_counts: dict[str, int]) -> ImportReport:
        report = dict(job.report) if isinstance(job.report, dict) else empty_batch_report()
        report['counts'] = {table: {'client_rows': int(count)} for table, count in client_row_counts.items()}
        report['source_store_id'] = job.source_store_id

        return report

    def _stream_discrepancies(self, job: ImportJob) -> dict[str, dict[str, int]]:
        manifest_tables = job.manifest.get('tables') if isinstance(job.manifest, dict) else None
        if not isinstance(manifest_tables, dict):
            return {}

        received: dict[str, int] = {}
        applied = job.applied_batches if isinstance(job.applied_batches, dict) else {}
        for batch in applied.values():
            if not isinstance(batch, dict):
                continue

            table = str(batch.get('table') or '')
            rows = int(batch.get('created') or 0) + int(batch.get('duplicates') or 0) + int(batch.get('skipped') or 0)
            received[table] = received.get(table, 0) + rows

        discrepancies: dict[str, dict[str, int]] = {}
        for table, declared in manifest_tables.items():
            if table not in TABLE_PHASE:
                continue

            expected = int(declared or 0)
            actual = received.get(table, 0)
            if expected != actual:
                discrepancies[table] = {'expected': expected, 'received': actual}

        return discrepancies

    def _lock_job(self, organization: Organization, import_id: uuid.UUID) -> ImportJob:
        try:
            return ImportJob.objects.select_for_update().get(organization=organization, id=import_id)
        except ImportJob.DoesNotExist:
            raise ImportJobNotFoundError('import job was not found') from None


class CancelImportJob:
    def execute(self, data: CancelImportJobInput) -> ImportJob:
        with transaction.atomic():
            job = self._lock_job(data.organization, data.import_id)
            if job.is_terminal:
                raise ImportJobStateError('import job is already finalized')

            job.status = ImportJobStatus.FAILED
            job.failure_reason = 'canceled'
            job.save(update_fields=['status', 'failure_reason', 'updated_at'])

        _emit_job_audit(
            job,
            event_type='ImportFailed',
            result=AuditResult.ERROR,
            actor_id=data.actor_id,
            metadata={'reason': 'canceled', 'source_store_id': job.source_store_id},
            request_id=data.request_id,
        )

        return job

    def _lock_job(self, organization: Organization, import_id: uuid.UUID) -> ImportJob:
        try:
            return ImportJob.objects.select_for_update().get(organization=organization, id=import_id)
        except ImportJob.DoesNotExist:
            raise ImportJobNotFoundError('import job was not found') from None


class ExpireStaleImportJobs:
    def execute(self) -> dict[str, int]:
        ttl_hours = int(getattr(settings, 'ENGRAM_IMPORT_JOB_TTL_HOURS', 24))
        cutoff = timezone.now() - timedelta(hours=ttl_hours)
        stale_ids = list(
            ImportJob.objects.filter(
                status__in=NON_TERMINAL_IMPORT_STATUSES,
                updated_at__lt=cutoff,
            ).values_list('id', flat=True),
        )
        expired = 0
        for job_id in stale_ids:
            job = self._expire(job_id, cutoff)
            if job is None:
                continue

            _emit_job_audit(
                job,
                event_type='ImportFailed',
                result=AuditResult.ERROR,
                actor_id=None,
                metadata={'reason': 'expired', 'source_store_id': job.source_store_id},
                request_id='',
            )
            logger.info(
                'import_job_expired',
                import_id=str(job.id),
                source_store_id=job.source_store_id,
            )
            expired += 1

        return {'expired': expired}

    def _expire(self, job_id: uuid.UUID, cutoff: object) -> ImportJob | None:
        with transaction.atomic():
            job = (
                ImportJob.objects.select_for_update()
                .filter(
                    id=job_id,
                    status__in=NON_TERMINAL_IMPORT_STATUSES,
                    updated_at__lt=cutoff,
                )
                .first()
            )
            if job is None:
                return None

            job.status = ImportJobStatus.EXPIRED
            job.failure_reason = 'expired'
            job.save(update_fields=['status', 'failure_reason', 'updated_at'])

        return job


def get_import_job(organization: Organization, import_id: uuid.UUID) -> ImportJob:
    try:
        return ImportJob.objects.get(organization=organization, id=import_id)
    except ImportJob.DoesNotExist:
        raise ImportJobNotFoundError('import job was not found') from None


def _merge_report(target: ImportReport, delta: ImportReport) -> None:
    for section in ('created', 'duplicates'):
        section_target = target.setdefault(section, {})
        for key, value in delta.get(section, {}).items():
            section_target[key] = section_target.get(key, 0) + value
    for section in ('unsupported', 'warnings'):
        target.setdefault(section, []).extend(delta.get(section, []))
    if delta.get('redactions', {}).get('redacted'):
        target.setdefault('redactions', {})['redacted'] = True
    if delta.get('truncations', {}).get('truncated'):
        target.setdefault('truncations', {})['truncated'] = True


def _emit_job_audit(
    job: ImportJob,
    *,
    event_type: str,
    result: str,
    actor_id: uuid.UUID | None,
    metadata: dict[str, object],
    request_id: str,
) -> None:
    AuditEvent.objects.create(
        organization=job.organization,
        project=job.project,
        team=job.team,
        event_type=event_type,
        actor_type='api_key',
        actor_id=str(actor_id) if actor_id else '',
        target_type='import_job',
        target_id=str(job.id),
        capability='memories:admin',
        result=result,
        request_id=request_id,
        metadata=redact_value(metadata).value,
    )
