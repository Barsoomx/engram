from __future__ import annotations

import importlib
import inspect
import os
from collections.abc import Callable
from unittest.mock import Mock, patch

from django_celery_outbox.app import OutboxCelery

from engram import celeryconfig
from engram.celery_app import app as celery_app
from engram.celery_bootsteps import LivenessProbe
from engram.core import redis_sentinel
from engram.core.domain.event_dispatcher import QUEUE_DOMAIN_EVENTS, CeleryEventDispatcher
from engram.core.domain.events import DomainEvent
from engram.core.models import WorkflowWorkType
from engram.core.redis_sentinel import REDIS_DB_CACHE, DynamicRedisConnectionFactory
from engram.core.retryable_django_task import RetryableTask
from engram.memory import tasks as memory_tasks

_LEASE_MARGIN_SECONDS = 30

_VERSIONED_WORK_TASKS_BY_TYPE = {
    WorkflowWorkType.OBSERVATION_PROCESSING: memory_tasks.process_observation_work_v1,
    WorkflowWorkType.SESSION_DISTILLATION: memory_tasks.distill_session_work_v1,
    WorkflowWorkType.DAILY_DIGEST: memory_tasks.generate_daily_digest_work_v1,
    WorkflowWorkType.WEEKLY_DIGEST: memory_tasks.generate_weekly_digest_work_v1,
}

EXPECTED_QUEUE_NAMES = {
    'engram-realtime',
    'engram-near-realtime',
    'engram-batch',
    'engram-highmemory',
    'engram-domain-events',
}


class SampleCeleryEvent(DomainEvent):
    value: int


def test_celeryconfig_registers_sla_quorum_queues() -> None:
    registered_queues = {queue.name: queue for queue in celeryconfig.task_queues}

    assert set(registered_queues) == EXPECTED_QUEUE_NAMES
    assert celeryconfig.QUEUE_DOMAIN_EVENTS == QUEUE_DOMAIN_EVENTS
    assert celeryconfig.task_default_queue == celeryconfig.QUEUE_NEAR_REALTIME
    assert celeryconfig.task_default_exchange == celeryconfig.QUEUE_NEAR_REALTIME
    assert celeryconfig.task_default_exchange_type == 'topic'
    assert celeryconfig.task_default_routing_key == celeryconfig.QUEUE_NEAR_REALTIME
    assert celeryconfig.task_default_queue_type == 'quorum'

    for queue_name, queue in registered_queues.items():
        assert queue.exchange.name == queue_name
        assert queue.exchange.type == 'topic'
        assert queue.routing_key == queue_name
        assert queue.queue_arguments == {'x-queue-type': 'quorum'}


def test_celeryconfig_uses_confirm_publish_and_quorum_delivery() -> None:
    assert celeryconfig.broker_transport_options == {'confirm_publish': True}
    assert celeryconfig.broker_native_delayed_delivery_queue_type == 'quorum'
    assert celeryconfig.worker_detect_quorum_queues is True
    assert celeryconfig.broker_connection_retry_on_startup is True
    assert celeryconfig.broker_connection_retry is True
    assert celeryconfig.broker_connection_max_retries is None
    assert celeryconfig.worker_enable_soft_shutdown_on_idle is True
    assert celeryconfig.worker_soft_shutdown_timeout == 60


def test_celeryconfig_uses_sentinel_result_backend_when_enabled() -> None:
    env = {
        'REDIS_USE_SENTINEL': '1',
        'REDIS_SENTINEL_NODE': 'redis-a:26379,redis-b:26379',
        'REDIS_PASS': 'secret',
        'ENGRAM_CELERY_RESULT_BACKEND_DB': '5',
    }
    tracked_env = set(env) | {'ENGRAM_CELERY_RESULT_BACKEND', 'ENGRAM_REDIS_URL'}
    original_env = {key: os.environ.get(key) for key in tracked_env}

    try:
        os.environ.update(env)
        os.environ.pop('ENGRAM_CELERY_RESULT_BACKEND', None)
        os.environ.pop('ENGRAM_REDIS_URL', None)
        reloaded_redis_sentinel = importlib.reload(redis_sentinel)
        reloaded_celeryconfig = importlib.reload(celeryconfig)

        assert reloaded_celeryconfig.CELERY_RESULT_BACKEND_DB == 5
        assert reloaded_celeryconfig.result_backend == (
            'sentinel://:secret@redis-a:26379/5;sentinel://:secret@redis-b:26379/5'
        )
        assert reloaded_celeryconfig.result_backend_transport_options['master_name'] == 'mymaster'
        assert reloaded_celeryconfig.result_backend_transport_options['sentinel_kwargs']['password'] == 'secret'
        assert (
            reloaded_celeryconfig.result_backend_transport_options['sentinel_kwargs']['retry']
            is reloaded_redis_sentinel.REDIS_RETRY_KWARGS['retry']
        )
    finally:
        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        importlib.reload(redis_sentinel)
        importlib.reload(celeryconfig)


