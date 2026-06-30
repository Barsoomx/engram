import django.db.models.deletion
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0010_memorylink_link_type_choices'),
    ]

    operations = [
        migrations.CreateModel(
            name='OrganizationSettings',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('hybrid_retrieval_enabled', models.BooleanField(default=True)),
                ('require_provenance', models.BooleanField(default=False)),
                (
                    'organization',
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='settings',
                        to='core.organization',
                    ),
                ),
            ],
            options={
                'abstract': False,
            },
        ),
    ]
