from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0036_distillation_coverage'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='distillationstage',
            name='core_distill_stage_coord_uniq',
        ),
        migrations.AddConstraint(
            model_name='distillationstage',
            constraint=models.UniqueConstraint(
                fields=('window', 'stage_kind', 'level', 'ordinal', 'policy', 'policy_version', 'policy_role'),
                name='core_distill_stage_coord_uniq',
            ),
        ),
    ]
