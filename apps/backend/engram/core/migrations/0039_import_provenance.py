import django.db.models.deletion
from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor


def _backfill_distillation(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    source_model = apps.get_model('core', 'MemoryCandidateSource')
    source_model.objects.filter(source_kind__isnull=True).update(source_kind='distillation')


def _guard_reverse(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    source_model = apps.get_model('core', 'MemoryCandidateSource')
    if source_model.objects.filter(source_kind='import').exists():
        raise RuntimeError('cannot reverse 0039 while import provenance exists')


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0038_atomic_memory_transitions'),
    ]

    operations = [
        migrations.AddField(
            model_name='memorycandidatesource',
            name='source_kind',
            field=models.CharField(
                choices=[('distillation', 'Distillation'), ('import', 'Import')],
                db_default='distillation',
                default='distillation',
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='memorycandidatesource',
            name='import_source',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='memory_candidate_sources',
                to='core.observationsource',
            ),
        ),
        migrations.AlterField(
            model_name='memorycandidatesource',
            name='window',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='candidate_sources',
                to='core.distillationwindow',
            ),
        ),
        migrations.AlterField(
            model_name='memorycandidatesource',
            name='stage',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='candidate_sources',
                to='core.distillationstage',
            ),
        ),
        migrations.RemoveConstraint(
            model_name='memorycandidatesource',
            name='core_candidate_source_uniq',
        ),
        migrations.AddConstraint(
            model_name='memorycandidatesource',
            constraint=models.UniqueConstraint(
                condition=models.Q(source_kind='distillation'),
                fields=('candidate', 'window', 'observation'),
                name='core_candidate_source_distill_uniq',
            ),
        ),
        migrations.AddConstraint(
            model_name='memorycandidatesource',
            constraint=models.UniqueConstraint(
                condition=models.Q(source_kind='import'),
                fields=('candidate', 'import_source'),
                name='core_candidate_source_import_uniq',
            ),
        ),
        migrations.AddConstraint(
            model_name='memorycandidatesource',
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        source_kind='distillation',
                        window__isnull=False,
                        stage__isnull=False,
                        import_source__isnull=True,
                    )
                    | models.Q(
                        source_kind='import',
                        window__isnull=True,
                        stage__isnull=True,
                        import_source__isnull=False,
                    )
                ),
                name='core_candidate_source_shape_ck',
            ),
        ),
        migrations.RunPython(_backfill_distillation, _guard_reverse),
    ]
