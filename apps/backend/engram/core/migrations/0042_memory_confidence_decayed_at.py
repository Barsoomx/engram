from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0041_retrieval_document_projection_hash_db_defaults'),
    ]

    operations = [
        migrations.AddField(
            model_name='memory',
            name='confidence_decayed_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
