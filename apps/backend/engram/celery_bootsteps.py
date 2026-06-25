from __future__ import annotations

from typing import Any

from celery import bootsteps

from engram.celeryconfig import HEARTBEAT_FILE


class LivenessProbe(bootsteps.StartStopStep):  # pragma: no cover
    requires = {'celery.worker.components:Timer'}

    def __init__(self, worker: Any, **kwargs: Any) -> None:
        self.requests: list[Any] = []
        self.tref: Any = None

    def start(self, worker: Any) -> None:
        self.tref = worker.timer.call_repeatedly(
            1.0,
            self.update_heartbeat_file,
            (worker,),
            priority=10,
        )

    def stop(self, worker: Any) -> None:
        HEARTBEAT_FILE.unlink(missing_ok=True)

    def update_heartbeat_file(self, worker: Any) -> None:
        HEARTBEAT_FILE.touch()
