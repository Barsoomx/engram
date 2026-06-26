import logging
import os

import sentry_sdk
from sentry_sdk.integrations.celery import CeleryIntegration
from sentry_sdk.integrations.django import DjangoIntegration
from sentry_sdk.integrations.logging import LoggingIntegration

from engram.core.observability.logs import configure_structlog
from engram.core.observability.sentryconfig import (
    SENTRY_DSN,
    create_before_send,
    create_before_send_transaction,
    traces_sampler,
)

DEBUG = bool(os.getenv('DEBUG', False))

SENTRY_TAGS = {
    'app_name': os.environ.get('APP_LABEL', 'engram-backend'),
}

DISABLED_LOGGERS = {
    'openai',
    'celery.utils.functional',
    'git.cmd',
    'datadog',
    'numexpr',
    'faker',
}

IGNORE_ERRORS = [
    'celery.exceptions.Retry',
]


def configure_logger(log_level: str = 'INFO', env_profile: str = 'dev') -> None:
    logging.basicConfig(level=log_level)

    if SENTRY_DSN is not None:
        sentry_sdk.init(
            debug=False,
            environment=env_profile,
            dsn=SENTRY_DSN,
            integrations=[
                CeleryIntegration(monitor_beat_tasks=True),
                DjangoIntegration(cache_spans=True),
                LoggingIntegration(event_level=None, level=None),
            ],
            ignore_errors=IGNORE_ERRORS,
            before_send=create_before_send(SENTRY_TAGS),
            before_send_transaction=create_before_send_transaction(SENTRY_TAGS),
            traces_sampler=traces_sampler,
            profiles_sample_rate=float(os.getenv('SENTRY_PROFILES_SAMPLE_RATE', '0.05')),
            send_default_pii=False,
            auto_session_tracking=True,
        )

    configure_structlog(disabled_loggers=DISABLED_LOGGERS)
