from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from django.utils import timezone
from django_celery_outbox.models import CeleryOutbox

from engram.core.models import (
    AuditEvent,
    CandidateStatus,
    LinkType,
    MemoryCandidate,
    MemoryConflict,
    MemoryLink,
    Organization,
    Project,
    WorkflowRun,
    WorkflowSubjectType,
    WorkflowWork,
    WorkflowWorkDisposition,
    WorkflowWorkExecutionState,
    WorkflowWorkType,
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
from engram.memory.candidate_ttl import (
    TTL_REASON,
    ExpireStaleCandidates,
    candidate_review_ttl_days,
    candidate_ttl_batch,
    candidate_ttl_dry_run,
)
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


def _decision_work(candidate: MemoryCandidate, *, execution_state: str) -> WorkflowWork:
    return WorkflowWork.objects.create(
        organization=candidate.organization,
        project=candidate.project,
        team=candidate.team,
        work_type=WorkflowWorkType.CANDIDATE_DECISION,
        subject_type=WorkflowSubjectType.MEMORY_CANDIDATE,
        subject_id=candidate.id,
        input_fingerprint=f'{candidate.id.int:064x}'[:64],
        input_snapshot={'schema': 'candidate_decision_input/v1', 'candidate_id': str(candidate.id)},
        disposition=WorkflowWorkDisposition.REQUIRED,
        execution_state=execution_state,
        next_retry_at=(timezone.now() if execution_state == WorkflowWorkExecutionState.RETRY_WAIT else None),
    )


@pytest.fixture
def f_org() -> Organization:
    return Organization.objects.create(name='Sweep', slug='sweep')


@pytest.fixture
def f_project(f_org: Organization) -> Project:
    return Project.objects.create(organization=f_org, name='Eng', slug='eng')


def test_ttl_settings_read_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('ENGRAM_CANDIDATE_REVIEW_TTL_DAYS', raising=False)
    monkeypatch.delenv('ENGRAM_CANDIDATE_TTL_BATCH', raising=False)

    assert candidate_review_ttl_days() == 30
    assert candidate_ttl_batch() == 500

    monkeypatch.setenv('ENGRAM_CANDIDATE_REVIEW_TTL_DAYS', '7')
    monkeypatch.setenv('ENGRAM_CANDIDATE_TTL_BATCH', '9')

    assert candidate_review_ttl_days() == 7
    assert candidate_ttl_batch() == 9

    monkeypatch.setenv('ENGRAM_CANDIDATE_REVIEW_TTL_DAYS', '0')
    with pytest.raises(ValueError):
        candidate_review_ttl_days()


@pytest.mark.django_db
def test_stale_proposed_candidate_is_expired_with_an_audit_trail(
    f_org: Organization,
    f_project: Project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('ENGRAM_CANDIDATE_REVIEW_TTL_DAYS', '14')
    candidate = _make_candidate(f_org, f_project, created_at=timezone.now() - timedelta(days=20))

    result = ExpireStaleCandidates().execute(dry_run=False)

    candidate.refresh_from_db()
    audit = AuditEvent.objects.get(target_id=str(candidate.id), event_type='MemoryAutoRejected')
    assert result.scanned == 1
    assert result.rejected == 1
    assert result.dry_run is False
    assert result.candidate_ids == (str(candidate.id),)
    assert candidate.status == CandidateStatus.REJECTED
    assert audit.metadata['reason'] == TTL_REASON
    assert audit.metadata['ttl_days'] == 14
    assert audit.actor_type == 'system'


@pytest.mark.django_db
def test_dry_run_reports_the_same_candidates_without_writing(
    f_org: Organization,
    f_project: Project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('ENGRAM_CANDIDATE_REVIEW_TTL_DAYS', '14')
    candidate = _make_candidate(f_org, f_project, created_at=timezone.now() - timedelta(days=20))

    preview = ExpireStaleCandidates().execute(dry_run=True)

    candidate.refresh_from_db()
    assert preview.scanned == 1
    assert preview.rejected == 0
    assert preview.dry_run is True
    assert preview.candidate_ids == (str(candidate.id),)
    assert candidate.status == CandidateStatus.PROPOSED
    assert not AuditEvent.objects.filter(target_id=str(candidate.id)).exists()


@pytest.mark.django_db
def test_dry_run_can_be_forced_from_the_environment(
    f_org: Organization,
    f_project: Project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('ENGRAM_CANDIDATE_REVIEW_TTL_DAYS', '14')
    monkeypatch.setenv('ENGRAM_CANDIDATE_TTL_DRY_RUN', 'true')
    candidate = _make_candidate(f_org, f_project, created_at=timezone.now() - timedelta(days=20))

    result = ExpireStaleCandidates().execute(dry_run=False)

    candidate.refresh_from_db()
    assert result.dry_run is True
    assert result.rejected == 0
    assert candidate.status == CandidateStatus.PROPOSED


@pytest.mark.django_db
def test_fresh_candidate_is_untouched(
    f_org: Organization,
    f_project: Project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('ENGRAM_CANDIDATE_REVIEW_TTL_DAYS', '14')
    candidate = _make_candidate(f_org, f_project, created_at=timezone.now())

    result = ExpireStaleCandidates().execute(dry_run=False)

    candidate.refresh_from_db()
    assert result.scanned == 0
    assert result.rejected == 0
    assert candidate.status == CandidateStatus.PROPOSED


@pytest.mark.django_db
def test_high_confidence_stale_candidate_expires_on_age_alone(
    f_org: Organization,
    f_project: Project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('ENGRAM_CANDIDATE_REVIEW_TTL_DAYS', '14')
    candidate = _make_candidate(
        f_org,
        f_project,
        confidence='0.900',
        created_at=timezone.now() - timedelta(days=30),
    )

    result = ExpireStaleCandidates().execute(dry_run=False)

    candidate.refresh_from_db()
    assert result.rejected == 1
    assert candidate.status == CandidateStatus.REJECTED


@pytest.mark.django_db
def test_candidate_with_active_decision_work_is_never_expired(
    f_org: Organization,
    f_project: Project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('ENGRAM_CANDIDATE_REVIEW_TTL_DAYS', '14')
    candidate = _make_candidate(f_org, f_project, created_at=timezone.now() - timedelta(days=40))
    _decision_work(candidate, execution_state=WorkflowWorkExecutionState.RETRY_WAIT)

    result = ExpireStaleCandidates().execute(dry_run=False)

    candidate.refresh_from_db()
    assert result.scanned == 0
    assert result.rejected == 0
    assert candidate.status == CandidateStatus.PROPOSED


@pytest.mark.django_db
def test_candidate_with_terminal_decision_work_is_expired(
    f_org: Organization,
    f_project: Project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('ENGRAM_CANDIDATE_REVIEW_TTL_DAYS', '14')
    candidate = _make_candidate(f_org, f_project, created_at=timezone.now() - timedelta(days=40))
    _decision_work(candidate, execution_state=WorkflowWorkExecutionState.TERMINAL_FAILURE)

    result = ExpireStaleCandidates().execute(dry_run=False)

    candidate.refresh_from_db()
    assert result.rejected == 1
    assert candidate.status == CandidateStatus.REJECTED


@pytest.mark.django_db
def test_sweep_is_capped_by_the_configured_batch(
    f_org: Organization,
    f_project: Project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('ENGRAM_CANDIDATE_REVIEW_TTL_DAYS', '14')
    monkeypatch.setenv('ENGRAM_CANDIDATE_TTL_BATCH', '2')
    base = timezone.now() - timedelta(days=30)
    candidates = [_make_candidate(f_org, f_project, created_at=base + timedelta(hours=index)) for index in range(5)]

    result = ExpireStaleCandidates().execute(dry_run=False)

    for candidate in candidates:
        candidate.refresh_from_db()

    assert result.rejected == 2
    assert [candidate.status for candidate in candidates] == [
        CandidateStatus.REJECTED,
        CandidateStatus.REJECTED,
        CandidateStatus.PROPOSED,
        CandidateStatus.PROPOSED,
        CandidateStatus.PROPOSED,
    ]


@pytest.mark.django_db
def test_second_run_expires_nothing_more(
    f_org: Organization,
    f_project: Project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('ENGRAM_CANDIDATE_REVIEW_TTL_DAYS', '14')
    _make_candidate(f_org, f_project, created_at=timezone.now() - timedelta(days=20))

    first = ExpireStaleCandidates().execute(dry_run=False)
    second = ExpireStaleCandidates().execute(dry_run=False)

    assert first.rejected == 1
    assert second.rejected == 0
    assert AuditEvent.objects.filter(event_type='MemoryAutoRejected').count() == 1


@pytest.mark.django_db
def test_expiry_never_queues_candidate_decision_work(
    f_org: Organization,
    f_project: Project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('ENGRAM_CANDIDATE_REVIEW_TTL_DAYS', '14')
    _make_candidate(f_org, f_project, created_at=timezone.now() - timedelta(days=20))

    ExpireStaleCandidates().execute(dry_run=False)

    assert not WorkflowWork.objects.exists()
    assert not WorkflowRun.objects.exists()
    assert not CeleryOutbox.objects.exists()


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
    second = ReconcileCandidateDecisionWork().execute(as_of=as_of + timedelta(minutes=1))

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
def test_unresolved_conflict_is_excluded_from_ttl_even_when_old(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('ENGRAM_CANDIDATE_REVIEW_TTL_DAYS', '14')
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

    result = ExpireStaleCandidates().execute(dry_run=False)

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
def test_resolved_conflict_allows_later_ttl_noop_without_erasing_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('ENGRAM_CANDIDATE_REVIEW_TTL_DAYS', '14')
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
    result = ExpireStaleCandidates().execute(dry_run=False)

    assert resolved.transition.id == conflict.resolved_transition_id
    assert conflict.resolution == 'reject_candidate'
    assert candidate.status == CandidateStatus.REJECTED
    assert result.rejected == 0
    assert MemoryLink.objects.filter(id=conflict.semantic_link_id).count() == before_link_count == 1


@pytest.mark.django_db
def test_sweep_previews_by_default_so_an_unchanged_caller_cannot_mass_reject(
    f_org: Organization,
    f_project: Project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv('ENGRAM_CANDIDATE_TTL_DRY_RUN', raising=False)
    monkeypatch.setenv('ENGRAM_CANDIDATE_REVIEW_TTL_DAYS', '14')
    candidate = _make_candidate(f_org, f_project, created_at=timezone.now() - timedelta(days=20))

    result = ExpireStaleCandidates().execute()

    candidate.refresh_from_db()
    assert result.dry_run is True
    assert result.rejected == 0
    assert candidate.status == CandidateStatus.PROPOSED


def test_kill_switch_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('ENGRAM_CANDIDATE_TTL_DRY_RUN', raising=False)
    assert candidate_ttl_dry_run() is False

    monkeypatch.setenv('ENGRAM_CANDIDATE_TTL_DRY_RUN', '1')
    assert candidate_ttl_dry_run() is True
