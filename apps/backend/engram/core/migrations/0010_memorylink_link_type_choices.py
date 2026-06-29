from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0009_workflow_run'),
    ]

    operations = [
        migrations.AlterField(
            model_name='memorylink',
            name='link_type',
            field=models.CharField(
                choices=[
                    ('file', 'File'),
                    ('symbol', 'Symbol'),
                    ('commit', 'Commit'),
                    ('issue', 'Issue'),
                    ('narrowed_by', 'Narrowed by'),
                    ('superseded_by', 'Superseded by'),
                ],
                max_length=40,
            ),
        ),
    ]
