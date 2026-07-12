import structlog
from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.models import Count, F, Max, Q
from django.db.models.functions import Coalesce

logger = structlog.get_logger(__name__)

DROP_EXPAND_SQL = 'ALTER TABLE "core_raweventenvelope" DROP CONSTRAINT IF EXISTS "core_raw_norm_expand_valid"'
DROP_FINAL_SQL = 'ALTER TABLE "core_raweventenvelope" DROP CONSTRAINT IF EXISTS "core_raw_norm_final_valid"'

ADD_FINAL_SQL = """
ALTER TABLE "core_raweventenvelope"
ADD CONSTRAINT "core_raw_norm_final_valid" CHECK (
    (
        "normalization_contract_version" = 0
        AND "normalization_disposition" IS NULL
        AND "normalization_reason" IS NULL
    )
    OR (
        "normalization_contract_version" = 1
        AND "normalization_disposition" IS NOT NULL
        AND "normalization_disposition" = 'observation'
        AND "normalization_reason" IS NULL
    )
    OR (
        "normalization_contract_version" = 1
        AND "normalization_disposition" IS NOT NULL
        AND "normalization_disposition" = 'no_op'
        AND "normalization_reason" IS NOT NULL
        AND "normalization_reason" = 'evidence_only'
    )
)
"""

ADD_PERMISSIVE_EXPAND_SQL = """
ALTER TABLE "core_raweventenvelope"
ADD CONSTRAINT "core_raw_norm_expand_valid" CHECK (
    (
        "normalization_contract_version" IS NULL
        AND "normalization_disposition" IS NULL
        AND "normalization_reason" IS NULL
    )
    OR (
        "normalization_contract_version" = 0
        AND "normalization_disposition" IS NULL
        AND "normalization_reason" IS NULL
    )
    OR (
        "normalization_contract_version" = 1
        AND "normalization_disposition" = 'observation'
        AND "normalization_reason" IS NULL
    )
    OR (
        "normalization_contract_version" = 1
        AND "normalization_disposition" = 'no_op'
        AND "normalization_reason" = 'evidence_only'
    )
)
"""

FINAL_CONSTRAINT = models.CheckConstraint(
    condition=(
        models.Q(
            normalization_contract_version=0,
            normalization_disposition__isnull=True,
            normalization_reason__isnull=True,
        )
        | models.Q(
            normalization_contract_version=1,
            normalization_disposition__isnull=False,
            normalization_disposition='observation',
            normalization_reason__isnull=True,
        )
        | models.Q(
            normalization_contract_version=1,
            normalization_disposition__isnull=False,
            normalization_disposition='no_op',
            normalization_reason__isnull=False,
            normalization_reason='evidence_only',
        )
    ),
    name='core_raw_norm_final_valid',
)


def _assert_raw_normalization(raw_event_model: type[models.Model], alias: str) -> None:
    partial_tuple = Q(normalization_contract_version__isnull=True) & (
        Q(normalization_disposition__isnull=False) | Q(normalization_reason__isnull=False)
    )
    unknown_version = Q(normalization_contract_version__isnull=False) & ~Q(normalization_contract_version__in=(0, 1))
    offender = raw_event_model.objects.using(alias).filter(partial_tuple | unknown_version).order_by('id').first()
    if offender is not None:
        raise RuntimeError(
            f'raw event {offender.id} has an invalid normalization tuple '
            f'(version={offender.normalization_contract_version}, '
            f'disposition={offender.normalization_disposition}, '
            f'reason={offender.normalization_reason})'
        )

    return


def _assert_observation_sequences(observation_model: type[models.Model], alias: str) -> None:
    unsequenced = (
        observation_model.objects.using(alias)
        .filter(Q(session_sequence__isnull=True) | Q(session_sequence__lte=0))
        .order_by('id')
        .first()
    )
    if unsequenced is not None:
        raise RuntimeError(
            f'observation {unsequenced.id} has a non-positive session_sequence {unsequenced.session_sequence}'
        )

    duplicate = (
        observation_model.objects.using(alias)
        .values('session_id', 'session_sequence')
        .annotate(occurrence_count=Count('id'))
        .filter(occurrence_count__gt=1)
        .order_by('session_id', 'session_sequence')
        .first()
    )
    if duplicate is not None:
        raise RuntimeError(
            f'session {duplicate["session_id"]} has duplicate session_sequence {duplicate["session_sequence"]}'
        )

    return


def _assert_session_cursors(session_model: type[models.Model], alias: str) -> None:
    offender = (
        session_model.objects.using(alias)
        .annotate(max_sequence=Coalesce(Max('observations__session_sequence'), 0))
        .filter(Q(observation_sequence_cursor__isnull=True) | ~Q(observation_sequence_cursor=F('max_sequence')))
        .order_by('id')
        .first()
    )
    if offender is not None:
        raise RuntimeError(
            f'session {offender.id} cursor {offender.observation_sequence_cursor} does not match its useful watermark'
        )

    return


def run_preflight(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    alias = schema_editor.connection.alias
    _assert_raw_normalization(apps.get_model('core', 'RawEventEnvelope'), alias)
    _assert_observation_sequences(apps.get_model('core', 'Observation'), alias)
    _assert_session_cursors(apps.get_model('core', 'AgentSession'), alias)

    return


def mark_null_normalization_v0(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    alias = schema_editor.connection.alias
    raw_event_model = apps.get_model('core', 'RawEventEnvelope')
    marked = (
        raw_event_model.objects.using(alias)
        .filter(
            normalization_contract_version__isnull=True,
            normalization_disposition__isnull=True,
            normalization_reason__isnull=True,
        )
        .update(normalization_contract_version=0)
    )

    logger.info(
        'memory_loop_input_contract_applied',
        marked_v0=marked,
    )

    return


def noop(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    return


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0033_backfill_observation_sequence'),
    ]

    operations = [
        migrations.RunPython(run_preflight, noop),
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveConstraint(model_name='raweventenvelope', name='core_raw_norm_expand_valid'),
            ],
            database_operations=[
                migrations.RunSQL(sql=DROP_EXPAND_SQL, reverse_sql=ADD_PERMISSIVE_EXPAND_SQL),
            ],
        ),
        migrations.RunPython(mark_null_normalization_v0, noop),
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddConstraint(model_name='raweventenvelope', constraint=FINAL_CONSTRAINT),
            ],
            database_operations=[
                migrations.RunSQL(sql=ADD_FINAL_SQL, reverse_sql=DROP_FINAL_SQL),
            ],
        ),
        migrations.AlterField(
            model_name='raweventenvelope',
            name='normalization_contract_version',
            field=models.PositiveSmallIntegerField(),
        ),
        migrations.AlterField(
            model_name='observation',
            name='session_sequence',
            field=models.PositiveBigIntegerField(),
        ),
        migrations.RemoveConstraint(
            model_name='observation',
            name='core_obs_session_seq_pos',
        ),
        migrations.AddConstraint(
            model_name='observation',
            constraint=models.CheckConstraint(
                condition=models.Q(session_sequence__gt=0),
                name='core_obs_session_seq_pos',
            ),
        ),
        migrations.AlterField(
            model_name='agentsession',
            name='observation_sequence_cursor',
            field=models.PositiveBigIntegerField(db_default=0),
        ),
    ]
