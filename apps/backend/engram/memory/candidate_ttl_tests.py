from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from django.utils import timezone
from pytest_django.fixtures import SettingsWrapper

from engram.core.models import (
    AuditEvent,
    CandidateStatus,
    MemoryCandidate,
    Organization,
    OrganizationSettings,
    Project,
)
from engram.memory.candidate_ttl import ExpireStaleCandidates


def _make_candidate(
    organization: Organization,
    project: Project,
    *,
    status: str = CandidateStatus.PROPOSED,
    confidence: str | None = '0.300',
    created_at: datetime | None = None,
) -> MemoryCandidate:
    counter = MemoryCandidate.objects.count()

    candidate = MemoryCandidate.objects.create(
        organization=organization,
        project=project,
        title=f'Candidate {counter}',
        body=f'Body {counter}',
        status=status,
        content_hash=f'hash-c-{counter}',
        confidence=confidence,
    )

    if created_at is not None:
        MemoryCandidate.objects.filter(id=candidate.id).update(created_at=created_at)
        candidate.refresh_from_db()

    return candidate


@pytest.fixture
def f_org() -> Organization:
    return Organization.objects.create(name='Sweep', slug='sweep')


@pytest.fixture
def f_project(f_org: Organization) -> Project:
    return Project.objects.create(organization=f_org, name='Eng', slug='eng')


@pytest.mark.django_db
def test_expired_below_threshold_candidate_is_rejected_with_audit(
    f_org: Organization,
    f_project: Project,
    settings: SettingsWrapper,
) -> None:
    settings.ENGRAM_CANDIDATE_REVIEW_TTL_DAYS = 14
    settings.ENGRAM_DISTILLATION_AUTO_APPROVE_THRESHOLD = '0.500'

    candidate = _make_candidate(
        f_org,
        f_project,
        confidence='0.300',
        created_at=timezone.now() - timedelta(days=20),
    )

    result = ExpireStaleCandidates().execute()

    candidate.refresh_from_db()

    assert result.rejected == 1
    assert candidate.status == CandidateStatus.REJECTED

    audit = AuditEvent.objects.get(
        organization=f_org,
        event_type='MemoryAutoRejected',
        target_id=str(candidate.id),
    )
    assert audit.metadata['reason'] == 'review_ttl_expired'
    assert audit.metadata['decision'] == 'rejected'
    assert audit.actor_id == 'curator'


@pytest.mark.django_db
def test_fresh_candidate_is_untouched(
    f_org: Organization,
    f_project: Project,
    settings: SettingsWrapper,
) -> None:
    settings.ENGRAM_CANDIDATE_REVIEW_TTL_DAYS = 14

    candidate = _make_candidate(f_org, f_project, confidence='0.300', created_at=timezone.now())

    result = ExpireStaleCandidates().execute()

    candidate.refresh_from_db()

    assert result.rejected == 0
    assert candidate.status == CandidateStatus.PROPOSED


@pytest.mark.django_db
def test_high_confidence_old_candidate_is_untouched(
    f_org: Organization,
    f_project: Project,
    settings: SettingsWrapper,
) -> None:
    settings.ENGRAM_CANDIDATE_REVIEW_TTL_DAYS = 14
    settings.ENGRAM_DISTILLATION_AUTO_APPROVE_THRESHOLD = '0.500'

    candidate = _make_candidate(
        f_org,
        f_project,
        confidence='0.900',
        created_at=timezone.now() - timedelta(days=30),
    )

    result = ExpireStaleCandidates().execute()

    candidate.refresh_from_db()

    assert result.rejected == 0
    assert candidate.status == CandidateStatus.PROPOSED


@pytest.mark.django_db
def test_per_org_threshold_from_organization_settings_is_respected(
    f_org: Organization,
    f_project: Project,
    settings: SettingsWrapper,
) -> None:
    settings.ENGRAM_CANDIDATE_REVIEW_TTL_DAYS = 14
    settings.ENGRAM_DISTILLATION_AUTO_APPROVE_THRESHOLD = '0.900'
    OrganizationSettings.objects.update_or_create(
        organization=f_org,
        defaults={'distillation_auto_approve_threshold': '0.200'},
    )

    candidate = _make_candidate(
        f_org,
        f_project,
        confidence='0.300',
        created_at=timezone.now() - timedelta(days=20),
    )

    result = ExpireStaleCandidates().execute()

    candidate.refresh_from_db()

    assert result.rejected == 0
    assert candidate.status == CandidateStatus.PROPOSED


@pytest.mark.django_db
def test_batch_cap_rejects_only_oldest(
    f_org: Organization,
    f_project: Project,
    settings: SettingsWrapper,
) -> None:
    settings.ENGRAM_CANDIDATE_REVIEW_TTL_DAYS = 14
    settings.ENGRAM_CANDIDATE_TTL_BATCH = 2
    settings.ENGRAM_DISTILLATION_AUTO_APPROVE_THRESHOLD = '0.500'

    base = timezone.now() - timedelta(days=30)
    candidates = [
        _make_candidate(
            f_org,
            f_project,
            confidence='0.300',
            created_at=base + timedelta(hours=index),
        )
        for index in range(5)
    ]

    result = ExpireStaleCandidates().execute()

    assert result.rejected == 2

    for candidate in candidates:
        candidate.refresh_from_db()

    statuses = [candidate.status for candidate in candidates]
    assert statuses[0] == CandidateStatus.REJECTED
    assert statuses[1] == CandidateStatus.REJECTED
    assert statuses[2] == CandidateStatus.PROPOSED
    assert statuses[3] == CandidateStatus.PROPOSED
    assert statuses[4] == CandidateStatus.PROPOSED


@pytest.mark.django_db
def test_second_run_is_idempotent(
    f_org: Organization,
    f_project: Project,
    settings: SettingsWrapper,
) -> None:
    settings.ENGRAM_CANDIDATE_REVIEW_TTL_DAYS = 14
    settings.ENGRAM_CANDIDATE_TTL_BATCH = 500
    settings.ENGRAM_DISTILLATION_AUTO_APPROVE_THRESHOLD = '0.500'

    _make_candidate(
        f_org,
        f_project,
        confidence='0.300',
        created_at=timezone.now() - timedelta(days=20),
    )

    first = ExpireStaleCandidates().execute()
    second = ExpireStaleCandidates().execute()

    assert first.rejected == 1
    assert second.rejected == 0
    assert AuditEvent.objects.filter(event_type='MemoryAutoRejected').count() == 1
