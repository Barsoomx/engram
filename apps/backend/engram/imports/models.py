from __future__ import annotations

from django.db import models

from engram.core.models import (
    Organization,
    Project,
    Team,
    TimestampedModel,
    check_organization_scope,
    check_project_organization,
    raise_scope_errors,
)


class ImportJobStatus(models.TextChoices):
    CREATED = 'created', 'Created'
    RECEIVING = 'receiving', 'Receiving'
    SUCCEEDED = 'succeeded', 'Succeeded'
    FAILED = 'failed', 'Failed'
    EXPIRED = 'expired', 'Expired'


NON_TERMINAL_IMPORT_STATUSES = (ImportJobStatus.CREATED, ImportJobStatus.RECEIVING)
TERMINAL_IMPORT_STATUSES = (
    ImportJobStatus.SUCCEEDED,
    ImportJobStatus.FAILED,
    ImportJobStatus.EXPIRED,
)


class ImportJob(TimestampedModel):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='import_jobs')
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='import_jobs')
    team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name='import_jobs', null=True, blank=True)
    source_store_id = models.CharField(max_length=255)
    status = models.CharField(max_length=40, choices=ImportJobStatus.choices, default=ImportJobStatus.CREATED)
    manifest = models.JSONField(default=dict, blank=True)
    batches_applied = models.PositiveIntegerField(default=0)
    rows_created = models.PositiveIntegerField(default=0)
    rows_duplicate = models.PositiveIntegerField(default=0)
    last_batch_seq = models.IntegerField(default=-1)
    max_table_phase = models.IntegerField(default=-1)
    applied_batches = models.JSONField(default=dict, blank=True)
    report = models.JSONField(default=dict, blank=True)
    failure_reason = models.CharField(max_length=255, blank=True)
    created_by_api_key = models.UUIDField(null=True, blank=True)
    created_by_identity = models.UUIDField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'project', 'source_store_id'],
                condition=models.Q(status__in=NON_TERMINAL_IMPORT_STATUSES),
                name='uniq_active_import_per_store',
            ),
        ]
        indexes = [
            models.Index(fields=['organization', 'project', 'status']),
            models.Index(fields=['organization', 'created_at']),
        ]
        ordering = ['organization_id', '-created_at']

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_IMPORT_STATUSES

    def clean(self) -> None:
        errors: dict[str, list[str]] = {}
        if self.project_id:
            check_project_organization(errors, 'project', self.project, self.organization_id)
        if self.team_id:
            check_organization_scope(errors, 'team', self.team, self.organization_id)
        raise_scope_errors(errors)

    def __str__(self) -> str:
        return f'ImportJob:{self.source_store_id}:{self.status}'
