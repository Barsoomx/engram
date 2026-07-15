from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from django.utils import timezone
from django_celery_outbox.models import CeleryOutbox
from pytest_django.fixtures import SettingsWrapper

from engram.core.models import (
    AuditEvent,
    CandidateStatus,
    LinkType,
    MemoryCandidate,
    MemoryConflict,
    MemoryLink,
    Organization,
    OrganizationSettings,
    Project,
    WorkflowRun,
    WorkflowWork,
)
from engram.memory import candidate_work_reconciler
from engram.memory.candidate_decision_work import evidence_manifest
from engram.memory.candidate_decision_work_tests import (
    _candidate as _decision_candidate,
)
from engram.memory.candidate_decision_work_tests import (
    _mark_cp3_candidate,
)
from engram.memory.candidate_decision_work_tests import (
    _scope as _decision_scope,
)
from engram.memory.candidate_ttl import ExpireStaleCandidates
from engram.memory.candidate_work_reconciler import ReconcileCandidateDecisionWork
from engram.memory.curation import CurateMemoryCandidate, CurateMemoryCandidateInput
from engram.memory.curation_test_support import (
    JudgeGatewayStub,
    create_curation_policy,
    patch_atomic_near_duplicate,
    patch_judge_gateway,
    seed_atomic_existing_and_duplicate,
    set_curator_settings,
)
from engram.memory.transitions import (
    CandidateFence,
    ResolveMemoryConflict,
    ResolveMemoryConflictInput,
    TransitionRequest,
    TransitionScope,
    build_memory_fence,
)


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
def test_candidate_ttl_never_rejects_or_audits_old_low_confidence_candidate(
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

    assert result.scanned == 0
    assert result.rejected == 0
    assert result.rejected == 0
    assert candidate.status == CandidateStatus.PROPOSED
    assert not AuditEvent.objects.filter(target_id=str(candidate.id), event_type='MemoryAutoRejected').exists()


@pytest.mark.django_db
def test_candidate_ttl_never_rejects_old_low_confidence_candidate(
    f_org: Organization,
    f_project: Project,
) -> None:
    candidate = _make_candidate(
        f_org,
        f_project,
        confidence='0.001',
        created_at=timezone.now() - timedelta(days=365),
    )

    result = ExpireStaleCandidates().execute()

    candidate.refresh_from_db()
    assert candidate.status == CandidateStatus.PROPOSED
    assert result.rejected == 0


@pytest.mark.django_db
def test_reconciliation_creates_and_queues_missing_v1_work_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = _decision_scope('ttl-reconcile-once')
    candidate = _decision_candidate(scope, 'candidate')
    _mark_cp3_candidate(scope, candidate)
    sent: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        'engram.memory.work_dispatch.app.send_task',
        lambda _name, *, args, **_kwargs: sent.append(tuple(args)),
    )
    as_of = timezone.now()

    first = ReconcileCandidateDecisionWork().execute(as_of=as_of)
    second = ReconcileCandidateDecisionWork().execute(as_of=as_of + timedelta(minutes=31))

    assert first.scanned == 1
    assert first.queued == 1
    run_count = WorkflowRun.objects.filter(work__subject_id=candidate.id).count()
    outbox_count = CeleryOutbox.objects.count()
    first_run = WorkflowRun.objects.get(work__subject_id=candidate.id)
    assert second.queued == 0
    assert len(sent) == 1
    assert WorkflowRun.objects.filter(work__subject_id=candidate.id).count() == run_count
    assert CeleryOutbox.objects.count() == outbox_count
    first_run.refresh_from_db()
    assert first_run.dispatched_at == as_of


@pytest.mark.django_db
def test_legacy_expire_task_delegates_without_semantic_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    result = type('Result', (), {'scanned': 3, 'queued': 2})()
    calls: list[object] = []

    def execute(_self: object) -> object:
        calls.append(_self)
        return result

    monkeypatch.setattr('engram.memory.candidate_ttl.ReconcileCandidateDecisionWork.execute', execute)
    returned = ExpireStaleCandidates().execute()

    assert len(calls) == 1
    assert returned.scanned == 3
    assert returned.rejected == 0