def test_celeryconfig_uses_json_and_eager_result_contract() -> None:
    assert celeryconfig.task_serializer == 'json'
    assert celeryconfig.accept_content == ['json']
    assert celeryconfig.result_serializer == 'json'
    assert celeryconfig.enable_utc is True
    assert celeryconfig.result_expires == 3600
    assert celeryconfig.task_ignore_result is True
    assert celeryconfig.task_store_eager_result == celeryconfig.task_always_eager


def test_celery_app_uses_outbox_transport_and_foundation_config() -> None:
    engram_package = importlib.import_module('engram')

    assert isinstance(celery_app, OutboxCelery)
    assert celery_app.main == 'engram'
    assert engram_package.celery_app is celery_app
    assert celery_app.task_cls == 'engram.core.retryable_django_task.RetryableTask'
    assert set(celery_app.conf.include or ()) == {'engram.memory.tasks'}
    assert celery_app.conf.broker_transport_options == {'confirm_publish': True}
    assert celery_app.conf.task_default_queue == celeryconfig.QUEUE_NEAR_REALTIME
    assert celery_app.conf.task_default_queue_type == 'quorum'
    effective_reconnect_config = {
        'broker_connection_retry_on_startup': celery_app.conf.broker_connection_retry_on_startup,
        'broker_connection_retry': celery_app.conf.broker_connection_retry,
        'broker_connection_max_retries': celery_app.conf.broker_connection_max_retries,
        'worker_enable_soft_shutdown_on_idle': celery_app.conf.worker_enable_soft_shutdown_on_idle,
    }
    assert effective_reconnect_config == {
        'broker_connection_retry_on_startup': True,
        'broker_connection_retry': True,
        'broker_connection_max_retries': None,
        'worker_enable_soft_shutdown_on_idle': True,
    }
    assert LivenessProbe in celery_app.steps['worker']


def test_versioned_work_hard_time_limit_fits_inside_lease() -> None:
    for work_type, task in _VERSIONED_WORK_TASKS_BY_TYPE.items():
        lease_seconds = memory_tasks.LEASE_BY_WORK_TYPE[work_type].total_seconds()

        assert task.soft_time_limit is not None, f'{work_type} task must set a soft time limit'
        assert task.time_limit is not None, f'{work_type} task must set a hard time limit'
        assert task.soft_time_limit < task.time_limit
        assert task.time_limit + _LEASE_MARGIN_SECONDS <= lease_seconds


def test_celery_support_classes_are_available() -> None:
    assert issubclass(RetryableTask, object)
    assert DynamicRedisConnectionFactory().redis_db == REDIS_DB_CACHE


def test_celery_event_dispatcher_wraps_handlers_with_concrete_event_type() -> None:
    captured: dict[str, Callable] = {}

    def handler(_: DomainEvent) -> None:
        return None

    def shared_task_stub(**_: object) -> Callable:
        def decorator(func: Callable) -> Mock:
            captured['func'] = func
            task = Mock()
            task.__name__ = getattr(func, '__name__', 'task')
            return task

        return decorator

    dispatcher = CeleryEventDispatcher()

    with patch('engram.core.domain.event_dispatcher.shared_task', shared_task_stub):
        dispatcher.add_handler(SampleCeleryEvent, handler, queue=QUEUE_DOMAIN_EVENTS)

    wrapped_handler = captured['func']
    annotation = inspect.signature(wrapped_handler).parameters['event'].annotation
    parsed_event = annotation.model_validate(SampleCeleryEvent(value=7).model_dump())

    assert annotation is SampleCeleryEvent
    assert parsed_event.value == 7


@patch('engram.core.domain.event_dispatcher.transaction')
def test_celery_event_dispatcher_delays_handlers_after_current_transaction(m_transaction: Mock) -> None:
    dispatcher = CeleryEventDispatcher()
    dispatcher._handlers.clear()
    handler = Mock()
    event = SampleCeleryEvent(value=11)
    m_transaction.get_connection.return_value.in_atomic_block = True

    dispatcher._run_handler(handler, event)

    handler.delay_on_commit.assert_called_once_with(event={'value': 11})
    handler.delay.assert_not_called()
