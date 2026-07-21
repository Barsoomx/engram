from __future__ import annotations

from django.db.models import Exists, OuterRef

from engram.core.models import MemoryConflict


def open_memory_conflict_exists(memory_ref: str = 'memory_id') -> Exists:
    return Exists(
        MemoryConflict.objects.filter(
            memory_id=OuterRef(memory_ref),
            resolved_transition__isnull=True,
        ),
    )
