from __future__ import annotations

from django.apps import apps

from engram.memory import candidate_work_reconciler
from engram.memory.candidate_decision_work import (
    get_candidate_decision_work_builder as get_canonical_builder,
)


def test_memory_app_startup_registers_canonical_candidate_builder() -> None:
    previous_builder = candidate_work_reconciler.get_candidate_decision_work_builder()
    candidate_work_reconciler.set_candidate_decision_work_builder(None)
    try:
        apps.get_app_config('memory').ready()

        assert candidate_work_reconciler.get_candidate_decision_work_builder() is get_canonical_builder()
    finally:
        candidate_work_reconciler.set_candidate_decision_work_builder(previous_builder)
