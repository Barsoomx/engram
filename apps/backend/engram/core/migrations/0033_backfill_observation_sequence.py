import uuid

import structlog
from django.apps.registry import Apps
from django.db import DatabaseError, migrations, models, transaction
from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.models import Count

logger = structlog.get_logger(__name__)

MAX_OBSERVATIONS_PER_SESSION = 10_000
UPDATE_BATCH_SIZE = 500
SESSION_LOCK_TIMEOUT = '5s'
STATEMENT_TIMEOUT = '60s'


def _assert_session_cap(observation_model: type[models.Model], alias: str) -> None:
    offender = (
        observation_model.objects.using(alias)
        .values('session_id')
        .annotate(observation_count=Count('id'))
        .filter(observation_count__gt=MAX_OBSERVATIONS_PER_SESSION)
        .order_by('session_id')
        .first()
    )
    if offender is not None:
        raise RuntimeError(
            f'session {offender["session_id"]} has {offender["observation_count"]} '
            f'observations, exceeding cap {MAX_OBSERVATIONS_PER_SESSION}'
        )

    return


def _process_session(
    connection: BaseDatabaseWrapper,
    alias: str,
    session_model: type[models.Model],
    observation_model: type[models.Model],
    session_id: uuid.UUID,
) -> bool:
    with transaction.atomic(using=alias):
        with connection.cursor() as cursor:
            cursor.execute(f"SET LOCAL lock_timeout = '{SESSION_LOCK_TIMEOUT}'")
            cursor.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")

        session = session_model.objects.using(alias).select_for_update(of=('self',)).get(id=session_id)
        children = list(
            observation_model.objects.using(alias)
            .filter(session_id=session_id)
            .order_by('created_at', 'id')
            .only('id', 'session_sequence')
        )
        count = len(children)
        sequences = [child.session_sequence for child in children]

        if sequences == list(range(1, count + 1)) and session.observation_sequence_cursor == count:
            return True

        observation_model.objects.using(alias).filter(session_id=session_id).update(session_sequence=None)
        for index, child in enumerate(children, start=1):
            child.session_sequence = index

        for start in range(0, count, UPDATE_BATCH_SIZE):
            chunk = children[start : start + UPDATE_BATCH_SIZE]
            observation_model.objects.using(alias).bulk_update(
                chunk, ['session_sequence'], batch_size=UPDATE_BATCH_SIZE
            )

        session_model.objects.using(alias).filter(id=session_id).update(observation_sequence_cursor=count)

    return False


def backfill_observation_sequences(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    connection = schema_editor.connection
    alias = connection.alias
    session_model = apps.get_model('core', 'AgentSession')
    observation_model = apps.get_model('core', 'Observation')

    _assert_session_cap(observation_model, alias)

    session_ids = list(session_model.objects.using(alias).order_by('id').values_list('id', flat=True))

    completed = 0
    skipped = 0
    failed = 0
    first_error = None

    for session_id in session_ids:
        try:
            was_skipped = _process_session(connection, alias, session_model, observation_model, session_id)
        except DatabaseError as error:
            failed += 1
            if first_error is None:
                first_error = error

            continue

        if was_skipped:
            skipped += 1
        else:
            completed += 1

    logger.info(
        'backfill_observation_sequences_finished',
        completed=completed,
        skipped=skipped,
        failed=failed,
    )

    if first_error is not None:
        raise first_error

    return


def noop_reverse(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    return


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ('core', '0032b_agentsession_end_work_db_default'),
    ]

    operations = [
        migrations.RunPython(backfill_observation_sequences, noop_reverse),
    ]
