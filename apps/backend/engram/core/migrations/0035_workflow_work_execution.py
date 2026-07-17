import structlog
from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

logger = structlog.get_logger(__name__)


def assert_no_linked_v0_active_runs(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    alias = schema_editor.connection.alias
    run_model = apps.get_model('core', 'WorkflowRun')
    offender = (
        run_model.objects.using(alias)
        .filter(work__isnull=False, status__in=('queued', 'running'))
        .order_by('id')
        .first()
    )
    if offender is not None:
        raise RuntimeError(
            f'workflow run {offender.id} is a linked version-0 {offender.status} run; execution activation fails closed'
        )

    return


def backfill_execution_states(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    alias = schema_editor.connection.alias
    work_model = apps.get_model('core', 'WorkflowWork')
    settled = work_model.objects.using(alias).exclude(disposition='required').update(execution_state='settled')

    logger.info(
        'workflow_work_execution_backfilled',
        settled=settled,
    )

    return


def noop(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    return


def guard_reverse_execution_history(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    alias = schema_editor.connection.alias
    run_model = apps.get_model('core', 'WorkflowRun')
    v1_run = run_model.objects.using(alias).filter(execution_contract_version=1).order_by('id').first()
    if v1_run is not None:
        raise RuntimeError(
            f'cannot reverse 0035 while v1 workflow run {v1_run.id} execution history exists'
        )

    work_model = apps.get_model('core', 'WorkflowWork')
    offending_work = (
        work_model.objects.using(alias)
        .filter(
            models.Q(fencing_token__gt=0)
            | models.Q(failure_streak__gt=0)
            | models.Q(lease_expires_at__isnull=False)
            | models.Q(heartbeat_at__isnull=False)
            | models.Q(next_retry_at__isnull=False)
            | ~models.Q(lease_owner='')
            | ~models.Q(blocked_configuration_fingerprint='')
            | ~models.Q(execution_state__in=('ready', 'settled'))
        )
        .order_by('id')
        .first()
    )
    if offending_work is not None:
        raise RuntimeError(
            f'cannot reverse 0035 while v1 workflow work {offending_work.id} execution state exists'
        )

    return


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0034_memory_loop_input_contract'),
    ]

    operations = [
        migrations.RunPython(assert_no_linked_v0_active_runs, noop),
        migrations.AddField(
            model_name='workflowrun',
            name='configuration_fingerprint',
            field=models.CharField(blank=True, db_default='', max_length=64),
        ),
        migrations.AddField(
            model_name='workflowrun',
            name='dispatched_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='workflowrun',
            name='execution_contract_version',
            field=models.PositiveSmallIntegerField(db_default=0, default=0),
        ),
        migrations.AddField(
            model_name='workflowrun',
            name='failure_class',
            field=models.CharField(
                blank=True,
                choices=[
                    ('worker_lost', 'Worker lost'),
                    ('infrastructure_transient', 'Infrastructure transient'),
                    ('provider_transient', 'Provider transient'),
                    ('configuration', 'Configuration'),
                    ('invalid_input', 'Invalid input'),
                    ('unexpected', 'Unexpected'),
                ],
                db_default='',
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name='workflowrun',
            name='failure_code',
            field=models.CharField(blank=True, db_default='', max_length=128),
        ),
        migrations.AddField(
            model_name='workflowrun',
            name='fencing_token',
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='workflowrun',
            name='heartbeat_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='workflowrun',
            name='lease_expires_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='workflowrun',
            name='lease_owner',
            field=models.CharField(blank=True, db_default='', max_length=255),
        ),
        migrations.AddField(
            model_name='workflowrun',
            name='origin',
            field=models.CharField(
                choices=[
                    ('legacy', 'Legacy'),
                    ('automatic', 'Automatic'),
                    ('reconciliation', 'Reconciliation'),
                    ('manual', 'Manual'),
                ],
                db_default='legacy',
                default='legacy',
                max_length=24,
            ),
        ),
        migrations.AddField(
            model_name='workflowwork',
            name='blocked_configuration_fingerprint',
            field=models.CharField(blank=True, db_default='', max_length=64),
        ),
        migrations.AddField(
            model_name='workflowwork',
            name='execution_state',
            field=models.CharField(
                choices=[
                    ('ready', 'Ready'),
                    ('leased', 'Leased'),
                    ('retry_wait', 'Retry wait'),
                    ('blocked', 'Blocked'),
                    ('terminal_failure', 'Terminal failure'),
                    ('settled', 'Settled'),
                ],
                db_default='ready',
                default='ready',
                max_length=24,
            ),
        ),
        migrations.AddField(
            model_name='workflowwork',
            name='failure_streak',
            field=models.PositiveIntegerField(db_default=0, default=0),
        ),
        migrations.AddField(
            model_name='workflowwork',
            name='fencing_token',
            field=models.PositiveBigIntegerField(db_default=0, default=0),
        ),
        migrations.AddField(
            model_name='workflowwork',
            name='heartbeat_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='workflowwork',
            name='lease_expires_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='workflowwork',
            name='lease_owner',
            field=models.CharField(blank=True, db_default='', max_length=255),
        ),
        migrations.AddField(
            model_name='workflowwork',
            name='next_retry_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(backfill_execution_states, noop),
        migrations.AddIndex(
            model_name='workflowrun',
            index=models.Index(fields=['work', 'status', 'created_at'], name='core_run_work_status_time_idx'),
        ),
        migrations.AddIndex(
            model_name='workflowrun',
            index=models.Index(fields=['work', 'fencing_token'], name='core_run_work_token_idx'),
        ),
        migrations.AddIndex(
            model_name='workflowrun',
            index=models.Index(
                fields=['organization', 'project', 'failure_class', 'finished_at'], name='core_run_scope_failclass_idx'
            ),
        ),
        migrations.AddIndex(
            model_name='workflowwork',
            index=models.Index(
                fields=['organization', 'project', 'execution_state', 'next_retry_at'], name='core_work_exec_retry_idx'
            ),
        ),
        migrations.AddIndex(
            model_name='workflowwork',
            index=models.Index(
                fields=['organization', 'project', 'work_type', 'execution_state'], name='core_work_type_exec_idx'
            ),
        ),
        migrations.AddIndex(
            model_name='workflowwork',
            index=models.Index(fields=['execution_state', 'lease_expires_at'], name='core_work_exec_lease_idx'),
        ),
        migrations.AddConstraint(
            model_name='workflowrun',
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(('execution_contract_version', 1), _negated=True),
                    models.Q(
                        ('dispatched_at__isnull', False),
                        ('failure_class', ''),
                        ('failure_code', ''),
                        ('fencing_token__isnull', True),
                        ('finished_at__isnull', True),
                        ('heartbeat_at__isnull', True),
                        ('lease_expires_at__isnull', True),
                        ('lease_owner', ''),
                        ('started_at__isnull', True),
                        ('status', 'queued'),
                    ),
                    models.Q(
                        ('failure_class', ''),
                        ('failure_code', ''),
                        ('fencing_token__gt', 0),
                        ('fencing_token__isnull', False),
                        ('finished_at__isnull', True),
                        ('heartbeat_at__isnull', False),
                        ('lease_expires_at__isnull', False),
                        ('started_at__isnull', False),
                        ('status', 'running'),
                        models.Q(('lease_owner', ''), _negated=True),
                    ),
                    models.Q(
                        ('failure_class', ''),
                        ('failure_code', ''),
                        ('fencing_token__gt', 0),
                        ('fencing_token__isnull', False),
                        ('finished_at__isnull', False),
                        ('started_at__isnull', False),
                        ('status', 'succeeded'),
                        models.Q(('lease_owner', ''), _negated=True),
                    ),
                    models.Q(
                        ('fencing_token__gt', 0),
                        ('fencing_token__isnull', False),
                        ('finished_at__isnull', False),
                        ('started_at__isnull', False),
                        ('status', 'failed'),
                        models.Q(('lease_owner', ''), _negated=True),
                        models.Q(('failure_class', ''), _negated=True),
                        models.Q(('failure_code', ''), _negated=True),
                    ),
                    _connector='OR',
                ),
                name='core_run_v1_status_shape_ck',
            ),
        ),
        migrations.AddConstraint(
            model_name='workflowrun',
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ('configuration_fingerprint', ''), models.Q(('failure_class', 'configuration'), _negated=True)
                    ),
                    models.Q(
                        ('configuration_fingerprint__regex', '^[0-9a-f]{64}$'), ('failure_class', 'configuration')
                    ),
                    _connector='OR',
                ),
                name='core_run_config_fingerprint_ck',
            ),
        ),
        migrations.AddConstraint(
            model_name='workflowrun',
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    ('execution_contract_version', 1), ('fencing_token__isnull', False), ('work__isnull', False)
                ),
                fields=('work', 'fencing_token'),
                name='core_run_v1_work_token_uniq',
            ),
        ),
        migrations.AddConstraint(
            model_name='workflowrun',
            constraint=models.UniqueConstraint(
                condition=models.Q(('execution_contract_version', 1), ('status', 'running'), ('work__isnull', False)),
                fields=('work',),
                name='core_run_v1_one_running_uniq',
            ),
        ),
        migrations.AddConstraint(
            model_name='workflowwork',
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ('execution_state', 'leased'),
                        models.Q(('lease_owner', ''), _negated=True),
                        ('heartbeat_at__isnull', False),
                        ('lease_expires_at__isnull', False),
                        ('lease_expires_at__gt', models.F('heartbeat_at')),
                        ('next_retry_at__isnull', True),
                        ('blocked_configuration_fingerprint', ''),
                    ),
                    models.Q(
                        ('execution_state', 'retry_wait'),
                        ('lease_owner', ''),
                        ('heartbeat_at__isnull', True),
                        ('lease_expires_at__isnull', True),
                        ('next_retry_at__isnull', False),
                        ('blocked_configuration_fingerprint', ''),
                    ),
                    models.Q(
                        ('execution_state', 'blocked'),
                        ('lease_owner', ''),
                        ('heartbeat_at__isnull', True),
                        ('lease_expires_at__isnull', True),
                        ('next_retry_at__isnull', True),
                        ('blocked_configuration_fingerprint__regex', '^[0-9a-f]{64}$'),
                    ),
                    models.Q(
                        ('execution_state__in', ('ready', 'terminal_failure', 'settled')),
                        ('lease_owner', ''),
                        ('heartbeat_at__isnull', True),
                        ('lease_expires_at__isnull', True),
                        ('next_retry_at__isnull', True),
                        ('blocked_configuration_fingerprint', ''),
                    ),
                    _connector='OR',
                ),
                name='core_work_execution_shape_ck',
            ),
        ),
        migrations.AddConstraint(
            model_name='workflowwork',
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(('execution_state', 'settled'), _negated=True),
                    models.Q(('disposition', 'required'), _negated=True),
                    _connector='OR',
                ),
                name='core_work_settled_disposition_ck',
            ),
        ),
        migrations.AddConstraint(
            model_name='workflowwork',
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(('execution_state', 'terminal_failure'), _negated=True),
                    ('disposition', 'required'),
                    _connector='OR',
                ),
                name='core_work_terminal_disposition_ck',
            ),
        ),
        migrations.AddConstraint(
            model_name='workflowwork',
            constraint=models.CheckConstraint(
                condition=models.Q(('fencing_token__gte', 0)), name='core_work_fencing_token_nonneg'
            ),
        ),
        migrations.AddConstraint(
            model_name='workflowwork',
            constraint=models.CheckConstraint(
                condition=models.Q(('failure_streak__gte', 0)), name='core_work_failure_streak_nonneg'
            ),
        ),
        migrations.RunPython(noop, guard_reverse_execution_history),
    ]
