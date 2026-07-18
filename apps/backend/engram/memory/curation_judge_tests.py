from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import pytest
from django.utils import timezone

from engram.core.models import (
    CurationDecision,
    Memory,
    MemoryCandidate,
    MemoryCandidateSource,
    MemoryConflict,
    MemoryTransition,
    MemoryVersionSource,
    Observation,
    SessionStatus,
    VisibilityScope,
    WorkflowWork,
)
from engram.memory.curation_test_support import create_curation_policy
from engram.memory.deterministic_gates import EffectiveCandidateScope, SanitizedCandidateView
from engram.memory.distillation_provenance import candidate_source_anchors, canonical_source_manifest
from engram.memory.distillation_window import materialize_distillation_window
from engram.memory.session_lifecycle import EndSession
from engram.memory.transitions_test_support import (
    _stage_history,
    candidate_in_scope,
    provenanced_candidate,
    provenanced_candidate_in_scope,
    transition_request,
    transitions_module,
)
from engram.memory.workflow_work import observation_content_digest
from engram.model_policy.services import ProviderCallResult

_CANDIDATE_EVIDENCE_AT = datetime(2026, 2, 1, tzinfo=UTC)
_TARGET_EVIDENCE_AT = datetime(2026, 1, 1, tzinfo=UTC)


def _judge_module() -> object:
    from engram.memory import curation_judge

    return curation_judge


def _shortlist_module() -> object:
    from engram.memory import curation_shortlist

    return curation_shortlist


def _fixture(*, comparison_complete: bool = True) -> tuple[object, object, object, object]:
    module = _judge_module()
    shortlist_module = _shortlist_module()
    candidate, _source, scope = provenanced_candidate('judge-contract')
    target_id = uuid.uuid4()
    transition_id = uuid.uuid4()
    entry = shortlist_module.CurationShortlistEntry(
        memory_id=uuid.uuid4(),
        memory_version_id=target_id,
        current_transition_id=transition_id,
        visibility_scope=VisibilityScope.PROJECT,
        team_id=None,
        title='target title',
        body='target body',
        kind='decision',
        body_hash='a' * 64,
        exact_overlap=0,
        vector_distance=0.1,
        lexical_rank=0.0,
        trigram_similarity=0.0,
        has_open_conflict=False,
    )
    shortlist = shortlist_module.CurationShortlist(
        entries=(entry,),
        manifest_hash='b' * 64,
        authorized_corpus_count=1,
        comparison_complete=comparison_complete,
    )
    evidence = module.CurationEvidenceContext(
        candidate=module.ClaimEvidence(
            tier='corroborated',
            refs=('candidate-ref', 'candidate-ref-2'),
            latest_evidence_at=_CANDIDATE_EVIDENCE_AT,
        ),
        targets={
            target_id: module.ClaimEvidence(
                tier='supported',
                refs=('target-ref',),
                latest_evidence_at=_TARGET_EVIDENCE_AT,
            )
        },
    )
    data = module.CurationJudgeInput(
        organization_id=scope[0].id,
        project_id=scope[1].id,
        candidate_id=candidate.id,
        candidate=SanitizedCandidateView(
            title='candidate title',
            body='candidate body',
            kind='decision',
            evidence=({'source': 'opaque'},),
            content_hash='c' * 64,
            redaction_codes=(),
        ),
        effective_scope=EffectiveCandidateScope(VisibilityScope.PROJECT, None),
        shortlist=shortlist,
        evidence=evidence,
        request_id='judge-request-1',
        trace_id='judge-trace-1',
    )
    return module, data, entry, candidate


def _two_entry_fixture() -> tuple[object, object, tuple[object, object], object]:
    module, data, first, candidate = _fixture()
    second = replace(
        first,
        memory_id=uuid.uuid4(),
        memory_version_id=uuid.uuid4(),
        current_transition_id=uuid.uuid4(),
        title='second target title',
        body='second target body',
        body_hash='d' * 64,
    )
    shortlist = replace(data.shortlist, entries=(first, second), authorized_corpus_count=2)
    evidence = replace(
        data.evidence,
        targets={
            first.memory_version_id: data.evidence.targets[first.memory_version_id],
            second.memory_version_id: module.ClaimEvidence(
                tier='supported',
                refs=('target-ref-2',),
                latest_evidence_at=_TARGET_EVIDENCE_AT,
            ),
        },
    )
    return module, replace(data, shortlist=shortlist, evidence=evidence), (first, second), candidate


