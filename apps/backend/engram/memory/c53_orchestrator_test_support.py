from __future__ import annotations

import importlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
from django.db import transaction
from django.utils import timezone

from engram.core.models import (
    Memory,
    MemoryCandidate,
    MemoryVersion,
    Organization,
    Project,
    Team,
    VisibilityScope,
    WorkflowRun,
    WorkflowRunOrigin,
    WorkflowWork,
)
from engram.memory.candidate_decision_work import ensure_candidate_decision_work_locked
from engram.memory.curation_judge import (
    ClaimEvidence,
    CurationEvidenceContext,
    CurationJudgeComparisonV1,
    CurationJudgeResult,
    CurationJudgeVerdictV1,
)
from engram.memory.curation_shortlist import CurationShortlist, CurationShortlistEntry
from engram.memory.distillation_provenance import session_candidate_content_hash
from engram.memory.transitions import (
    MemoryFence,
    MemoryTransitionError,
    PromoteMemoryCandidate,
    ReviseMemory,
    ReviseMemoryInput,
    TransitionRequest,
    TransitionScope,
    build_memory_fence,
)
from engram.memory.transitions_test_support import provenanced_candidate_in_scope, transition_request
from engram.memory.work_dispatch import queue_work_attempt
from engram.model_policy.models import ModelPolicy, ProviderCallRecord, ProviderSecret


def curation_module() -> Any:
    return importlib.import_module('engram.memory.curation')


def tasks_module() -> Any:
    return importlib.import_module('engram.memory.tasks')


def decide_memory_candidate() -> Any:
    return curation_module().DecideMemoryCandidate


_LONG_BODY = 'The retrieval pipeline ranks documents by cosine similarity over cached embeddings.'

EMBEDDING_1536: tuple[float, ...] = (0.1,) + (0.0,) * 1535


@dataclass(frozen=True, slots=True)
class OrchestratorScope:
    organization: Organization
    team: Team
    project: Project


def orchestrator_scope(suffix: str) -> OrchestratorScope:
    organization = Organization.objects.create(name=f'Decide Org {suffix}', slug=f'decide-org-{suffix}')
    team = Team.objects.create(organization=organization, name=f'Team {suffix}', slug=f'decide-team-{suffix}')
    project = Project.objects.create(
        organization=organization,
        name=f'Backend {suffix}',
        slug=f'decide-backend-{suffix}',
    )

    return OrchestratorScope(organization=organization, team=team, project=project)


def target_memory(scope: OrchestratorScope, *, suffix: str, title: str, body: str) -> Memory:
    candidate, _source, _session = provenanced_candidate_in_scope(
        scope.organization,
        scope.project,
        scope.team,
        suffix=f'target-{suffix}',
        title=title,
        body=body,
        visibility_scope=VisibilityScope.PROJECT,
    )
    result = PromoteMemoryCandidate().execute(transition_request(candidate))

    return result.memory


def current_version(memory: Memory) -> MemoryVersion:
    return MemoryVersion.objects.get(memory=memory, version=memory.current_version)


def subject_candidate(
    scope: OrchestratorScope,
    *,
    suffix: str,
    title: str = 'Subject claim about retry backoff',
    body: str = _LONG_BODY,
    visibility_scope: str = VisibilityScope.PROJECT,
) -> tuple[MemoryCandidate, WorkflowWork, WorkflowRun]:
    candidate, _source, _session = provenanced_candidate_in_scope(
        scope.organization,
        scope.project,
        scope.team,
        suffix=f'subject-{suffix}',
        title=title,
        body=body,
        visibility_scope=visibility_scope,
    )
    with transaction.atomic():
        work, _created = ensure_candidate_decision_work_locked(candidate)
    run = queue_work_attempt(work_id=work.id, now=timezone.now(), origin=WorkflowRunOrigin.AUTOMATIC)

    return candidate, work, run


def next_generation_work(candidate: MemoryCandidate) -> tuple[WorkflowWork, WorkflowRun]:
    with transaction.atomic():
        work, _created = ensure_candidate_decision_work_locked(candidate)
    run = queue_work_attempt(work_id=work.id, now=timezone.now(), origin=WorkflowRunOrigin.AUTOMATIC)

    return work, run


def curation_policy(scope: OrchestratorScope) -> ModelPolicy:
    secret = ProviderSecret.objects.create(
        organization=scope.organization,
        team=scope.team,
        name='Decide curation secret',
        provider='anthropic',
        scope='team',
        current_version=1,
    )

    return ModelPolicy.objects.create(
        organization=scope.organization,
        team=scope.team,
        project=scope.project,
        name='decide curation policy',
        scope='project',
        task_type='curation',
        provider='anthropic',
        model='claude-judge',
        secret=secret,
        version=1,
    )


