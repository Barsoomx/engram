from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

from celery.schedules import crontab
from django.apps import apps
from django.db.models import Model
from kombu import Exchange, Queue
from kombu.utils.json import register_type

from engram.core.redis_sentinel import REDIS_PASS, REDIS_RETRY_KWARGS, REDIS_SENTINELS, REDIS_USE_SENTINEL

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
CELERY_RESULT_BACKEND_DB = int(os.getenv('ENGRAM_CELERY_RESULT_BACKEND_DB', '1'))

if REDIS_USE_SENTINEL and 'ENGRAM_CELERY_RESULT_BACKEND' not in os.environ:
    result_backend = ';'.join(
        f'sentinel://:{REDIS_PASS}@{host}:{port}/{CELERY_RESULT_BACKEND_DB}' for host, port in REDIS_SENTINELS
    )
    result_backend_transport_options = {
        'master_name': 'mymaster',
        'sentinel_kwargs': {
            'password': REDIS_PASS,
            **REDIS_RETRY_KWARGS,
        },
    }
else:
    result_backend = os.environ.get(
        'ENGRAM_CELERY_RESULT_BACKEND',
        os.environ.get('ENGRAM_REDIS_URL', 'redis://redis:6379/1'),
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

task_routes = {
    'engram.memory.process_observation_recorded': {'queue': QUEUE_NEAR_REALTIME},
    'engram.memory.distill_session': {'queue': QUEUE_BATCH},
    'engram.memory.generate_daily_digest': {'queue': QUEUE_BATCH},
    'engram.memory.generate_weekly_digest': {'queue': QUEUE_BATCH},
    'engram.memory.sweep_stale_sessions': {'queue': QUEUE_BATCH},
    'engram.memory.retry_failed_distillations': {'queue': QUEUE_BATCH},
    'engram.memory.decay_memory_confidence': {'queue': QUEUE_BATCH},
    'engram.memory.expire_stale_candidates': {'queue': QUEUE_BATCH},
    'engram.imports.expire_stale_import_jobs': {'queue': QUEUE_BATCH},
}

task_soft_time_limit = 120
task_time_limit = 180

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

beat_schedule: dict[str, dict] = {
    'daily-digest': {
        'task': 'engram.memory.run_scheduled_digests',
        'schedule': crontab(hour=2, minute=0),
        'options': {'queue': QUEUE_BATCH},
    },
    'weekly-digest': {
        'task': 'engram.memory.run_scheduled_weekly_digests',
        'schedule': crontab(day_of_week=1, hour=3, minute=0),
        'options': {'queue': QUEUE_BATCH},
    },
    'stale-session-sweep': {
        'task': 'engram.memory.sweep_stale_sessions',
        'schedule': timedelta(minutes=5),
        'options': {'queue': QUEUE_BATCH},
    },
    'retry-failed-distillations': {
        'task': 'engram.memory.retry_failed_distillations',
        'schedule': timedelta(minutes=30),
        'options': {'queue': QUEUE_BATCH},
    },
    'reembed-missing-embeddings': {
        'task': 'engram.memory.reembed_missing_embeddings',
        'schedule': crontab(minute='*/15'),
        'options': {'queue': QUEUE_BATCH},
    },
    'confidence-decay': {
        'task': 'engram.memory.decay_memory_confidence',
        'schedule': crontab(day_of_week=1, hour=4, minute=0),
        'options': {'queue': QUEUE_BATCH},
    },
    'expire-stale-candidates': {
        'task': 'engram.memory.expire_stale_candidates',
        'schedule': timedelta(minutes=30),
        'options': {'queue': QUEUE_BATCH},
    },
    'expire-stale-import-jobs': {
        'task': 'engram.imports.expire_stale_import_jobs',
        'schedule': timedelta(minutes=30),
        'options': {'queue': QUEUE_BATCH},
    },
}

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
