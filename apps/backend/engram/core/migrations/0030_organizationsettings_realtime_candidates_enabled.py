from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0029_memoryreviewexample'),
    ]

    operations = [
        migrations.AddField(
            model_name='organizationsettings',
            name='realtime_candidates_enabled',
            field=models.BooleanField(default=False),
        ),
    ]