def provider_call_record(scope: OrchestratorScope, policy: ModelPolicy) -> ProviderCallRecord:
    return ProviderCallRecord.objects.create(
        organization=scope.organization,
        project=scope.project,
        team=scope.team,
        policy=policy,
        secret=policy.secret,
        provider=policy.provider,
        model=policy.model,
        task_type=policy.task_type,
        policy_version=policy.version,
        request_id=f'curation-decision:{uuid.uuid4()}',
        redaction_state='redacted',
    )


def shortlist_entry(memory: Memory) -> CurationShortlistEntry:
    version = current_version(memory)

    return CurationShortlistEntry(
        memory_id=memory.id,
        memory_version_id=version.id,
        current_transition_id=memory.current_transition_id,
        visibility_scope=memory.visibility_scope,
        team_id=memory.team_id,
        title=memory.title,
        body=version.body,
        kind=memory.kind,
        body_hash='b' * 64,
        exact_overlap=0,
        vector_distance=0.10,
        lexical_rank=0.5,
        trigram_similarity=None,
        has_open_conflict=False,
    )


def stub_shortlist(
    *entries: CurationShortlistEntry,
    comparison_complete: bool = True,
    authorized_corpus_count: int | None = None,
) -> CurationShortlist:
    count = authorized_corpus_count if authorized_corpus_count is not None else len(entries)

    return CurationShortlist(
        entries=tuple(entries),
        manifest_hash='c' * 64,
        authorized_corpus_count=count,
        comparison_complete=comparison_complete,
    )


def stub_evidence(
    *,
    candidate_tier: str,
    target: MemoryVersion | None = None,
    target_tier: str = 'supported',
    candidate_newer: bool = True,
) -> CurationEvidenceContext:
    now = timezone.now()
    candidate_at = now if candidate_newer else now - timedelta(hours=2)
    target_at = now - timedelta(hours=1)
    candidate_claim = ClaimEvidence(tier=candidate_tier, refs=('cref-1', 'cref-2'), latest_evidence_at=candidate_at)
    targets: dict[uuid.UUID, ClaimEvidence] = {}
    if target is not None:
        targets[target.id] = ClaimEvidence(tier=target_tier, refs=('tref-1',), latest_evidence_at=target_at)

    return CurationEvidenceContext(candidate=candidate_claim, targets=targets)


_OUTCOME_TABLE = {
    'publish_new': ('unrelated', 'distinct_claim', 'different', 'not_applicable'),
    'merge_evidence': ('equivalent', 'equivalent_claim', 'same', 'not_applicable'),
    'revise_memory': ('candidate_revises', 'same_subject_revision', 'same', 'candidate_newer'),
    'supersede_memory': ('candidate_supersedes', 'ordered_replacement', 'same', 'candidate_newer'),
    'open_conflict': ('mutually_incompatible', 'same_scope_contradiction', 'same', 'unordered'),
}


def stub_verdict(outcome: str, *, target: MemoryVersion | None = None) -> CurationJudgeVerdictV1:
    relation, reason_code, applicability, temporal = _OUTCOME_TABLE[outcome]
    comparisons: tuple[CurationJudgeComparisonV1, ...] = ()
    target_id = None
    if target is not None:
        target_id = target.id
        comparisons = (CurationJudgeComparisonV1(target.id, relation, ('tref-1',)),)

    return CurationJudgeVerdictV1(
        schema_version=1,
        outcome=outcome,
        relation=relation,
        target_memory_version_id=target_id,
        candidate_evidence_refs=('cref-1',),
        comparisons=comparisons,
        applicability=applicability,
        temporal_order=temporal,
        reason_code=reason_code,
        reason='decision under contract v1',
    )


def stub_judge_result(
    verdict: CurationJudgeVerdictV1,
    call_record: ProviderCallRecord,
    policy: ModelPolicy,
    shortlist: CurationShortlist,
) -> CurationJudgeResult:
    return CurationJudgeResult(
        verdict=verdict,
        provider_call_record_id=call_record.id,
        policy_id=policy.id,
        policy_version=policy.version,
        response_hash='d' * 64,
        fallback_used=False,
        comparison_manifest_hash=shortlist.manifest_hash,
        authorized_corpus_count=shortlist.authorized_corpus_count,
        comparison_complete=shortlist.comparison_complete,
    )


def _forbidden(*_args: object, **_kwargs: object) -> Any:
    raise AssertionError('deterministic terminal must not call embedding, shortlist, or judge')