def _payload(
    entry: object,
    *,
    outcome: str = 'publish_new',
    relation: str = 'unrelated',
    target: str | None = None,
    candidate_refs: list[str] | None = None,
    target_refs: list[str] | None = None,
    comparisons: list[dict[str, object]] | None = None,
    reason_code: str = 'distinct_claim',
    applicability: str = 'same',
    temporal_order: str = 'not_applicable',
    reason: str = 'distinct claim',
) -> dict[str, object]:
    memory_version_id = str(entry.memory_version_id)
    return {
        'schema_version': 1,
        'outcome': outcome,
        'relation': relation,
        'target_memory_version_id': target,
        'candidate_evidence_refs': (
            candidate_refs if candidate_refs is not None else ['candidate-ref', 'candidate-ref-2']
        ),
        'comparisons': comparisons
        if comparisons is not None
        else [
            {
                'memory_version_id': memory_version_id,
                'relation': relation,
                'target_evidence_refs': target_refs if target_refs is not None else ['target-ref'],
            },
        ],
        'applicability': applicability,
        'temporal_order': temporal_order,
        'reason_code': reason_code,
        'reason': reason,
    }


def _manifest_comparisons(entries: tuple[object, ...]) -> list[dict[str, object]]:
    return [
        {
            'memory_version_id': str(entry.memory_version_id),
            'relation': 'unrelated',
            'target_evidence_refs': [f'target-ref-{index + 1}'],
        }
        for index, entry in enumerate(entries)
    ]


def _semantic_snapshot(project_id: UUID) -> dict[str, tuple[object, ...]]:
    return {
        'decisions': tuple(
            CurationDecision.objects.filter(project_id=project_id)
            .order_by('id')
            .values_list('id', 'outcome', 'target_memory_version_id')
        ),
        'transitions': tuple(
            MemoryTransition.objects.filter(project_id=project_id)
            .order_by('id')
            .values_list('id', 'transition_type', 'memory_id', 'from_version_id', 'to_version_id')
        ),
        'conflicts': tuple(
            MemoryConflict.objects.filter(project_id=project_id)
            .order_by('id')
            .values_list(
                'id',
                'memory_id',
                'candidate_id',
                'resolved_transition_id',
                'resolution',
                'resolved_at',
            )
        ),
        'candidates': tuple(
            MemoryCandidate.objects.filter(project_id=project_id)
            .order_by('id')
            .values_list('id', 'status', 'promoted_memory_id')
        ),
        'memories': tuple(
            Memory.objects.filter(project_id=project_id)
            .order_by('id')
            .values_list('id', 'status', 'stale', 'refuted', 'current_transition_id', 'current_version')
        ),
    }


@contextmanager
def _unchanged(project_id: UUID) -> Iterator[None]:
    before = _semantic_snapshot(project_id)
    yield
    assert _semantic_snapshot(project_id) == before


@pytest.mark.django_db
def test_malformed_judge_output_has_no_default_semantic_outcome() -> None:
    module, data, _entry, candidate = _fixture()
    for raw in ('', '[]', 'null', '{"decision":"keep_both"}'):
        with _unchanged(candidate.project_id), pytest.raises(ValueError) as error:
            module.parse_curation_judge_verdict(raw, data)
        assert getattr(error.value, 'code', None) == 'judge_invalid_output'


@pytest.mark.django_db
def test_judge_cannot_reference_memory_or_evidence_outside_manifest() -> None:
    module, data, entry, candidate = _fixture()
    foreign_target = str(uuid.uuid4())
    foreign_payload = _payload(entry, target=foreign_target, relation='equivalent', outcome='merge_evidence')

    with _unchanged(candidate.project_id), pytest.raises(ValueError) as error:
        module.parse_curation_judge_verdict(json.dumps(foreign_payload), data)

    assert getattr(error.value, 'code', None) == 'judge_invalid_output'

    foreign_payload = _payload(entry, relation='equivalent', outcome='merge_evidence')
    foreign_payload['comparisons'][0]['target_evidence_refs'] = ['not-in-manifest']
    with _unchanged(candidate.project_id), pytest.raises(ValueError) as error:
        module.parse_curation_judge_verdict(json.dumps(foreign_payload), data)

    assert getattr(error.value, 'code', None) == 'judge_reference_invalid'


