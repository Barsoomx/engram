import django.db.models.deletion
from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor


def _guard_reverse(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    source_model = apps.get_model('core', 'MemoryCandidateSource')
    if source_model.objects.filter(source_kind='agent_proposal').exists():
        raise RuntimeError('cannot reverse 0044 while agent proposal sources exist')


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0043_curation_decision_evidence_context'),
    ]

    operations = [
        migrations.AlterField(
            model_name='memorycandidatesource',
            name='source_kind',
            field=models.CharField(
                choices=[
                    ('distillation', 'Distillation'),
                    ('import', 'Import'),
                    ('agent_proposal', 'Agent Proposal'),
                ],
                db_default='distillation',
                default='distillation',
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name='memorycandidatesource',
            name='observation',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='candidate_sources',
                to='core.observation',
            ),
        ),
        migrations.RemoveConstraint(
            model_name='memorycandidatesource',
            name='core_candidate_source_shape_ck',
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
                        observation__isnull=False,
                    )
                    | models.Q(
                        source_kind='import',
                        window__isnull=True,
                        stage__isnull=True,
                        import_source__isnull=False,
                        observation__isnull=False,
                    )
                    | models.Q(
                        source_kind='agent_proposal',
                        window__isnull=True,
                        stage__isnull=True,
                        import_source__isnull=True,
                        observation__isnull=True,
                    )
                ),
                name='core_candidate_source_shape_ck',
            ),
        ),
        migrations.AddConstraint(
            model_name='memorycandidatesource',
            constraint=models.UniqueConstraint(
                condition=models.Q(source_kind='agent_proposal'),
                fields=('candidate',),
                name='core_candidate_source_agent_uniq',
            ),
        ),
        migrations.RunPython(migrations.RunPython.noop, _guard_reverse),
    ]
