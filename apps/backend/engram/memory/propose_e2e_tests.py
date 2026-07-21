from __future__ import annotations

import json
import uuid

import pytest

from engram.access.services import EffectiveScope
from engram.core.models import (
    CandidateStatus,
    Memory,
    MemoryCandidate,
    MemoryCandidateSource,
    Organization,
    Project,
    ProjectTeam,
    Team,
    VisibilityScope,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowSubjectType,
    WorkflowWork,
    WorkflowWorkExecutionState,
    WorkflowWorkType,
)
from engram.memory import c53_orchestrator_test_support as orch
from engram.memory.curation_judge import CurationJudgeComparisonV1, CurationJudgeVerdictV1
from engram.memory.memory_propose_service import ProposeMemory, ProposeMemoryInput
from engram.model_policy.models import ModelPolicy, ProviderCallRecord, ProviderSecret


def _scope() -> tuple[orch.OrchestratorScope, EffectiveScope]:
    organization = Organization.objects.create(name='Propose E2E', slug=f'propose-e2e-{uuid.uuid4().hex[:8]}')
    team = Team.objects.create(organization=organization, name='Team', slug=f'team-{uuid.uuid4().hex[:8]}')
    project = Project.objects.create(organization=organization, name='Backend', slug=f'proj-{uuid.uuid4().hex[:8]}')
    orch_scope = orch.OrchestratorScope(organization=organization, team=team, project=project)
    api_key_id = uuid.uuid4()
    effective = EffectiveScope(
        organization_id=organization.id,
        identity_id=uuid.uuid4(),
        api_key_id=api_key_id,
        project_ids=(project.id,),
        team_ids=(),
        capabilities=('memories:propose',),
        actor_type='api_key',
        actor_id=str(api_key_id),
        project_bound=False,
        team_bound=False,
    )

    return orch_scope, effective


def _project_policy(orch_scope: orch.OrchestratorScope) -> tuple[ModelPolicy, ProviderCallRecord]:
    secret = ProviderSecret.objects.create(
        organization=orch_scope.organization,
        team=None,
        name=f'e2e secret {uuid.uuid4().hex[:8]}',
        provider='anthropic',
        scope='organization',
        current_version=1,
    )
    policy = ModelPolicy.objects.create(
        organization=orch_scope.organization,
        team=None,
        project=orch_scope.project,
        name=f'e2e policy {uuid.uuid4().hex[:8]}',
        scope='project',
        task_type='curation',
        provider='anthropic',
        model='claude-judge',
        secret=secret,
        version=1,
    )
    call = ProviderCallRecord.objects.create(
        organization=orch_scope.organization,
        project=orch_scope.project,
        team=None,
        policy=policy,
        secret=secret,
        provider=policy.provider,
        model=policy.model,
        task_type=policy.task_type,
        policy_version=policy.version,
        request_id=f'curation-decision:{uuid.uuid4()}',
        redaction_state='redacted',
    )

    return policy, call


def _propose(
    effective: EffectiveScope,
    orch_scope: orch.OrchestratorScope,
    *,
    title: str,
    body: str,
    team_id: uuid.UUID | None = None,
) -> tuple[MemoryCandidate, WorkflowWork, WorkflowRun]:
    result = ProposeMemory().execute(
        ProposeMemoryInput(
            scope=effective,
            project=orch_scope.project,
            team_id=team_id,
            title=title,
            body=body,
            kind='',
            request_id=f'req-{uuid.uuid4()}',
            correlation_id='',
        )
    )
    candidate = MemoryCandidate.objects.get(id=result.candidate_id)
    work = WorkflowWork.objects.get(
        subject_type=WorkflowSubjectType.MEMORY_CANDIDATE,
        subject_id=candidate.id,
        work_type=WorkflowWorkType.CANDIDATE_DECISION,
    )
    run = WorkflowRun.objects.filter(work=work, status=WorkflowRunStatus.QUEUED).order_by('-created_at').first()

    return candidate, work, run


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False


def _opener_returning(body: bytes) -> object:
    def opener(request: object, timeout: float = 30) -> _FakeResponse:
        opener.requests.append(request)

        return _FakeResponse(body)

    opener.requests = []

    return opener