@pytest.mark.django_db
def test_fallback_verdict_must_pass_the_same_strict_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    module, data, entries, candidate = _two_entry_fixture()
    data = replace(data, candidate=replace(data.candidate, body='Use sk-abcdefghijklmnop in production'))
    entry = entries[0]
    candidate.refresh_from_db()
    organization, project = candidate.organization, candidate.project
    session_team = candidate.team
    primary = create_curation_policy(organization, session_team, project, task_type='curation')
    primary.fallback_enabled = True
    primary.save(update_fields=['fallback_enabled'])
    create_curation_policy(organization, session_team, project, task_type='generation')
    malformed_fallback = _payload(entry, comparisons=_manifest_comparisons(entries))
    malformed_fallback['comparisons'][0]['nested'] = {'forbidden': True}
    calls: list[object] = []

    class GatewayStub:
        def call(self, call_input: object) -> ProviderCallResult:
            calls.append(call_input)
            body = '{malformed' if len(calls) == 1 else json.dumps(malformed_fallback)
            return ProviderCallResult(
                provider=call_input.policy.provider,
                model=call_input.policy.model,
                call_record_id=uuid.uuid4(),
                redaction_state='clean',
                generated_title='',
                generated_body=body,
            )

    monkeypatch.setattr(module, 'get_provider_gateway', lambda *_args, **_kwargs: GatewayStub())

    with _unchanged(candidate.project_id), pytest.raises(ValueError) as error:
        module.JudgeCurationCandidate().execute(data)

    assert getattr(error.value, 'code', None) == 'judge_invalid_output'
    assert len(calls) == 2
    assert all(call.response_kind == 'curation_decision_v1' for call in calls)
    prompt = json.loads(calls[0].prompt)
    assert [item['memory_version_id'] for item in prompt['comparisons']] == [
        str(entry.memory_version_id) for entry in entries
    ]
    assert 'vector_distance' not in calls[0].prompt
    assert 'sk-' not in calls[0].prompt


@pytest.mark.django_db
def test_destructive_verdict_below_evidence_threshold_is_not_applied() -> None:
    module, data, entry, candidate = _fixture(comparison_complete=False)
    destructive = _payload(
        entry,
        outcome='supersede_memory',
        relation='candidate_supersedes',
        target=str(entry.memory_version_id),
        reason_code='ordered_replacement',
        temporal_order='candidate_newer',
        reason='destructive outcome lacks complete comparison',
    )

    with _unchanged(candidate.project_id), pytest.raises(ValueError) as error:
        module.parse_curation_judge_verdict(json.dumps(destructive), data)

    assert getattr(error.value, 'code', None) == 'judge_policy_denied'


@pytest.mark.django_db
def test_similarity_point_999_cannot_choose_destructive_outcome() -> None:
    module, data, entry, candidate = _fixture()
    ranked_entry = replace(entry, vector_distance=0.001)
    data = replace(
        data,
        shortlist=replace(data.shortlist, entries=(ranked_entry,)),
        evidence=replace(data.evidence, candidate=module.ClaimEvidence(tier='supported', refs=('candidate-ref',))),
    )
    destructive = _payload(
        ranked_entry,
        outcome='supersede_memory',
        relation='candidate_supersedes',
        target=str(ranked_entry.memory_version_id),
        candidate_refs=['candidate-ref'],
        reason_code='ordered_replacement',
        temporal_order='candidate_newer',
        reason='high similarity is retrieval evidence only',
    )

    with _unchanged(candidate.project_id), pytest.raises(ValueError) as error:
        module.parse_curation_judge_verdict(json.dumps(destructive), data)

    assert getattr(error.value, 'code', None) == 'judge_policy_denied'


@pytest.mark.django_db
def test_conflict_tag_blocks_revise_and_supersede_targets() -> None:
    module, data, entry, candidate = _fixture()
    conflicted = replace(entry, has_open_conflict=True)
    data = replace(data, shortlist=replace(data.shortlist, entries=(conflicted,)))
    for outcome, relation, reason_code in (
        ('revise_memory', 'candidate_revises', 'same_subject_revision'),
        ('supersede_memory', 'candidate_supersedes', 'ordered_replacement'),
    ):
        payload = _payload(
            conflicted,
            outcome=outcome,
            relation=relation,
            target=str(conflicted.memory_version_id),
            reason_code=reason_code,
            temporal_order='candidate_newer',
        )
        with _unchanged(candidate.project_id), pytest.raises(ValueError) as error:
            module.parse_curation_judge_verdict(json.dumps(payload), data)
        assert getattr(error.value, 'code', None) == 'judge_policy_denied'


def _conflict_payload(
    entry: object,
    *,
    candidate_refs: list[str] | None = None,
    target_refs: list[str] | None = None,
) -> dict[str, object]:
    return _payload(
        entry,
        outcome='open_conflict',
        relation='mutually_incompatible',
        target=str(entry.memory_version_id),
        candidate_refs=candidate_refs,
        target_refs=target_refs,
        reason_code='same_scope_contradiction',
        applicability='same',
        temporal_order='unordered',
        reason='same scope contradiction',
    )


def _conflict_data(
    module: object,
    data: object,
    entry: object,
    *,
    candidate_at: datetime | None,
    target_at: datetime | None,
    candidate_refs: tuple[str, ...] = ('candidate-ref',),
    target_refs: tuple[str, ...] = ('target-ref',),
    complete: bool = True,
) -> object:
    evidence = module.CurationEvidenceContext(
        candidate=module.ClaimEvidence(tier='supported', refs=candidate_refs, latest_evidence_at=candidate_at),
        targets={
            entry.memory_version_id: module.ClaimEvidence(
                tier='supported', refs=target_refs, latest_evidence_at=target_at
            )
        },
    )

    return replace(data, shortlist=replace(data.shortlist, comparison_complete=complete), evidence=evidence)


