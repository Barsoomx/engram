from __future__ import annotations

import os

import redis
from redis.backoff import ExponentialBackoff
from redis.retry import Retry


class DynamicRedisConnectionFactory:
    def __init__(self, db: int | str | None = None, decode_responses: bool = True) -> None:
        self._sentinel_service_name = 'mymaster'

        self._use_sentinel = REDIS_USE_SENTINEL

        self._redis_host = REDIS_HOST
        self._redis_port = REDIS_PORT
        self._redis_pass = REDIS_PASS
        self._redis_sentinels = REDIS_SENTINELS

        self.redis_db = REDIS_DB_CACHE if db is None else db
        self.decode_responses = decode_responses

    def get_redis_client(self) -> redis.Redis:
        if not self._use_sentinel:
            return self._get_redis_client()
        return self._get_sentinel_client()

    def get_cacheops_params(self) -> dict:
        if not self._use_sentinel:
            return {
                'host': self._redis_host,
                'port': self._redis_port,
                'password': self._redis_pass,
                'db': self.redis_db,
            }
        return {
            'locations': self._redis_sentinels,
            'service_name': self._sentinel_service_name,
            'db': self.redis_db,
            'password': self._redis_pass,
            'sentinel_kwargs': {
                'password': self._redis_pass,
                **REDIS_RETRY_KWARGS,
            },
            **REDIS_RETRY_KWARGS,
        }

    def _get_redis_client(self) -> redis.Redis:
        return redis.Redis(
            host=self._redis_host,
            port=self._redis_port,
            db=self.redis_db,
            password=self._redis_pass,
            decode_responses=self.decode_responses,
            **REDIS_RETRY_KWARGS,
        )

    def _get_sentinel_client(self) -> redis.Redis:
        sentinel = redis.sentinel.Sentinel(
            self._redis_sentinels,
            sentinel_kwargs={
                'password': self._redis_pass,
                **REDIS_RETRY_KWARGS,
            },
            decode_responses=self.decode_responses,
            password=self._redis_pass,
            **REDIS_RETRY_KWARGS,
        )
        return sentinel.master_for(self._sentinel_service_name, db=self.redis_db)


REDIS_RETRY_STRATEGY = Retry(ExponentialBackoff(), 3)
REDIS_RETRY_KWARGS = {
    'retry_on_error': [
        redis.exceptions.ConnectionError,
        redis.exceptions.TimeoutError,
        redis.exceptions.ReadOnlyError,
        redis.exceptions.TryAgainError,
        redis.exceptions.BusyLoadingError,
    ],
    'retry': REDIS_RETRY_STRATEGY,
}

REDIS_USE_SENTINEL = bool(int(os.environ.get('REDIS_USE_SENTINEL', 0)))
REDIS_SENTINELS = [tuple(node.split(':')) for node in os.environ.get('REDIS_SENTINEL_NODE', '').split(',') if node]

REDIS_HOST = os.getenv('REDIS_HOST', 'redis')
REDIS_PORT = os.getenv('REDIS_PORT', '6379')
REDIS_PASS = os.getenv('REDIS_PASS', None)
REDIS_DB_CACHE = int(os.getenv('REDIS_DB_CACHE', '3'))
REDIS_PRIMARY_DB = int(os.getenv('REDIS_PRIMARY_DB', '0'))
REDIS_MUTEX_DB = int(os.getenv('REDIS_MUTEX_DB', '0'))
