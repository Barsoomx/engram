from __future__ import annotations

import uuid

from engram.core.models import LinkType, MemoryCandidate, MemoryLink

CONFLICT_CANDIDATE_TARGET_PREFIX = 'candidate:'


def conflict_candidate_target(candidate_id: uuid.UUID) -> str:
    return f'{CONFLICT_CANDIDATE_TARGET_PREFIX}{candidate_id}'


def clear_candidate_conflict_links(candidate: MemoryCandidate) -> None:
    # Canonical CP4 conflict links are protected by their relational owners.
    # Only pre-cutover string links, which have no durable owner, are cleanup-safe.
    MemoryLink.objects.filter(
        organization=candidate.organization,
        project=candidate.project,
        link_type=LinkType.CONFLICTS_WITH,
        target=conflict_candidate_target(candidate.id),
        memory_conflict__isnull=True,
        memory_transition__isnull=True,
    ).delete()