@pytest.mark.django_db
def test_open_conflict_opens_with_genuinely_unordered_evidence() -> None:
    module, data, entry, candidate = _fixture()
    data = _conflict_data(module, data, entry, candidate_at=_TARGET_EVIDENCE_AT, target_at=_TARGET_EVIDENCE_AT)
    payload = _conflict_payload(entry, candidate_refs=['candidate-ref'])

    with _unchanged(candidate.project_id):
        verdict = module.parse_curation_judge_verdict(json.dumps(payload), data)

    assert verdict.outcome == 'open_conflict'


@pytest.mark.django_db
def test_parse_judge_verdict_strips_markdown_json_fence() -> None:
    module, data, entry, candidate = _fixture()
    payload = _payload(entry)
    fenced = f'```json\n{json.dumps(payload)}\n```'

    with _unchanged(candidate.project_id):
        verdict = module.parse_curation_judge_verdict(fenced, data)

    assert verdict.outcome == 'publish_new'


@pytest.mark.django_db
def test_open_conflict_denied_when_comparison_incomplete() -> None:
    module, data, entry, candidate = _fixture()
    data = _conflict_data(
        module, data, entry, candidate_at=_TARGET_EVIDENCE_AT, target_at=_TARGET_EVIDENCE_AT, complete=False
    )
    payload = _conflict_payload(entry, candidate_refs=['candidate-ref'])

    with _unchanged(candidate.project_id), pytest.raises(ValueError) as error:
        module.parse_curation_judge_verdict(json.dumps(payload), data)

    assert getattr(error.value, 'code', None) == 'judge_policy_denied'


@pytest.mark.django_db
def test_open_conflict_denied_when_deterministic_precedence_exists() -> None:
    module, data, entry, candidate = _fixture()
    data = _conflict_data(module, data, entry, candidate_at=_CANDIDATE_EVIDENCE_AT, target_at=_TARGET_EVIDENCE_AT)
    payload = _conflict_payload(entry, candidate_refs=['candidate-ref'])

    with _unchanged(candidate.project_id), pytest.raises(ValueError) as error:
        module.parse_curation_judge_verdict(json.dumps(payload), data)

    assert getattr(error.value, 'code', None) == 'judge_policy_denied'


@pytest.mark.django_db
@pytest.mark.parametrize('side', ['candidate', 'target'])
def test_open_conflict_denied_when_evidence_refs_empty(side: str) -> None:
    module, data, entry, candidate = _fixture()
    candidate_refs = () if side == 'candidate' else ('candidate-ref',)
    target_refs = () if side == 'target' else ('target-ref',)
    data = _conflict_data(
        module,
        data,
        entry,
        candidate_at=_TARGET_EVIDENCE_AT,
        target_at=_TARGET_EVIDENCE_AT,
        candidate_refs=candidate_refs,
        target_refs=target_refs,
    )
    payload = _conflict_payload(entry, candidate_refs=list(candidate_refs), target_refs=list(target_refs))

    with _unchanged(candidate.project_id), pytest.raises(ValueError) as error:
        module.parse_curation_judge_verdict(json.dumps(payload), data)

    assert getattr(error.value, 'code', None) == 'judge_policy_denied'


@pytest.mark.django_db
@pytest.mark.parametrize(
    'mutator',
    [
        lambda payload: payload.update({'extra': 1}),
        lambda payload: payload.pop('reason'),
        lambda payload: payload['comparisons'][0].update({'extra': 1}),
        lambda payload: payload['comparisons'][0].pop('relation'),
        lambda payload: payload.update({'target_memory_version_id': 'not-a-uuid'}),
    ],
)
def test_judge_schema_rejects_recursive_extra_and_missing_keys(
    mutator: Callable[[dict[str, object]], object],
) -> None:
    module, data, entry, candidate = _fixture()
    payload = _payload(entry)
    if mutator is not None:
        mutator(payload)
    with _unchanged(candidate.project_id), pytest.raises(ValueError) as error:
        module.parse_curation_judge_verdict(json.dumps(payload), data)
    assert getattr(error.value, 'code', None) == 'judge_invalid_output'


@pytest.mark.django_db
@pytest.mark.parametrize(
    ('field', 'value'),
    [
        ('schema_version', True),
        ('schema_version', 1.0),
        ('outcome', 1),
        ('target_memory_version_id', 1),
        ('candidate_evidence_refs', [True]),
        ('comparisons', {}),
        ('applicability', False),
    ],
)
def test_judge_schema_rejects_bool_as_int_and_wrong_primitive_types(field: str, value: object) -> None:
    module, data, entry, candidate = _fixture()
    payload = _payload(entry)
    payload[field] = value
    with _unchanged(candidate.project_id), pytest.raises(ValueError) as error:
        module.parse_curation_judge_verdict(json.dumps(payload), data)
    assert getattr(error.value, 'code', None) == 'judge_invalid_output'


