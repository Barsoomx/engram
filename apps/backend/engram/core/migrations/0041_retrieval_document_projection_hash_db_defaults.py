from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0040_curation_decision'),
    ]

    operations = [
        migrations.RunSQL(
            sql=(
                'ALTER TABLE "core_retrievaldocument" '
                'ALTER COLUMN "exact_projection_hash" SET DEFAULT \'\', '
                'ALTER COLUMN "embedding_projection_hash" SET DEFAULT \'\''
            ),
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
