import os
from typing import Any, NoReturn, Protocol

import structlog
from django.core.cache import cache as django_cache

logger = structlog.get_logger(__name__)

CELERY_RETRIES_CHECKER_CACHE_TTL = int(
    os.getenv(
        'CELERY_RETRIES_CHECKER_CACHE_TTL',
        60 * 60 * 3,
    ),
)
CELERY_TASK_MAX_ATTEMPTS = int(os.getenv('CELERY_TASK_MAX_ATTEMPTS', 5))
CELERY_TASK_RETRY_DELAY = int(os.getenv('CELERY_TASK_RETRY_DELAY', 180))


class Cache(Protocol):
    def get(self, key: str) -> Any:
        pass

    def set(self, key: str, value: Any, ttl: int) -> None:
        pass


class MaxRetriesExceededError(Exception): ...


class RetriesChecker:
    def __init__(
        self,
        cache_key: str,
        max_retries: int = CELERY_TASK_MAX_ATTEMPTS,
        ttl: int = CELERY_RETRIES_CHECKER_CACHE_TTL,
        cache: Cache | None = None,
    ) -> None:
        self._cache_key = cache_key
        self._max_retries = max_retries
        self._ttl = ttl
        self._cache = cache or django_cache

    def check(self) -> NoReturn:
        try:
            custom_retries = self._cache.get(self._cache_key) or 0
            custom_retries = int(custom_retries)
        except Exception as e:
            logger.warning(
                'can not get retries from redis cache',
                cache_key=self._cache_key,
                error=e,
            )
            custom_retries = 0

        if custom_retries > self._max_retries:
            logger.error(
                'MaxRetriesExceededError',
                cache_key=self._cache_key,
                max_retries=self._max_retries,
                custom_retries=custom_retries,
            )
            raise MaxRetriesExceededError(
                'Maximum number of retries exceeded',
            )

        try:
            self._cache.set(self._cache_key, custom_retries + 1, self._ttl)
        except Exception as e:
            logger.warning(
                'can not set retries to redis cache',
                cache_key=self._cache_key,
                error=e,
            )
