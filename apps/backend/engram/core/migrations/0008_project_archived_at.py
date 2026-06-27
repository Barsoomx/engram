from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0007_team_archived_at'),
    ]

    operations = [
        migrations.AddField(
            model_name='project',
            name='archived_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