def enable_rollout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(curation_module(), 'candidate_decision_enabled', lambda _work: True, raising=False)
    monkeypatch.setattr(tasks_module(), 'candidate_decision_enabled', lambda _work: True, raising=False)


def disable_rollout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(curation_module(), 'candidate_decision_enabled', lambda _work: False, raising=False)
    monkeypatch.setattr(tasks_module(), 'candidate_decision_enabled', lambda _work: False, raising=False)


def install_decision_services(
    monkeypatch: pytest.MonkeyPatch,
    *,
    embedding: tuple[float, ...] | None,
    shortlist: CurationShortlist,
    evidence: CurationEvidenceContext,
    judge_result: CurationJudgeResult,
) -> None:
    module = curation_module()
    monkeypatch.setattr(module, 'resolve_candidate_embedding', lambda *_a, **_k: embedding, raising=False)
    monkeypatch.setattr(module, 'build_curation_shortlist', lambda *_a, **_k: shortlist, raising=False)
    monkeypatch.setattr(module, 'build_curation_evidence_context', lambda *_a, **_k: evidence, raising=False)
    monkeypatch.setattr(module, 'judge_curation_candidate', lambda *_a, **_k: judge_result, raising=False)


def install_judged_decision(
    monkeypatch: pytest.MonkeyPatch,
    *,
    embedding: tuple[float, ...] | None,
    shortlist: CurationShortlist,
    evidence: CurationEvidenceContext,
    judge_result: CurationJudgeResult,
) -> None:
    enable_rollout(monkeypatch)
    install_decision_services(
        monkeypatch, embedding=embedding, shortlist=shortlist, evidence=evidence, judge_result=judge_result
    )


def install_deterministic_only(monkeypatch: pytest.MonkeyPatch) -> None:
    module = curation_module()
    enable_rollout(monkeypatch)
    monkeypatch.setattr(module, 'resolve_candidate_embedding', _forbidden, raising=False)
    monkeypatch.setattr(module, 'build_curation_shortlist', _forbidden, raising=False)
    monkeypatch.setattr(module, 'judge_curation_candidate', _forbidden, raising=False)


def install_fault(monkeypatch: pytest.MonkeyPatch, point: str, action: Callable[[], None]) -> None:
    def boundary(where: str) -> None:
        if where == point:
            action()

    monkeypatch.setattr(curation_module(), '_fault_boundary', boundary, raising=False)


def advance_target_memory(memory: Memory, *, title: str, body: str) -> None:
    request = TransitionRequest(
        scope=TransitionScope(
            organization_id=memory.organization_id,
            project_id=memory.project_id,
            team_id=memory.team_id,
        ),
        idempotency_key=f'advance:{memory.id}:{uuid.uuid4()}:v1',
        actor_type='test',
        actor_id='orchestrator-tests',
        capability='memories:write',
        request_id=str(uuid.uuid4()),
        correlation_id=str(uuid.uuid4()),
        reason='competing advance',
        origin='orchestrator-tests',
    )
    fence: MemoryFence = build_memory_fence(memory)
    ReviseMemory().execute(ReviseMemoryInput(request=request, memory_fence=fence, title=title, body=body))
    memory.refresh_from_db()


class RaisingTransition:
    def execute(self, _data: object) -> Any:
        raise MemoryTransitionError('transition_contention', 'injected cp4 fault', retryable=True)


def patch_cp4_service(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setattr(curation_module(), name, RaisingTransition, raising=False)


def steal_work_lease(work: WorkflowWork) -> None:
    token = WorkflowWork.objects.values_list('fencing_token', flat=True).get(id=work.id)
    now = timezone.now()
    WorkflowWork.objects.filter(id=work.id).update(
        fencing_token=token + 5,
        lease_owner='thief-worker',
        heartbeat_at=now - timedelta(seconds=2),
        lease_expires_at=now - timedelta(seconds=1),
    )


def mutate_candidate_generation(candidate: MemoryCandidate, *, new_title: str) -> None:
    session_id = candidate.source_observation.session_id
    candidate.title = new_title
    candidate.content_hash = session_candidate_content_hash(session_id, new_title, candidate.body)
    candidate.save(update_fields=['title', 'content_hash', 'updated_at'])


def run_decision(work: WorkflowWork, run: WorkflowRun) -> tuple[str | None, BaseException | None]:
    task = tasks_module().process_candidate_decision_work_v1
    try:
        return task(str(work.id), str(run.id)), None
    except Exception as error:  # noqa: BLE001 - the task re-raises operational failures after recording them
        return None, error


def curation_decisions_for(candidate: MemoryCandidate) -> list[Any]:
    from engram.core.models import CurationDecision

    return list(CurationDecision.objects.filter(candidate=candidate))
