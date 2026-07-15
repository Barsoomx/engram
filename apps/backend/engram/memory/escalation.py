from __future__ import annotations

from django.conf import settings

from engram.core.models import MemoryCandidate, VisibilityScope

_ESCALATION_ORG_WIDE = 'org_wide_scope'
_ESCALATION_SENSITIVE = 'security_sensitive'


def escalation_reason(candidate: MemoryCandidate) -> str:
    if candidate.decision_work_contract_version == 1:
        return ''

    if not settings.ENGRAM_CURATOR_ESCALATION_ENABLED:
        return ''

    if candidate.visibility_scope == VisibilityScope.ORGANIZATION:
        return _ESCALATION_ORG_WIDE

    haystack = f'{candidate.title}\n{candidate.body}'.casefold()
    for term in settings.ENGRAM_CURATOR_SENSITIVE_TERMS:
        if term in haystack:
            return _ESCALATION_SENSITIVE

    return ''
