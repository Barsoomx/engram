from __future__ import annotations

import os

from django_celery_outbox.app import OutboxCelery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'settings.settings')

app = OutboxCelery('engram')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()