@pytest.mark.django_db
@pytest.mark.parametrize(
    ('outcome', 'relation', 'target', 'reason_code', 'applicability', 'temporal_order', 'candidate_tier', 'complete'),
    [
        ('publish_new', 'unrelated', None, 'distinct_claim', 'same', 'not_applicable', 'supported', True),
        ('publish_new', 'compatible_distinct', None, 'distinct_claim', 'same', 'not_applicable', 'supported', True),
        ('merge_evidence', 'equivalent', 'target', 'equivalent_claim', 'same', 'not_applicable', 'supported', True),
        (
            'revise_memory',
            'candidate_revises',
            'target',
            'same_subject_revision',
            'same',
            'candidate_newer',
            'corroborated',
            True,
        ),
        (
            'supersede_memory',
            'candidate_supersedes',
            'target',
            'ordered_replacement',
            'same',
            'candidate_newer',
            'corroborated',
            True,
        ),
        ('reject_candidate', 'redundant', 'target', 'redundant_claim', 'same', 'not_applicable', 'supported', True),
        ('reject_candidate', 'unsupported', None, 'unsupported_claim', 'same', 'not_applicable', 'none', True),
        (
            'open_conflict',
            'mutually_incompatible',
            'target',
            'same_scope_contradiction',
            'same',
            'unordered',
            'supported',
            True,
        ),
    ],
)
def test_judge_allows_only_locked_outcome_relation_target_combinations(
    outcome: str,
    relation: str,
    target: str | None,
    reason_code: str,
    applicability: str,
    temporal_order: str,
    candidate_tier: str,
    complete: bool,
) -> None:
    module, data, entry, candidate = _fixture()
    candidate_at = _TARGET_EVIDENCE_AT if outcome == 'open_conflict' else _CANDIDATE_EVIDENCE_AT
    data = replace(
        data,
        shortlist=replace(data.shortlist, comparison_complete=complete),
        evidence=replace(
            data.evidence,
            candidate=module.ClaimEvidence(
                tier=candidate_tier,
                refs=() if candidate_tier == 'none' else ('candidate-ref', 'candidate-ref-2'),
                latest_evidence_at=candidate_at,
            ),
        ),
    )
    target_id = str(entry.memory_version_id) if target else None
    payload = _payload(
        entry,
        outcome=outcome,
        relation=relation,
        target=target_id,
        candidate_refs=[] if candidate_tier == 'none' else None,
        reason_code=reason_code,
        applicability=applicability,
        temporal_order=temporal_order,
    )
    with _unchanged(candidate.project_id):
        verdict = module.parse_curation_judge_verdict(json.dumps(payload), data)
    assert verdict.outcome == outcome
    assert verdict.relation == relation


@pytest.mark.django_db
def test_judge_requires_comparisons_exactly_once_in_manifest_order() -> None:
    module, data, (first, second), candidate = _two_entry_fixture()
    comparisons = [
        {
            'memory_version_id': str(first.memory_version_id),
            'relation': 'unrelated',
            'target_evidence_refs': ['target-ref'],
        },
        {
            'memory_version_id': str(second.memory_version_id),
            'relation': 'unrelated',
            'target_evidence_refs': ['target-ref-2'],
        },
    ]
    payload = _payload(first, comparisons=comparisons)
    payload['comparisons'] = [comparisons[0]]
    with _unchanged(candidate.project_id), pytest.raises(ValueError):
        module.parse_curation_judge_verdict(json.dumps(payload), data)
    payload['comparisons'] = [comparisons[0], comparisons[0]]
    with _unchanged(candidate.project_id), pytest.raises(ValueError):
        module.parse_curation_judge_verdict(json.dumps(payload), data)
    payload['comparisons'] = list(reversed(comparisons))
    with _unchanged(candidate.project_id), pytest.raises(ValueError):
        module.parse_curation_judge_verdict(json.dumps(payload), data)


@pytest.mark.django_db
@pytest.mark.parametrize('refs', [['candidate-ref'] * 2, ['invented-ref'], [f'ref-{index}' for index in range(17)]])
def test_judge_evidence_refs_are_unique_manifest_tokens_and_capped_at_sixteen(refs: list[str]) -> None:
    module, data, entry, candidate = _fixture()
    payload = _payload(entry, candidate_refs=refs)
    with _unchanged(candidate.project_id), pytest.raises(ValueError) as error:
        module.parse_curation_judge_verdict(json.dumps(payload), data)
    expected_code = 'judge_reference_invalid' if refs == ['invented-ref'] else 'judge_invalid_output'
    assert getattr(error.value, 'code', None) == expected_code


