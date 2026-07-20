from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor


def _guard_reverse(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    memory_model = apps.get_model('core', 'Memory')
    audit_model = apps.get_model('core', 'AuditEvent')
    if (
        memory_model.objects.filter(last_confirmed_at__isnull=False).exists()
        or audit_model.objects.filter(event_type='MemoryConfirmed').exists()
    ):
        raise RuntimeError(
            'cannot reverse 0041 while confirmation history exists '
            '(dropping last_confirmed_at would orphan MemoryConfirmed receipts and '
            'silently resurrect decay on confirmed memories)'
        )


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0040_curation_decision'),
    ]

    operations = [
        migrations.AddField(
            model_name='memory',
            name='last_confirmed_at',
            field=models.DateTimeField(null=True, blank=True),
        ),
        migrations.RunPython(migrations.RunPython.noop, _guard_reverse),
    ]
