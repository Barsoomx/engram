from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from django.db.models import Exists, OuterRef, QuerySet

from engram.core.models import MemoryConflict


def unresolved_conflict_exists(outer_memory_field: str = 'memory_id') -> Exists:
    return Exists(
        MemoryConflict.objects.filter(
            memory_id=OuterRef(outer_memory_field),
            resolved_transition__isnull=True,
        ),
    )


def exclude_unresolved_conflicts(queryset: QuerySet, *, outer_memory_field: str = 'memory_id') -> QuerySet:
    return queryset.filter(~unresolved_conflict_exists(outer_memory_field))


def has_unresolved_conflict(memory_id: UUID) -> bool:
    return MemoryConflict.objects.filter(memory_id=memory_id, resolved_transition__isnull=True).exists()


def conflicted_memory_ids(memory_ids: Iterable[UUID]) -> set[UUID]:
    ids = list(memory_ids)
    if not ids:
        return set()

    return set(
        MemoryConflict.objects.filter(
            memory_id__in=ids,
            resolved_transition__isnull=True,
        ).values_list('memory_id', flat=True),
    )