def _completion_bytes(payload: dict[str, object]) -> bytes:
    return json.dumps({'choices': [{'message': {'content': json.dumps(payload)}}]}).encode()


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    shortlist: object,
    judge_result: object,
) -> None:
    orch.enable_rollout(monkeypatch)
    module = orch.curation_module()
    monkeypatch.setattr(module, 'resolve_candidate_embedding', lambda *_a, **_k: orch.EMBEDDING_1536, raising=False)
    monkeypatch.setattr(module, 'build_curation_shortlist', lambda *_a, **_k: shortlist, raising=False)
    monkeypatch.setattr(module, 'judge_curation_candidate', lambda *_a, **_k: judge_result, raising=False)
    monkeypatch.setattr(module, 'revalidate_curation_shortlist', orch._stub_revalidate, raising=False)


@pytest.mark.django_db
def test_agent_proposal_publish_new_reaches_approved_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    orch_scope, effective = _scope()
    policy, call = _project_policy(orch_scope)
    candidate, work, run = _propose(
        effective,
        orch_scope,
        title='Deploy gate policy',
        body='The production deploy pipeline requires a manual approval step before release.',
    )
    shortlist = orch.stub_shortlist(comparison_complete=True, authorized_corpus_count=0)
    judge = orch.stub_judge_result(orch.stub_verdict('publish_new'), call, policy, shortlist)
    _install(monkeypatch, shortlist=shortlist, judge_result=judge)

    _result, error = orch.run_decision(work, run)

    assert error is None
    candidate.refresh_from_db()
    assert candidate.status == CandidateStatus.PROMOTED
    assert candidate.promoted_memory_id is not None
    assert Memory.objects.filter(id=candidate.promoted_memory_id).exists()


@pytest.mark.django_db
def test_agent_proposal_reject_redundant_settles_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    orch_scope, effective = _scope()
    policy, call = _project_policy(orch_scope)
    target = orch.target_memory(
        orch_scope,
        suffix='reject',
        title='Cache eviction policy',
        body='The hot cache tier evicts the oldest entries first under memory pressure.',
    )
    target_version = orch.current_version(target)
    candidate, work, run = _propose(
        effective,
        orch_scope,
        title='Deploy approval requirement',
        body='The production deploy pipeline requires a manual approval step before release.',
    )
    shortlist = orch.stub_shortlist(orch.shortlist_entry(target))
    verdict = CurationJudgeVerdictV1(
        schema_version=1,
        outcome='reject_candidate',
        relation='redundant',
        target_memory_version_id=target_version.id,
        candidate_evidence_refs=('cref-1',),
        comparisons=(CurationJudgeComparisonV1(target_version.id, 'redundant', ('tref-1',)),),
        applicability='same',
        temporal_order='not_applicable',
        reason_code='redundant_claim',
        reason='duplicate of existing memory',
    )
    judge = orch.stub_judge_result(verdict, call, policy, shortlist)
    _install(monkeypatch, shortlist=shortlist, judge_result=judge)

    _result, error = orch.run_decision(work, run)

    assert error is None
    candidate.refresh_from_db()
    assert candidate.status == CandidateStatus.REJECTED


@pytest.mark.django_db
def test_agent_proposal_team_publish_new_creates_team_visible_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    orch_scope, effective = _scope()
    ProjectTeam.objects.create(
        organization=orch_scope.organization,
        project=orch_scope.project,
        team=orch_scope.team,
    )
    policy, call = _project_policy(orch_scope)
    candidate, work, run = _propose(
        effective,
        orch_scope,
        title='Team deploy gate policy',
        body='The team production deploy pipeline requires a manual approval step before release.',
        team_id=orch_scope.team.id,
    )
    assert candidate.visibility_scope == VisibilityScope.TEAM
    assert candidate.team_id == orch_scope.team.id
    shortlist = orch.stub_shortlist(comparison_complete=True, authorized_corpus_count=0)
    judge = orch.stub_judge_result(orch.stub_verdict('publish_new'), call, policy, shortlist)
    _install(monkeypatch, shortlist=shortlist, judge_result=judge)

    _result, error = orch.run_decision(work, run)

    assert error is None
    candidate.refresh_from_db()
    assert candidate.status == CandidateStatus.PROMOTED
    memory = Memory.objects.get(id=candidate.promoted_memory_id)
    assert memory.visibility_scope == VisibilityScope.TEAM
    assert memory.team_id == orch_scope.team.id


