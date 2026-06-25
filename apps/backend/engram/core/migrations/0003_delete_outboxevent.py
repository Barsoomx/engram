from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0002_remove_outboxevent_core_outbox_unique_idempotency_key_per_event_and_more'),
    ]

    operations = [
        migrations.DeleteModel(name='OutboxEvent'),
    ]