@pytest.mark.django_db
def test_reconciliation_locked_recheck_skips_conflict_created_after_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization, team, project, existing, candidate = seed_atomic_existing_and_duplicate('ttl-race')
    create_curation_policy(organization, team, project)
    set_curator_settings(organization, threshold='1.050', llm_judge_enabled=True)
    patch_atomic_near_duplicate(monkeypatch, existing, score=1.000)
    patch_judge_gateway(monkeypatch, JudgeGatewayStub('{"decision": "contradicts", "reason": "opposite claim"}'))
    selected = candidate_work_reconciler._cp3_repair_candidates(organization.id, project.id)
    assert [row.id for row in selected] == [candidate.id]

    before: dict[str, object] = {}
    original_repair = candidate_work_reconciler._repair_candidate

    def create_conflict_then_recheck(*, candidate_id: uuid.UUID, as_of: datetime) -> bool:
        CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=candidate_id))
        before['candidate'] = list(MemoryCandidate.objects.filter(id=candidate_id).values().order_by('id'))
        before['conflicts'] = list(MemoryConflict.objects.filter(candidate_id=candidate_id).values().order_by('id'))
        before['links'] = list(MemoryLink.objects.filter(organization=organization).values().order_by('id'))
        before['audits'] = list(AuditEvent.objects.filter(organization=organization).values().order_by('id'))
        before['works'] = list(WorkflowWork.objects.filter(subject_id=candidate_id).values().order_by('id'))
        before['outbox'] = list(CeleryOutbox.objects.values().order_by('id'))
        return original_repair(candidate_id=candidate_id, as_of=as_of)

    monkeypatch.setattr(candidate_work_reconciler, '_repair_candidate', create_conflict_then_recheck)
    sent: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        'engram.memory.work_dispatch.app.send_task',
        lambda _name, *, args, **_kwargs: sent.append(tuple(args)),
    )

    result = candidate_work_reconciler.ReconcileCandidateDecisionWork().execute(as_of=timezone.now())

    assert result.queued == 0
    assert not WorkflowRun.objects.filter(work__subject_id=candidate.id).exists()
    assert not sent
    assert list(WorkflowWork.objects.filter(subject_id=candidate.id).values().order_by('id')) == before['works']
    assert list(CeleryOutbox.objects.values().order_by('id')) == before['outbox']
    assert list(MemoryCandidate.objects.filter(id=candidate.id).values().order_by('id')) == before['candidate']
    assert list(MemoryConflict.objects.filter(candidate_id=candidate.id).values().order_by('id')) == before['conflicts']
    assert list(MemoryLink.objects.filter(organization=organization).values().order_by('id')) == before['links']
    assert list(AuditEvent.objects.filter(organization=organization).values().order_by('id')) == before['audits']


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
def test_old_candidates_are_not_batch_rejected(
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

    assert result.rejected == 0
    assert result.rejected == 0

    for candidate in candidates:
        candidate.refresh_from_db()

    assert [candidate.status for candidate in candidates] == [CandidateStatus.PROPOSED] * 5


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

    assert first.rejected == 0
    assert second.rejected == 0
    assert AuditEvent.objects.filter(event_type='MemoryAutoRejected').count() == 0


@pytest.mark.django_db
def test_unresolved_conflict_is_excluded_from_ttl_even_when_old_and_low_confidence(
    monkeypatch: pytest.MonkeyPatch,
    settings: SettingsWrapper,
) -> None:
    settings.ENGRAM_CANDIDATE_REVIEW_TTL_DAYS = 14
    settings.ENGRAM_CANDIDATE_TTL_BATCH = 500
    settings.ENGRAM_DISTILLATION_AUTO_APPROVE_THRESHOLD = '0.500'
    organization, team, project, existing, candidate = seed_atomic_existing_and_duplicate('ttl-conflict')
    create_curation_policy(organization, team, project)
    set_curator_settings(organization, threshold='1.050', llm_judge_enabled=True)
    patch_atomic_near_duplicate(monkeypatch, existing, score=1.000)
    patch_judge_gateway(monkeypatch, JudgeGatewayStub('{"decision": "contradicts", "reason": "opposite claim"}'))
    opened = CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=candidate.id))
    MemoryCandidate.objects.filter(id=candidate.id).update(
        created_at=timezone.now() - timedelta(days=30),
        confidence='0.100',
    )

    result = ExpireStaleCandidates().execute()

    candidate.refresh_from_db()
    conflict = MemoryConflict.objects.get(candidate=candidate, memory=existing)
    assert opened.decision == 'held_conflict'
    assert result.scanned == 0
    assert result.rejected == 0
    assert candidate.status == CandidateStatus.PROPOSED
    assert conflict.resolved_transition_id is None
    assert conflict.resolution == ''
    assert MemoryLink.objects.filter(id=conflict.semantic_link_id, link_type=LinkType.CONFLICTS_WITH).exists()


