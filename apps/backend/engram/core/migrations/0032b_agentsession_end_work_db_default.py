from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0032_workflowwork_sequence_expand'),
    ]

    operations = [
        migrations.RunSQL(
            sql=('ALTER TABLE "core_agentsession" ALTER COLUMN "end_work_contract_version" SET DEFAULT 0'),
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
