from __future__ import annotations

from engram.core.models import LinkType, MemoryCandidate, MemoryLink


def clear_candidate_conflict_links(candidate: MemoryCandidate) -> None:
    MemoryLink.objects.filter(
        organization=candidate.organization,
        link_type=LinkType.CONFLICTS_WITH,
        target=f'candidate:{candidate.id}',
    ).delete()
