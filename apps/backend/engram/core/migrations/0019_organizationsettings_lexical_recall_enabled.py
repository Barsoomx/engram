from django.contrib.postgres.operations import TrigramExtension
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0018_organizationsettings_lexical_fusion_enabled'),
    ]

    operations = [
        TrigramExtension(),
        migrations.AddField(
            model_name='organizationsettings',
            name='lexical_recall_enabled',
            field=models.BooleanField(default=False),
        ),
    ]