@pytest.mark.django_db
def test_ttl_locked_recheck_skips_candidate_with_conflict(
    monkeypatch: pytest.MonkeyPatch,
    settings: SettingsWrapper,
) -> None:
    settings.ENGRAM_CANDIDATE_REVIEW_TTL_DAYS = 14
    organization, team, project, existing, candidate = seed_atomic_existing_and_duplicate('ttl-lock-conflict')
    create_curation_policy(organization, team, project)
    set_curator_settings(organization, threshold='1.050', llm_judge_enabled=True)
    patch_atomic_near_duplicate(monkeypatch, existing, score=1.000)
    patch_judge_gateway(monkeypatch, JudgeGatewayStub('{"decision": "contradicts", "reason": "opposite claim"}'))
    CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=candidate.id))

    result = ExpireStaleCandidates().execute()

    candidate.refresh_from_db()
    conflict = MemoryConflict.objects.get(candidate=candidate, memory=existing)
    assert result.rejected == 0
    assert candidate.status == CandidateStatus.PROPOSED
    assert conflict.resolved_transition_id is None
    assert MemoryLink.objects.filter(id=conflict.semantic_link_id).exists()


@pytest.mark.django_db
def test_resolved_conflict_allows_later_ttl_noop_without_erasing_history(
    monkeypatch: pytest.MonkeyPatch,
    settings: SettingsWrapper,
) -> None:
    settings.ENGRAM_CANDIDATE_REVIEW_TTL_DAYS = 14
    organization, team, project, existing, candidate = seed_atomic_existing_and_duplicate('ttl-resolved-conflict')
    create_curation_policy(organization, team, project)
    set_curator_settings(organization, threshold='1.050', llm_judge_enabled=True)
    patch_atomic_near_duplicate(monkeypatch, existing, score=1.000)
    patch_judge_gateway(monkeypatch, JudgeGatewayStub('{"decision": "contradicts", "reason": "opposite claim"}'))
    CurateMemoryCandidate().execute(CurateMemoryCandidateInput(candidate_id=candidate.id))
    conflict = MemoryConflict.objects.get(candidate=candidate, memory=existing)
    _entries, manifest_hash = evidence_manifest(candidate)
    request = TransitionRequest(
        scope=TransitionScope(
            organization_id=organization.id,
            project_id=project.id,
            team_id=team.id,
        ),
        idempotency_key=f'candidate:{candidate.id}:conflict-resolve:v1',
        actor_type='system',
        actor_id='ttl-tests',
        capability='memories:admin',
        request_id=str(uuid.uuid4()),
        correlation_id=str(uuid.uuid4()),
        reason='resolved by test',
        origin='candidate-ttl-tests',
    )
    fence = CandidateFence(
        candidate_id=candidate.id,
        candidate_content_hash=candidate.content_hash,
        evidence_manifest_hash=manifest_hash,
    )
    resolved = ResolveMemoryConflict().execute(
        ResolveMemoryConflictInput(
            request=request,
            candidate_fence=fence,
            conflict_ids=(conflict.id,),
            conflict_memory_fences=(build_memory_fence(existing),),
            resolution='reject_candidate',
        ),
    )
    conflict.refresh_from_db()
    candidate.refresh_from_db()
    MemoryCandidate.objects.filter(id=candidate.id).update(created_at=timezone.now() - timedelta(days=30))

    before_link_count = MemoryLink.objects.filter(id=conflict.semantic_link_id).count()
    result = ExpireStaleCandidates().execute()

    assert resolved.transition.id == conflict.resolved_transition_id
    assert conflict.resolution == 'reject_candidate'
    assert candidate.status == CandidateStatus.REJECTED
    assert result.rejected == 0
    assert MemoryLink.objects.filter(id=conflict.semantic_link_id).count() == before_link_count == 1
