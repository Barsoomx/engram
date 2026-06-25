from __future__ import annotations

import os
from pathlib import Path

from django.apps import apps
from django.db.models import Model
from kombu import Exchange, Queue
from kombu.utils.json import register_type

HEARTBEAT_FILE = Path('/tmp/engram_celery_worker_heartbeat')  # noqa: S108
READINESS_FILE = Path('/tmp/engram_celery_ready')  # noqa: S108

CELERY_BROKER_USER = os.environ.get('ENGRAM_CELERY_BROKER_USER', 'engram')
CELERY_BROKER_HOST = os.environ.get('ENGRAM_CELERY_BROKER_HOST', 'rabbitmq')
CELERY_BROKER_PORT = os.environ.get('ENGRAM_CELERY_BROKER_PORT', '5672')
CELERY_BROKER_PASSWORD = os.environ.get('ENGRAM_CELERY_BROKER_PASSWORD', 'engram')
CELERY_BROKER_VHOST = os.environ.get('ENGRAM_CELERY_BROKER_VHOST', 'engram')

CELERY_BROKER_CONNECTION_STRING = '{schema}://{user}:{password}@{host}:{port}/{vhost}'.format(
    schema='amqp',
    user=CELERY_BROKER_USER,
    password=CELERY_BROKER_PASSWORD,
    host=CELERY_BROKER_HOST,
    port=CELERY_BROKER_PORT,
    vhost=CELERY_BROKER_VHOST,
)

broker_url = os.environ.get('ENGRAM_CELERY_BROKER_URL', CELERY_BROKER_CONNECTION_STRING)
broker_pool_limit = 10
result_backend = os.environ.get(
    'ENGRAM_CELERY_RESULT_BACKEND', os.environ.get('ENGRAM_REDIS_URL', 'redis://redis:6379/1')
)
result_expires = 3600

QUEUE_REALTIME = 'engram-realtime'
QUEUE_NEAR_REALTIME = 'engram-near-realtime'
QUEUE_BATCH = 'engram-batch'
QUEUE_HIGHMEMORY = 'engram-highmemory'
QUEUE_DOMAIN_EVENTS = 'engram-domain-events'

task_default_queue = QUEUE_NEAR_REALTIME

task_default_exchange = QUEUE_NEAR_REALTIME
task_default_exchange_type = 'topic'
task_default_routing_key = QUEUE_NEAR_REALTIME
task_default_queue_type = 'quorum'

task_serializer = 'json'
task_ignore_result = True

accept_content = ['json']
result_serializer = 'json'
enable_utc = True

task_queues = (
    Queue(
        QUEUE_REALTIME,
        Exchange(QUEUE_REALTIME, type='topic'),
        routing_key=QUEUE_REALTIME,
        queue_arguments={'x-queue-type': 'quorum'},
    ),
    Queue(
        QUEUE_NEAR_REALTIME,
        Exchange(QUEUE_NEAR_REALTIME, type='topic'),
        routing_key=QUEUE_NEAR_REALTIME,
        queue_arguments={'x-queue-type': 'quorum'},
    ),
    Queue(
        QUEUE_BATCH,
        Exchange(QUEUE_BATCH, type='topic'),
        routing_key=QUEUE_BATCH,
        queue_arguments={'x-queue-type': 'quorum'},
    ),
    Queue(
        QUEUE_HIGHMEMORY,
        Exchange(QUEUE_HIGHMEMORY, type='topic'),
        routing_key=QUEUE_HIGHMEMORY,
        queue_arguments={'x-queue-type': 'quorum'},
    ),
    Queue(
        QUEUE_DOMAIN_EVENTS,
        Exchange(QUEUE_DOMAIN_EVENTS, type='topic'),
        routing_key=QUEUE_DOMAIN_EVENTS,
        queue_arguments={'x-queue-type': 'quorum'},
    ),
)

beat_schedule: dict[str, dict] = {}

worker_max_tasks_per_child = int(os.getenv('ENGRAM_WORKER_MAX_TASKS_PER_CHILD', 512))
worker_task_log_format = '[%(asctime)s] [%(levelname)s] %(name)s %(module)s %(process)d | %(message)s'
worker_log_format = worker_task_log_format
worker_soft_shutdown_timeout = 60
worker_detect_quorum_queues = True

broker_connection_retry_on_startup = False
broker_connection_retry = False
broker_native_delayed_delivery_queue_type = 'quorum'
broker_transport_options = {
    'confirm_publish': True,
}

task_always_eager = bool(int(os.getenv('ENGRAM_CELERY_ALWAYS_EAGER', 0)))
task_store_eager_result = task_always_eager

register_type(
    Model,
    'model',
    lambda obj: [obj._meta.label, obj.pk],
    lambda obj: apps.get_model(obj[0]).objects.get(pk=obj[1]),
)