@pytest.mark.django_db
def test_agent_proposal_cross_visibility_open_conflict_terminates(monkeypatch: pytest.MonkeyPatch) -> None:
    from engram.memory import curation_judge
    from engram.model_policy.services import OpenAICompatibleGateway

    orch_scope, effective = _scope()
    ProjectTeam.objects.create(
        organization=orch_scope.organization,
        project=orch_scope.project,
        team=orch_scope.team,
    )
    _policy, _call = _project_policy(orch_scope)
    target = orch.target_memory(
        orch_scope,
        suffix='xvis-conflict',
        title='Cache eviction policy',
        body='The hot cache tier evicts the oldest entries first under sustained memory pressure.',
    )
    target_version = orch.current_version(target)
    candidate, work, run = _propose(
        effective,
        orch_scope,
        title='Team cache eviction rule',
        body='The team hot cache tier evicts the newest entries first under sustained memory pressure.',
        team_id=orch_scope.team.id,
    )
    entry = orch.shortlist_entry(target)
    shortlist = orch.stub_shortlist(entry)
    evidence = orch.stub_evidence(candidate_tier='supported', target=target_version, target_tier='supported')

    conflict_payload = {
        'schema_version': 1,
        'outcome': 'open_conflict',
        'relation': 'mutually_incompatible',
        'target_memory_version_id': str(target_version.id),
        'candidate_evidence_refs': ['cref-1'],
        'comparisons': [
            {
                'memory_version_id': str(target_version.id),
                'relation': 'mutually_incompatible',
                'target_evidence_refs': ['tref-1'],
            }
        ],
        'applicability': 'same',
        'temporal_order': 'unordered',
        'reason_code': 'same_scope_contradiction',
        'reason': 'rule-ignoring cross-visibility conflict',
    }
    orch.enable_rollout(monkeypatch)
    module = orch.curation_module()
    monkeypatch.setattr(module, 'resolve_candidate_embedding', lambda *_a, **_k: orch.EMBEDDING_1536, raising=False)
    monkeypatch.setattr(module, 'build_curation_shortlist', lambda *_a, **_k: shortlist, raising=False)
    monkeypatch.setattr(module, 'build_curation_evidence_context', lambda *_a, **_k: evidence, raising=False)
    opener = _opener_returning(_completion_bytes(conflict_payload))
    gateway = OpenAICompatibleGateway(base_url='https://provider.example/v1', api_key='key', opener=opener)
    monkeypatch.setattr(curation_judge, 'get_provider_gateway', lambda *_a, **_k: gateway)

    _result, error = orch.run_decision(work, run)

    assert error is not None
    assert getattr(error, 'code', None) == 'judge_cross_visibility_denied'
    work.refresh_from_db()
    assert work.execution_state == WorkflowWorkExecutionState.TERMINAL_FAILURE
    candidate.refresh_from_db()
    assert candidate.status != CandidateStatus.PROMOTED


@pytest.mark.django_db
def test_corrupted_anchors_hash_does_not_promote(monkeypatch: pytest.MonkeyPatch) -> None:
    orch_scope, effective = _scope()
    policy, call = _project_policy(orch_scope)
    candidate, work, run = _propose(
        effective,
        orch_scope,
        title='Forged fact',
        body='This claim was forged by mutating the provenance anchors hash after creation.',
    )
    MemoryCandidateSource.objects.filter(candidate_id=candidate.id).update(anchors_hash='f' * 64)
    shortlist = orch.stub_shortlist(comparison_complete=True, authorized_corpus_count=0)
    judge = orch.stub_judge_result(orch.stub_verdict('publish_new'), call, policy, shortlist)
    _install(monkeypatch, shortlist=shortlist, judge_result=judge)

    _result, _error = orch.run_decision(work, run)

    candidate.refresh_from_db()
    assert candidate.status != CandidateStatus.PROMOTED
    assert candidate.promoted_memory_id is None
    assert not Memory.objects.filter(project_id=orch_scope.project.id).exists()
