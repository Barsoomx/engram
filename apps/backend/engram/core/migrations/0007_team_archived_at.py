from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0006_embedding_pgvector'),
    ]

    operations = [
        migrations.AddField(
            model_name='team',
            name='archived_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
