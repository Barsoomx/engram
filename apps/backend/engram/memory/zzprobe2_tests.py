from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from engram.memory import distillation
from engram.memory import tasks as memory_tasks
from engram.model_policy.services import max_chat_http_timeout


class _Claim:
    def __init__(self, lease_expires_at: object) -> None:
        self.lease_expires_at = lease_expires_at


class _Task:
    def __init__(self, soft_time_limit: int) -> None:
        self.soft_time_limit = soft_time_limit
        self.request = None


def test_probe_provider_call_reserve_inside_a_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    real_task = memory_tasks.distill_session_work_v1
    monkeypatch.setattr(distillation, 'current_task', _Task(real_task.soft_time_limit))
    lease = _Claim(timezone.now() + timedelta(seconds=memory_tasks._SESSION_LEASE.total_seconds()))

    now = timezone.now()
    deadline = distillation.attempt_deadline(lease, started_at=now)
    reserve = distillation._provider_call_reserve()
    can_start = distillation._can_start_provider_call(now=now, started=0, budget=8, deadline=deadline)

    print(
        f'PROBE chat_socket={max_chat_http_timeout()} '
        f'task_soft={real_task.soft_time_limit} task_hard={real_task.time_limit} '
        f'lease={memory_tasks._SESSION_LEASE.total_seconds()} '
        f'reserve={reserve.total_seconds()} '
        f'deadline_in={(deadline - now).total_seconds()} '
        f'can_start_first_provider_call={can_start}'
    )
