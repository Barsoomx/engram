from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0017_retrievaldocument_embedding_hnsw_index'),
    ]

    operations = [
        migrations.AddField(
            model_name='organizationsettings',
            name='lexical_fusion_enabled',
            field=models.BooleanField(default=False),
        ),
    ]
