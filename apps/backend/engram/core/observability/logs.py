import logging

import sentry_sdk
import structlog
from structlog_sentry import SentryProcessor

from engram.core.observability import sentryconfig

DEFAULT_DISABLED_LOGGERS = {
    'sentry_sdk',
}


def add_sentry_tags_to_log(
    logger: logging.Logger,
    _: str,
    event_dict: structlog.stdlib.EventDict,
) -> structlog.stdlib.EventDict:
    span = sentry_sdk.get_current_span()
    if span:
        event_dict['sentry_trace_id'] = span.trace_id
        event_dict['sentry_span_id'] = span.span_id

        if span.containing_transaction:
            event_dict['transaction'] = span.containing_transaction.name

    return event_dict


def configure_structlog(
    disabled_loggers: set[str] | None = None,
    extra_processors: list | None = None,
) -> None:
    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.filter_by_level,
        structlog.processors.TimeStamper(fmt='iso', key='date'),
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.StackInfoRenderer(),
        SentryProcessor(
            active=sentryconfig.SENTRY_DSN is not None,
            event_level=sentryconfig.EVENT_LEVEL,
            as_context=True,
            tag_keys='__all__',
        ),
        add_sentry_tags_to_log,
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
    ]

    if extra_processors:
        processors.extend(extra_processors)

    processors.append(structlog.stdlib.ProcessorFormatter.wrap_for_formatter)

    structlog.configure(
        context_class=dict,
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )

    loggers_to_disable = DEFAULT_DISABLED_LOGGERS | (disabled_loggers or set())
    for logger_name in loggers_to_disable:
        logging.getLogger(logger_name).setLevel(logging.ERROR)