@pytest.mark.django_db
@pytest.mark.parametrize('reason', ['', 'x' * 501, 'Use sk-abcdefghijklmnop in production'])
def test_judge_reason_is_bounded_and_redacted(reason: str) -> None:
    module, data, entry, candidate = _fixture()
    payload = _payload(entry, reason=reason)
    with _unchanged(candidate.project_id), pytest.raises(ValueError) as error:
        module.parse_curation_judge_verdict(json.dumps(payload), data)
    assert getattr(error.value, 'code', None) == 'judge_invalid_output'


def _empty_shortlist() -> object:
    shortlist_module = _shortlist_module()
    return shortlist_module.CurationShortlist(
        entries=(),
        manifest_hash='b' * 64,
        authorized_corpus_count=0,
        comparison_complete=True,
    )


def _shortlist_for(memory: object, version: object) -> object:
    shortlist_module = _shortlist_module()
    entry = shortlist_module.CurationShortlistEntry(
        memory_id=memory.id,
        memory_version_id=version.id,
        current_transition_id=memory.current_transition_id,
        visibility_scope=VisibilityScope.PROJECT,
        team_id=None,
        title=memory.title,
        body=version.body,
        kind=memory.kind or 'decision',
        body_hash='a' * 64,
        exact_overlap=0,
        vector_distance=0.1,
        lexical_rank=0.0,
        trigram_similarity=0.0,
        has_open_conflict=False,
    )
    return shortlist_module.CurationShortlist(
        entries=(entry,),
        manifest_hash='b' * 64,
        authorized_corpus_count=1,
        comparison_complete=True,
    )


def _add_distillation_source(
    candidate: MemoryCandidate,
    session: object,
    *,
    window: object,
    stage: object,
    sequence: int,
    title: str,
    body: str,
) -> MemoryCandidateSource:
    observation = Observation.objects.create(
        organization=candidate.organization,
        project=candidate.project,
        team=candidate.team,
        agent=session.agent,
        session=session,
        observation_type='tool_use',
        title=title,
        body=body,
        content_hash=f'evidence-content-{session.id}-{sequence}',
        session_sequence=sequence,
        source_metadata={'event_type': 'post_tool_use'},
    )
    anchors = candidate_source_anchors(
        observation,
        observation_id=str(observation.id),
        session_sequence=sequence,
        observation_digest=observation_content_digest(observation),
    )
    return MemoryCandidateSource.objects.create(
        organization=candidate.organization,
        project=candidate.project,
        team=candidate.team,
        candidate=candidate,
        window=window,
        observation=observation,
        stage=stage,
        anchors=anchors,
        anchors_hash=canonical_source_manifest(anchors),
    )


@pytest.mark.django_db
def test_repeated_window_group_counts_as_one_candidate_group() -> None:
    module = _judge_module()
    candidate, source, scope = provenanced_candidate('judge-evidence-repeat-window')
    _add_distillation_source(
        candidate,
        scope[2],
        window=source.window,
        stage=source.stage,
        sequence=2,
        title='Second observation same window',
        body='Second observation body in the same distillation window',
    )
    before = _semantic_snapshot(candidate.project_id)

    context = module.build_curation_evidence_context(candidate.id, _empty_shortlist())

    assert context.candidate.tier == 'supported'
    assert len(context.candidate.refs) == 1
    assert len(set(context.candidate.refs)) == len(context.candidate.refs)
    assert _semantic_snapshot(candidate.project_id) == before


@pytest.mark.django_db
def test_two_distinct_window_groups_reach_corroborated_candidate_tier() -> None:
    module = _judge_module()
    candidate, _source, scope = provenanced_candidate('judge-evidence-two-windows')
    _second_candidate, second_source, _second_session = provenanced_candidate_in_scope(
        scope[0],
        scope[1],
        scope[2].team,
        suffix='judge-evidence-second-window',
        title='Independent window candidate',
        body='Independent window body',
    )
    MemoryCandidateSource.objects.create(
        organization=candidate.organization,
        project=candidate.project,
        team=candidate.team,
        candidate=candidate,
        window=second_source.window,
        observation=second_source.observation,
        stage=second_source.stage,
        anchors=second_source.anchors,
        anchors_hash=second_source.anchors_hash,
    )
    before = _semantic_snapshot(candidate.project_id)

    context = module.build_curation_evidence_context(candidate.id, _empty_shortlist())

    assert context.candidate.tier == 'corroborated'
    assert len(context.candidate.refs) >= 2
    assert len(set(context.candidate.refs)) == len(context.candidate.refs)
    assert _semantic_snapshot(candidate.project_id) == before


