from __future__ import annotations

import threading
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from django.db import close_old_connections, connection, transaction
from django.utils import timezone

from engram.access.services import EffectiveScope
from engram.core.models import (
    AuditEvent,
    Memory,
    MemoryStatus,
    Organization,
    OrganizationSettings,
    Project,
)
from engram.memory.confidence_decay import DecayMemoryConfidence
from engram.memory.services import MemoryFeedbackInput, RecordMemoryFeedback
from engram.memory.transitions_test_support import provenanced_candidate, transition_request, transitions_module

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
    last_confirmed_at: datetime | None = None,
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

    Memory.objects.filter(id=memory.id).update(
        updated_at=timezone.now() - timedelta(days=age_days),
        last_confirmed_at=last_confirmed_at,
    )
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


@pytest.mark.django_db
def test_recently_confirmed_memory_is_not_decayed(f_org: Organization, f_project: Project) -> None:
    memory = _make_memory(f_org, f_project, confidence='0.900', last_confirmed_at=timezone.now())

    DecayMemoryConfidence().execute()

    memory.refresh_from_db()

    assert memory.confidence == Decimal('0.900')


@pytest.mark.django_db
def test_unconfirmed_aged_memory_still_decays(f_org: Organization, f_project: Project) -> None:
    memory = _make_memory(f_org, f_project, confidence='0.900', last_confirmed_at=None)

    DecayMemoryConfidence().execute()

    memory.refresh_from_db()

    assert memory.confidence == Decimal('0.850')


@pytest.mark.django_db
def test_decay_rechecks_anchor_under_lock(
    f_org: Organization,
    f_project: Project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = _make_memory(f_org, f_project, confidence='0.900', last_confirmed_at=None)
    real_atomic = transaction.atomic
    advanced = {'done': False}

    def patched_atomic(*args: object, **kwargs: object) -> object:
        if not advanced['done']:
            advanced['done'] = True
            Memory.objects.filter(id=memory.id).update(last_confirmed_at=timezone.now())

        return real_atomic(*args, **kwargs)

    monkeypatch.setattr(transaction, 'atomic', patched_atomic)

    DecayMemoryConfidence().execute()

    memory.refresh_from_db()

    assert memory.confidence == Decimal('0.900')


def _aged_promoted_memory(suffix: str) -> Memory:
    candidate, _source, _scope = provenanced_candidate(suffix)
    result = transitions_module().PromoteMemoryCandidate().execute(transition_request(candidate))
    Memory.objects.filter(id=result.memory.id).update(updated_at=timezone.now() - timedelta(days=_AGED_DAYS))
    result.memory.refresh_from_db()

    return result.memory


def _confirm_input(memory: Memory, request_id: str) -> MemoryFeedbackInput:
    scope = EffectiveScope(
        organization_id=memory.organization_id,
        identity_id=memory.organization_id,
        api_key_id=memory.organization_id,
        project_ids=(memory.project_id,),
        team_ids=() if memory.team_id is None else (memory.team_id,),
        capabilities=('memories:review',),
        actor_type='api_key',
        actor_id='confirm-race-actor',
        project_bound=False,
    )

    return MemoryFeedbackInput(
        scope=scope,
        memory_id=memory.id,
        project_id=memory.project_id,
        team_id=memory.team_id,
        action='confirmed',
        reason='verified still accurate',
        request_id=request_id,
        correlation_id='',
    )


def _run_in_thread(target: object) -> tuple[threading.Thread, list[BaseException]]:
    errors: list[BaseException] = []

    def worker() -> None:
        close_old_connections()
        try:
            target()
        except BaseException as error:  # pragma: no cover - surfaced by assertions
            errors.append(error)
        finally:
            close_old_connections()

    thread = threading.Thread(target=worker)

    return thread, errors


def _assert_confirm_holds_lock_then_decay_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    memory = _aged_promoted_memory('confirm-decay-a')
    confirm_locked = threading.Event()
    release_confirm = threading.Event()
    decay_result: list[object] = []
    original_lock = RecordMemoryFeedback._lock_memory

    def paused_lock(self: RecordMemoryFeedback, data: MemoryFeedbackInput, scope: EffectiveScope) -> Memory:
        locked = original_lock(self, data, scope)
        confirm_locked.set()
        assert release_confirm.wait(timeout=10)

        return locked

    monkeypatch.setattr(RecordMemoryFeedback, '_lock_memory', paused_lock)

    confirm_thread, confirm_error = _run_in_thread(
        lambda: RecordMemoryFeedback().execute(_confirm_input(memory, 'confirm-race-a')),
    )
    decay_done = threading.Event()

    def run_decay() -> None:
        decay_result.append(DecayMemoryConfidence().execute())
        decay_done.set()

    decay_thread, decay_error = _run_in_thread(run_decay)

    confirm_thread.start()
    assert confirm_locked.wait(timeout=10)
    decay_thread.start()
    try:
        assert not decay_done.wait(timeout=1)
    finally:
        release_confirm.set()
        confirm_thread.join(timeout=10)
        decay_thread.join(timeout=10)

    assert not confirm_error, confirm_error
    assert not decay_error, decay_error
    memory.refresh_from_db()
    assert memory.confidence == Decimal('0.900')
    assert memory.last_confirmed_at is not None
    assert decay_result[0].memories == 0
    assert AuditEvent.objects.filter(event_type='MemoryConfirmed', target_id=str(memory.id)).count() == 1
    assert not AuditEvent.objects.filter(event_type='MemoryConfidenceDecayed').exists()


def _assert_decay_holds_lock_then_confirm_records(monkeypatch: pytest.MonkeyPatch) -> None:
    memory = _aged_promoted_memory('confirm-decay-b')
    decay_locked = threading.Event()
    release_decay = threading.Event()
    confirm_done = threading.Event()
    original_save = Memory.save

    def paused_save(self: Memory, *args: object, **kwargs: object) -> None:
        update_fields = kwargs.get('update_fields')
        if update_fields is not None and 'confidence' in update_fields and self.id == memory.id:
            decay_locked.set()
            assert release_decay.wait(timeout=10)

        return original_save(self, *args, **kwargs)

    monkeypatch.setattr(Memory, 'save', paused_save)

    decay_thread, decay_error = _run_in_thread(lambda: DecayMemoryConfidence().execute())

    def run_confirm() -> None:
        RecordMemoryFeedback().execute(_confirm_input(memory, 'confirm-race-b'))
        confirm_done.set()

    confirm_thread, confirm_error = _run_in_thread(run_confirm)

    decay_thread.start()
    assert decay_locked.wait(timeout=10)
    confirm_thread.start()
    try:
        assert not confirm_done.wait(timeout=1)
    finally:
        release_decay.set()
        decay_thread.join(timeout=10)
        confirm_thread.join(timeout=10)

    assert not decay_error, decay_error
    assert not confirm_error, confirm_error
    memory.refresh_from_db()
    assert memory.confidence == Decimal('0.850')
    assert memory.last_confirmed_at is not None
    assert AuditEvent.objects.filter(event_type='MemoryConfidenceDecayed').count() == 1
    assert AuditEvent.objects.filter(event_type='MemoryConfirmed', target_id=str(memory.id)).count() == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(connection.vendor != 'postgresql', reason='requires PostgreSQL row-lock semantics')
def test_confirm_and_decay_serialize_on_row_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    _assert_confirm_holds_lock_then_decay_skips(monkeypatch)
    monkeypatch.undo()
    _assert_decay_holds_lock_then_confirm_records(monkeypatch)
