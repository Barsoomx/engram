from __future__ import annotations

from typing import Any

import structlog
from celery.contrib.django.task import DjangoTask
from sentry_sdk import capture_exception

from engram.core.retries_checker import RetriesChecker

logger = structlog.get_logger(__name__)


class RetryableTask(DjangoTask):
    def __call__(self, *args: Any, **kwargs: dict[str, Any]) -> Any:
        if self.request.id and self.max_retries:
            self._check_custom_retry_policy()

        logger.info(
            'task started',
            attempt=self.request.retries,
            max_retries=self.max_retries,
        )
        try:
            result = super().__call__(*args, **kwargs)
        except Exception as exc:
            logger.exception(
                'task failed',
                attempt=self.request.retries,
                exc=exc,
            )
            capture_exception(exc)
            raise

        logger.info('task completed', attempt=self.request.retries)

        return result

    def _check_custom_retry_policy(self) -> None:
        retries_checker = RetriesChecker(
            cache_key=f'celery:custom_retries:{self.name}:{self.request.id}',
            max_retries=self.max_retries,
        )
        retries_checker.check()