@pytest.mark.django_db
def test_cumulative_windows_do_not_corroborate_the_same_observation() -> None:
    module = _judge_module()
    candidate, first_source, scope = provenanced_candidate('judge-evidence-cumulative-window')
    organization, project, session = scope
    original = first_source.observation
    first_window = first_source.window

    type(session).objects.filter(id=session.id).update(
        status=SessionStatus.ACTIVE,
        ended_at=None,
        end_work_contract_version=0,
        observation_sequence_cursor=2,
    )
    Observation.objects.create(
        organization=organization,
        project=project,
        team=session.team,
        agent=session.agent,
        session=session,
        observation_type='tool_use',
        title='Later unrelated observation',
        body='This later event does not independently support the candidate claim.',
        content_hash=f'evidence-content-{session.id}-2',
        session_sequence=2,
        source_metadata={'event_type': 'post_tool_use'},
    )
    ended = EndSession().execute(
        organization_id=organization.id,
        project_id=project.id,
        session_id=session.id,
        ended_at=timezone.now(),
        source='explicit',
    )
    second_work = WorkflowWork.objects.get(id=ended.work_id)
    second_window = materialize_distillation_window(second_work)
    second_stage, _primary_stage = _stage_history(scope, second_window)

    second_manifest_ids = {
        entry['observation_id']
        for chunk in second_window.chunks.order_by('ordinal')
        for entry in chunk.input_manifest['observations']
    }
    assert first_window.lower_sequence_exclusive == 0
    assert second_window.lower_sequence_exclusive == 0
    assert first_window.input_hash != second_window.input_hash
    assert str(original.id) in second_manifest_ids

    MemoryCandidateSource.objects.create(
        organization=organization,
        project=project,
        team=session.team,
        candidate=candidate,
        window=second_window,
        observation=original,
        stage=second_stage,
        anchors=first_source.anchors,
        anchors_hash=first_source.anchors_hash,
    )

    context = module.build_curation_evidence_context(candidate.id, _empty_shortlist())

    assert context.candidate.tier == 'supported'
    assert len(context.candidate.refs) == 1


@pytest.mark.django_db
def test_missing_target_provenance_raises_operational_retry_without_override() -> None:
    module = _judge_module()
    candidate, source, _scope = provenanced_candidate('judge-evidence-missing-target')
    promotable, _promotable_source = candidate_in_scope(
        candidate,
        source,
        title='Target memory for missing provenance',
        body='Target memory body for missing provenance',
    )
    result = transitions_module().PromoteMemoryCandidate().execute(transition_request(promotable))
    MemoryVersionSource.objects.filter(memory_version_id=result.memory_version.id).delete()
    shortlist = _shortlist_for(result.memory, result.memory_version)
    before = _semantic_snapshot(candidate.project_id)

    with _unchanged(candidate.project_id), pytest.raises(ValueError) as error:
        module.build_curation_evidence_context(candidate.id, shortlist)

    assert getattr(error.value, 'code', None) == 'transition_dependency_unavailable'
    assert _semantic_snapshot(candidate.project_id) == before


@pytest.mark.django_db
def test_corrupt_target_provenance_raises_operational_retry_without_downgrade() -> None:
    module = _judge_module()
    candidate, source, _scope = provenanced_candidate('judge-evidence-corrupt-target')
    promotable, _promotable_source = candidate_in_scope(
        candidate,
        source,
        title='Target memory for corrupt provenance',
        body='Target memory body for corrupt provenance',
    )
    result = transitions_module().PromoteMemoryCandidate().execute(transition_request(promotable))
    MemoryCandidateSource.objects.filter(candidate_id=promotable.id).update(anchors_hash='f' * 64)
    shortlist = _shortlist_for(result.memory, result.memory_version)
    before = _semantic_snapshot(candidate.project_id)

    with _unchanged(candidate.project_id), pytest.raises(ValueError) as error:
        module.build_curation_evidence_context(candidate.id, shortlist)

    assert getattr(error.value, 'code', None) == 'transition_dependency_unavailable'
    assert _semantic_snapshot(candidate.project_id) == before


@pytest.mark.django_db
def test_targetless_outcome_rejects_identity_relation_comparison() -> None:
    module, data, entry, candidate = _fixture()
    payload = _payload(
        entry,
        outcome='publish_new',
        relation='unrelated',
        target=None,
        comparisons=[
            {
                'memory_version_id': str(entry.memory_version_id),
                'relation': 'equivalent',
                'target_evidence_refs': ['target-ref'],
            }
        ],
    )

    with _unchanged(candidate.project_id), pytest.raises(ValueError) as error:
        module.parse_curation_judge_verdict(json.dumps(payload), data)

    assert getattr(error.value, 'code', None) == 'judge_invalid_output'


