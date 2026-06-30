from __future__ import annotations

from engram import celeryconfig
from engram.memory.tasks import (
    distill_session,
    generate_daily_digest,
    generate_weekly_digest,
    process_observation_recorded,
)


def test_task_routes_send_ingest_tasks_to_near_realtime_queue() -> None:
    assert celeryconfig.task_routes['engram.memory.process_observation_recorded']['queue'] == (
        celeryconfig.QUEUE_NEAR_REALTIME
    )
    assert celeryconfig.task_routes['engram.memory.distill_session']['queue'] == celeryconfig.QUEUE_NEAR_REALTIME


def test_task_routes_send_digest_tasks_to_batch_queue() -> None:
    assert celeryconfig.task_routes['engram.memory.generate_daily_digest']['queue'] == celeryconfig.QUEUE_BATCH
    assert celeryconfig.task_routes['engram.memory.generate_weekly_digest']['queue'] == celeryconfig.QUEUE_BATCH


def test_celeryconfig_sets_global_time_limits() -> None:
    assert celeryconfig.task_soft_time_limit == 120
    assert celeryconfig.task_time_limit == 180


def test_ingest_and_digest_tasks_ack_late_and_reject_on_worker_lost() -> None:
    for task in (process_observation_recorded, distill_session, generate_daily_digest, generate_weekly_digest):
        assert task.acks_late is True
        assert task.reject_on_worker_lost is True
