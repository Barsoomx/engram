from __future__ import annotations

import os
from typing import Any

import structlog
from celery import signals
from celery.signals import beat_init, worker_ready, worker_shutdown
from django_celery_outbox import OutboxCelery
from django_structlog.celery.steps import DjangoStructLogInitStep

from engram.celery_bootsteps import LivenessProbe
from engram.celeryconfig import READINESS_FILE

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'settings.settings')

app = OutboxCelery(
    'engram',
    task_cls='engram.core.retryable_django_task.RetryableTask',
    include=[
        'engram.memory.tasks',
    ],
)


app.steps['worker'].add(LivenessProbe)
app.steps['worker'].add(DjangoStructLogInitStep)
app.config_from_object('engram.celeryconfig')
app.autodiscover_tasks()

LOG_FORMATTER = {
    'json': structlog.stdlib.ProcessorFormatter(structlog.processors.JSONRenderer()),
    'console': structlog.stdlib.ProcessorFormatter(structlog.dev.ConsoleRenderer()),
}

formatter = LOG_FORMATTER[os.environ.get('ENGRAM_LOG_FORMATTER', os.environ.get('LOG_FORMATTER', 'console'))]


@signals.after_setup_task_logger.connect
def after_setup_celery_task_logger(
    logger: Any,
    **kwargs: Any,
) -> None:
    if logger.handlers:
        logger.handlers[0].setFormatter(formatter)


@signals.after_setup_logger.connect
def after_setup_celery_logger(
    logger: Any,
    **kwargs: Any,
) -> None:
    if logger.handlers:
        logger.handlers[0].setFormatter(formatter)


@worker_ready.connect
def worker_ready(**_: Any) -> None:
    READINESS_FILE.touch()


@worker_shutdown.connect
def worker_shutdown(**_: Any) -> None:
    READINESS_FILE.unlink(missing_ok=True)


@beat_init.connect
def beat_ready(**_: Any) -> None:
    READINESS_FILE.touch()