@pytest.mark.django_db
def test_supersede_requires_deterministic_precedence_not_provider_claim() -> None:
    module, data, entry, candidate = _fixture()
    data = replace(
        data,
        evidence=replace(
            data.evidence,
            candidate=replace(
                data.evidence.candidate,
                latest_evidence_at=datetime(2025, 1, 1, tzinfo=UTC),
            ),
        ),
    )
    payload = _payload(
        entry,
        outcome='supersede_memory',
        relation='candidate_supersedes',
        target=str(entry.memory_version_id),
        reason_code='ordered_replacement',
        applicability='same',
        temporal_order='candidate_newer',
        reason='provider asserts precedence the evidence does not support',
    )

    with _unchanged(candidate.project_id), pytest.raises(ValueError) as error:
        module.parse_curation_judge_verdict(json.dumps(payload), data)

    assert getattr(error.value, 'code', None) == 'judge_policy_denied'


@pytest.mark.django_db
def test_supersede_requires_same_applicability() -> None:
    module, data, entry, candidate = _fixture()
    payload = _payload(
        entry,
        outcome='supersede_memory',
        relation='candidate_supersedes',
        target=str(entry.memory_version_id),
        reason_code='ordered_replacement',
        applicability='different',
        temporal_order='candidate_newer',
        reason='supersession across a different applicability is not authorized',
    )

    with _unchanged(candidate.project_id), pytest.raises(ValueError) as error:
        module.parse_curation_judge_verdict(json.dumps(payload), data)

    assert getattr(error.value, 'code', None) == 'judge_policy_denied'


@pytest.mark.django_db
def test_judge_prompt_binds_comparison_manifest_and_completeness() -> None:
    module, data, _entry, _candidate = _fixture()

    prompt = json.loads(module.build_curation_judge_prompt(data))

    assert prompt.get('comparison_manifest_hash') == data.shortlist.manifest_hash
    assert prompt.get('authorized_corpus_count') == data.shortlist.authorized_corpus_count
    assert prompt.get('comparison_complete') == data.shortlist.comparison_complete


@pytest.mark.django_db
def test_judge_prompt_bounds_oversized_claim_snapshots() -> None:
    module, data, _entry, _candidate = _fixture()
    data = replace(data, candidate=replace(data.candidate, body='x' * 9000))

    prompt = module.build_curation_judge_prompt(data)
    envelope = json.loads(prompt)

    assert len(envelope['candidate']['claim']['body']) < 9000
    assert '[truncated' in envelope['candidate']['claim']['body']


@pytest.mark.django_db
def test_judge_result_carries_comparison_manifest_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    module, data, entry, candidate = _fixture()
    candidate.refresh_from_db()
    organization, project = candidate.organization, candidate.project
    session_team = candidate.team
    create_curation_policy(organization, session_team, project, task_type='curation')
    verdict_body = json.dumps(_payload(entry))

    class GatewayStub:
        def call(self, call_input: object) -> ProviderCallResult:
            return ProviderCallResult(
                provider=call_input.policy.provider,
                model=call_input.policy.model,
                call_record_id=uuid.uuid4(),
                redaction_state='clean',
                generated_title='',
                generated_body=verdict_body,
            )

    monkeypatch.setattr(module, 'get_provider_gateway', lambda *_args, **_kwargs: GatewayStub())

    result = module.JudgeCurationCandidate().execute(data)

    assert result.comparison_manifest_hash == data.shortlist.manifest_hash
    assert result.authorized_corpus_count == data.shortlist.authorized_corpus_count
    assert result.comparison_complete is data.shortlist.comparison_complete


@pytest.mark.django_db
def test_cyclic_target_provenance_is_detected_without_infinite_recursion() -> None:
    module = _judge_module()
    candidate, source, _scope = provenanced_candidate('judge-evidence-cyclic-target')
    first_candidate, _first_source = candidate_in_scope(
        candidate,
        source,
        title='First cyclic memory',
        body='First cyclic memory body',
    )
    second_candidate, _second_source = candidate_in_scope(
        candidate,
        source,
        title='Second cyclic memory',
        body='Second cyclic memory body',
    )
    first = transitions_module().PromoteMemoryCandidate().execute(transition_request(first_candidate))
    second = transitions_module().PromoteMemoryCandidate().execute(transition_request(second_candidate))
    cycle_hash = 'c' * 64
    MemoryVersionSource.objects.create(
        organization=first.memory.organization,
        project=first.memory.project,
        team=first.memory.team,
        memory_version=first.memory_version,
        source_memory_version=second.memory_version,
        source_content_hash=cycle_hash,
    )
    MemoryVersionSource.objects.create(
        organization=second.memory.organization,
        project=second.memory.project,
        team=second.memory.team,
        memory_version=second.memory_version,
        source_memory_version=first.memory_version,
        source_content_hash=cycle_hash,
    )
    shortlist = _shortlist_for(first.memory, first.memory_version)
    before = _semantic_snapshot(candidate.project_id)

    with _unchanged(candidate.project_id), pytest.raises(ValueError) as error:
        module.build_curation_evidence_context(candidate.id, shortlist)

    assert getattr(error.value, 'code', None) == 'transition_dependency_unavailable'
    assert _semantic_snapshot(candidate.project_id) == before
