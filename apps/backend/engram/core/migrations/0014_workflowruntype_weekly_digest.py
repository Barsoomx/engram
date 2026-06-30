from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0013_organizationsettings_distillation_auto_approve_threshold_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='workflowrun',
            name='run_type',
            field=models.CharField(
                choices=[
                    ('daily_digest', 'Daily Digest'),
                    ('observation_processing', 'Observation Processing'),
                    ('weekly_digest', 'Weekly Digest'),
                ],
                max_length=40,
            ),
        ),
    ]
