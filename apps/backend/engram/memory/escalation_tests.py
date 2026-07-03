from __future__ import annotations

from django.test import override_settings

from engram.core.models import MemoryCandidate, VisibilityScope
from engram.memory.escalation import escalation_reason
from settings.settings import _DEFAULT_CURATOR_SENSITIVE_TERMS, csv


def test_escalation_reason_flags_sensitive_term_in_body() -> None:
    candidate = MemoryCandidate(title='Deploy notes', body='Rotate the client secret before release')

    assert escalation_reason(candidate) == 'security_sensitive'


def test_escalation_reason_flags_sensitive_term_case_insensitively() -> None:
    candidate = MemoryCandidate(title='Deploy notes', body='ROTATE THE CLIENT SECRET before release')

    assert escalation_reason(candidate) == 'security_sensitive'


def test_escalation_reason_flags_sensitive_term_in_title() -> None:
    candidate = MemoryCandidate(title='CVE-2026-1234 patched', body='Upgraded the vulnerable dependency')

    assert escalation_reason(candidate) == 'security_sensitive'


def test_escalation_reason_flags_organization_wide_scope() -> None:
    candidate = MemoryCandidate(
        title='Org-wide rollout',
        body='Applies to every team',
        visibility_scope=VisibilityScope.ORGANIZATION,
    )

    assert escalation_reason(candidate) == 'org_wide_scope'


def test_escalation_reason_prefers_org_wide_over_sensitive_terms() -> None:
    candidate = MemoryCandidate(
        title='Org-wide rollout',
        body='Rotate the client secret before release',
        visibility_scope=VisibilityScope.ORGANIZATION,
    )

    assert escalation_reason(candidate) == 'org_wide_scope'


def test_escalation_reason_returns_empty_for_benign_candidate() -> None:
    candidate = MemoryCandidate(
        title='Networking port',
        body='Use port 8443 not 8080',
        visibility_scope=VisibilityScope.PROJECT,
    )

    assert escalation_reason(candidate) == ''


@override_settings(ENGRAM_CURATOR_ESCALATION_ENABLED=False)
def test_escalation_reason_disabled_by_settings_returns_empty_for_sensitive_term() -> None:
    candidate = MemoryCandidate(title='Deploy notes', body='Rotate the client secret before release')

    assert escalation_reason(candidate) == ''


@override_settings(ENGRAM_CURATOR_ESCALATION_ENABLED=False)
def test_escalation_reason_disabled_by_settings_returns_empty_for_org_wide_scope() -> None:
    candidate = MemoryCandidate(
        title='Org-wide rollout',
        body='Applies to every team',
        visibility_scope=VisibilityScope.ORGANIZATION,
    )

    assert escalation_reason(candidate) == ''


@override_settings(ENGRAM_CURATOR_SENSITIVE_TERMS=('rotate secret',))
def test_escalation_reason_reads_custom_sensitive_terms_from_settings() -> None:
    default_term_candidate = MemoryCandidate(title='Deploy notes', body='Rotate the client secret before release')
    custom_term_candidate = MemoryCandidate(title='Deploy notes', body='Time to rotate secret before release')

    assert escalation_reason(default_term_candidate) == ''
    assert escalation_reason(custom_term_candidate) == 'security_sensitive'


def test_curator_sensitive_terms_env_value_present_but_empty_falls_back_to_defaults() -> None:
    parsed_from_empty_env = tuple(term.casefold() for term in csv('', default=_DEFAULT_CURATOR_SENSITIVE_TERMS))

    assert parsed_from_empty_env == _DEFAULT_CURATOR_SENSITIVE_TERMS

    with override_settings(ENGRAM_CURATOR_SENSITIVE_TERMS=parsed_from_empty_env):
        candidate = MemoryCandidate(title='Deploy notes', body='Rotate the client secret before release')

        assert escalation_reason(candidate) == 'security_sensitive'
