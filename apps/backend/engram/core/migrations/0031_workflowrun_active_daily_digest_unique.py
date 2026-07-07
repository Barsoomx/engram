from __future__ import annotations

from django.db import migrations, models

_ACTIVE_STATUSES = ('queued', 'running')


def _fail_duplicate_active_daily_digests(apps, schema_editor):
    workflow_run = apps.get_model('core', 'WorkflowRun')
    active = workflow_run.objects.filter(
        run_type='daily_digest',
        status__in=_ACTIVE_STATUSES,
    ).order_by('project_id', '-created_at')
    keep_by_project = {}
    superseded_ids = []
    for run in active.only('id', 'project_id'):
        if run.project_id in keep_by_project:
            superseded_ids.append(run.id)
        else:
            keep_by_project[run.project_id] = run.id
    if superseded_ids:
        workflow_run.objects.filter(id__in=superseded_ids).update(
            status='failed',
            failure_reason='superseded_duplicate_active_run',
        )


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0030_organizationsettings_realtime_candidates_enabled'),
    ]

    operations = [
        migrations.RunPython(
            _fail_duplicate_active_daily_digests,
            migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name='workflowrun',
            constraint=models.UniqueConstraint(
                fields=['project', 'run_type'],
                condition=models.Q(run_type='daily_digest', status__in=('queued', 'running')),
                name='core_workflowrun_uniq_active_daily_digest',
            ),
        ),
    ]
