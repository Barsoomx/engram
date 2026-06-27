from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0008_project_archived_at'),
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
