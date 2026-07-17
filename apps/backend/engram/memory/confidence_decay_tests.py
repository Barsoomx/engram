from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from engram.core.models import (
    AuditEvent,
    Memory,
    MemoryStatus,
    Organization,
    OrganizationSettings,
    Project,
)
from engram.memory.confidence_decay import DecayMemoryConfidence

_AGED_DAYS = 40
_YOUNG_DAYS = 5


@pytest.fixture
def f_org() -> Organization:
    return Organization.objects.create(name='Decay Org', slug='decay-org')


@pytest.fixture
def f_project(f_org: Organization) -> Project:
    return Project.objects.create(organization=f_org, name='Backend', slug='backend')


def _make_memory(
    organization: Organization,
    project: Project,
    *,
    confidence: str | None = '0.900',
    status: str = MemoryStatus.APPROVED,
    stale: bool = False,
    refuted: bool = False,
    kind: str = '',
    age_days: int = _AGED_DAYS,
) -> Memory:
    memory = Memory.objects.create(
        organization=organization,
        project=project,
        title=f'Memory {Memory.objects.count()}',
        body='body',
        status=status,
        confidence=Decimal(confidence) if confidence is not None else None,
        stale=stale,
        refuted=refuted,
        metadata={'kind': kind} if kind else {},
    )

    Memory.objects.filter(id=memory.id).update(updated_at=timezone.now() - timedelta(days=age_days))
    memory.refresh_from_db()

    return memory


@pytest.mark.django_db
def test_decays_aged_approved_memory_confidence_by_step(f_org: Organization, f_project: Project) -> None:
    memory = _make_memory(f_org, f_project, confidence='0.900')

    DecayMemoryConfidence().execute()

    memory.refresh_from_db()

    assert memory.confidence == Decimal('0.850')


@pytest.mark.django_db
def test_clamps_to_floor_when_step_would_overshoot(f_org: Organization, f_project: Project) -> None:
    memory = _make_memory(f_org, f_project, confidence='0.210')

    DecayMemoryConfidence().execute()

    memory.refresh_from_db()

    assert memory.confidence == Decimal('0.200')


@pytest.mark.django_db
def test_confidence_exactly_at_floor_is_not_decayed_further(f_org: Organization, f_project: Project) -> None:
    memory = _make_memory(f_org, f_project, confidence='0.200')

    DecayMemoryConfidence().execute()

    memory.refresh_from_db()

    assert memory.confidence == Decimal('0.200')


@pytest.mark.django_db
def test_skips_memory_younger_than_min_age(f_org: Organization, f_project: Project) -> None:
    memory = _make_memory(f_org, f_project, confidence='0.900', age_days=_YOUNG_DAYS)

    DecayMemoryConfidence().execute()

    memory.refresh_from_db()

    assert memory.confidence == Decimal('0.900')


@pytest.mark.django_db
def test_skips_stale_memory(f_org: Organization, f_project: Project) -> None:
    memory = _make_memory(f_org, f_project, confidence='0.900', stale=True)

    DecayMemoryConfidence().execute()

    memory.refresh_from_db()

    assert memory.confidence == Decimal('0.900')


@pytest.mark.django_db
def test_skips_refuted_memory(f_org: Organization, f_project: Project) -> None:
    memory = _make_memory(f_org, f_project, confidence='0.900', refuted=True)

    DecayMemoryConfidence().execute()

    memory.refresh_from_db()

    assert memory.confidence == Decimal('0.900')


@pytest.mark.django_db
def test_skips_non_approved_memory(f_org: Organization, f_project: Project) -> None:
    memory = _make_memory(f_org, f_project, confidence='0.900', status=MemoryStatus.CONFLICT)

    DecayMemoryConfidence().execute()

    memory.refresh_from_db()

    assert memory.confidence == Decimal('0.900')


@pytest.mark.django_db
def test_skips_digest_memory(f_org: Organization, f_project: Project) -> None:
    memory = _make_memory(f_org, f_project, confidence='0.900', kind='digest')

    DecayMemoryConfidence().execute()

    memory.refresh_from_db()

    assert memory.confidence == Decimal('0.900')


@pytest.mark.django_db
def test_skips_memory_with_null_confidence(f_org: Organization, f_project: Project) -> None:
    memory = _make_memory(f_org, f_project, confidence=None)

    DecayMemoryConfidence().execute()

    memory.refresh_from_db()

    assert memory.confidence is None


@pytest.mark.django_db
def test_disabled_org_is_untouched(f_org: Organization, f_project: Project) -> None:
    OrganizationSettings.objects.create(organization=f_org, confidence_decay_enabled=False)

    memory = _make_memory(f_org, f_project, confidence='0.900')

    DecayMemoryConfidence().execute()

    memory.refresh_from_db()

    assert memory.confidence == Decimal('0.900')


@pytest.mark.django_db
def test_org_with_no_settings_row_is_enabled_by_default(f_org: Organization, f_project: Project) -> None:
    memory = _make_memory(f_org, f_project, confidence='0.900')

    DecayMemoryConfidence().execute()

    memory.refresh_from_db()

    assert memory.confidence == Decimal('0.850')


@pytest.mark.django_db
def test_writes_one_audit_event_per_project_with_metadata(f_org: Organization, f_project: Project) -> None:
    memory = _make_memory(f_org, f_project, confidence='0.900')

    DecayMemoryConfidence().execute()

    events = list(AuditEvent.objects.filter(organization=f_org, event_type='MemoryConfidenceDecayed'))

    assert len(events) == 1

    event = events[0]

    assert event.project_id == f_project.id
    assert event.actor_type == 'system'
    assert event.actor_id == 'curator'
    assert event.capability == 'memories:review'
    assert event.metadata['count'] == 1
    assert event.metadata['memory_ids'] == [str(memory.id)]
    assert event.metadata['step'] == '0.050'
    assert event.metadata['floor'] == '0.200'


@pytest.mark.django_db
def test_no_audit_event_when_nothing_decayed(f_org: Organization, f_project: Project) -> None:
    _make_memory(f_org, f_project, confidence='0.900', age_days=_YOUNG_DAYS)

    DecayMemoryConfidence().execute()

    assert not AuditEvent.objects.filter(organization=f_org, event_type='MemoryConfidenceDecayed').exists()


@pytest.mark.django_db
def test_running_twice_decays_exactly_once(f_org: Organization, f_project: Project) -> None:
    memory = _make_memory(f_org, f_project, confidence='0.900')

    DecayMemoryConfidence().execute()
    DecayMemoryConfidence().execute()

    memory.refresh_from_db()

    assert memory.confidence == Decimal('0.850')


@pytest.mark.django_db
def test_execute_returns_summary_counts(f_org: Organization, f_project: Project) -> None:
    _make_memory(f_org, f_project, confidence='0.900')

    result = DecayMemoryConfidence().execute()

    assert result.organizations == 1
    assert result.projects == 1
    assert result.memories == 1
