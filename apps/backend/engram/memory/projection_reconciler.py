from __future__ import annotations

import uuid
from datetime import datetime

from engram.core.models import Memory
from engram.memory.aware_time import require_aware
from engram.memory.invariant_queries import projection_inconsistency_memory_ids
from engram.memory.session_work_reconciler import SessionWorkFinding

CURRENT_PROJECTION_MISSING_OR_INCONSISTENT = 'current_projection_missing_or_inconsistent'

_ENTITY_TYPE = 'memory'
_PROPOSED_ACTION = 'defer_to_cp4'


def _finding(
    *,
    organization_id: uuid.UUID,
    project_id: uuid.UUID,
    memory_id: uuid.UUID,
    observed_at: datetime,
) -> SessionWorkFinding:
    return SessionWorkFinding(
        code=CURRENT_PROJECTION_MISSING_OR_INCONSISTENT,
        organization_id=organization_id,
        project_id=project_id,
        entity_type=_ENTITY_TYPE,
        entity_id=str(memory_id),
        work_id=None,
        workflow_run_id=None,
        observed_at=observed_at,
        proposed_action=_PROPOSED_ACTION,
        auto_repair_eligible=False,
    )


def inspect_projection(
    *,
    organization_id: uuid.UUID,
    project_id: uuid.UUID,
    as_of: datetime,
) -> tuple[SessionWorkFinding, ...]:
    require_aware(as_of)

    memory_ids = projection_inconsistency_memory_ids(
        organization_id=organization_id,
        project_id=project_id,
    )
    updated_at_by_id = dict(
        Memory.objects.filter(
            organization_id=organization_id,
            project_id=project_id,
            id__in=memory_ids,
        ).values_list('id', 'updated_at')
    )

    return tuple(
        _finding(
            organization_id=organization_id,
            project_id=project_id,
            memory_id=memory_id,
            observed_at=min(updated_at_by_id[memory_id], as_of),
        )
        for memory_id in memory_ids
    )
