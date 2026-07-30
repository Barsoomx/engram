from __future__ import annotations

import itertools
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from engram.memory import curation_judge
from engram.memory.curation_derivation import (
    DerivationFacts,
    DerivedDecision,
    TargetFacts,
    build_derivation_facts,
    derive_decision,
    feasible_outcomes,
)
from engram.memory.curation_judge import (
    ClaimEvidence,
    CurationEvidenceContext,
    CurationJudgeInput,
    CurationJudgeVerdict,
)
from engram.memory.curation_shortlist import CurationShortlist, CurationShortlistEntry
from engram.memory.deterministic_gates import EffectiveCandidateScope, SanitizedCandidateView

_RELATIONS = (
    'unrelated',
    'compatible_distinct',
    'equivalent',
    'candidate_revises',
    'candidate_supersedes',
    'redundant',
    'unsupported',
    'mutually_incompatible',
)
_TIERS = ('none', 'supported', 'corroborated')
_APPLICABILITIES = ('same', 'different')
_MUTATIONS = frozenset({'merge_evidence', 'revise_memory', 'supersede_memory', 'open_conflict'})
_CONFLICT_BLOCKED = frozenset({'merge_evidence', 'revise_memory', 'supersede_memory'})
_SUPPORTED = frozenset({'supported', 'corroborated'})

_EARLIER = datetime(2026, 1, 1, tzinfo=UTC)
_EQUAL = datetime(2026, 2, 1, tzinfo=UTC)
_LATER = datetime(2026, 3, 1, tzinfo=UTC)
_TARGET_MOMENTS = (_EARLIER, _EQUAL, _LATER, None)
_CANDIDATE_MOMENTS = (_EQUAL, None)
_OTHER_TEAM = uuid.UUID('11111111-1111-4111-8111-111111111111')


@dataclass(frozen=True, slots=True)
class _TargetSpec:
    tier: str
    same_visibility: bool
    has_open_conflict: bool
    target_at: datetime | None
    has_refs: bool = True


def _target_id(index: int) -> uuid.UUID:
    return uuid.UUID(f'00000000-0000-4000-8000-{index:012d}')


def _refs(prefix: str, tier: str, has_refs: bool) -> tuple[str, ...]:
    if not has_refs or tier == 'none':
        return ()
    if tier == 'supported':
        return (f'{prefix}-1',)

    return (f'{prefix}-1', f'{prefix}-2')


def _entry(index: int, spec: _TargetSpec) -> CurationShortlistEntry:
    return CurationShortlistEntry(
        memory_id=uuid.uuid4(),
        memory_version_id=_target_id(index),
        current_transition_id=uuid.uuid4(),
        visibility_scope='project' if spec.same_visibility else 'team',
        team_id=None if spec.same_visibility else _OTHER_TEAM,
        title=f'target {index}',
        body=f'target body {index}',
        kind='decision',
        body_hash='a' * 64,
        exact_overlap=0,
        vector_distance=0.1,
        lexical_rank=0.0,
        trigram_similarity=0.0,
        has_open_conflict=spec.has_open_conflict,
    )


def _judge_input(
    *,
    candidate_tier: str,
    candidate_at: datetime | None,
    complete: bool,
    specs: tuple[_TargetSpec, ...],
    candidate_has_refs: bool = True,
) -> CurationJudgeInput:
    entries = tuple(_entry(index, spec) for index, spec in enumerate(specs))
    targets = {
        entry.memory_version_id: ClaimEvidence(
            tier=spec.tier,
            refs=_refs(f'target-{index}-ref', spec.tier, spec.has_refs),
            latest_evidence_at=spec.target_at,
        )
        for index, (entry, spec) in enumerate(zip(entries, specs, strict=True))
    }

    return CurationJudgeInput(
        organization_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        candidate_id=uuid.uuid4(),
        candidate=SanitizedCandidateView(
            title='candidate title',
            body='candidate body',
            kind='decision',
            evidence=(),
            content_hash='c' * 64,
            redaction_codes=(),
        ),
        effective_scope=EffectiveCandidateScope('project', None),
        shortlist=CurationShortlist(
            entries=entries,
            manifest_hash='b' * 64,
            authorized_corpus_count=len(entries),
            comparison_complete=complete,
        ),
        evidence=CurationEvidenceContext(
            candidate=ClaimEvidence(
                tier=candidate_tier,
                refs=_refs('candidate-ref', candidate_tier, candidate_has_refs),
                latest_evidence_at=candidate_at,
            ),
            targets=targets,
        ),
        request_id='derivation-request',
        trace_id='derivation-trace',
    )


def _relations(data: CurationJudgeInput, assignment: tuple[str, ...]) -> dict[uuid.UUID, str]:
    return {
        entry.memory_version_id: relation for entry, relation in zip(data.shortlist.entries, assignment, strict=True)
    }


def _applicability(data: CurationJudgeInput, assignment: tuple[str, ...]) -> dict[uuid.UUID, str]:
    return {entry.memory_version_id: value for entry, value in zip(data.shortlist.entries, assignment, strict=True)}


def _verdict(decision: DerivedDecision, data: CurationJudgeInput) -> CurationJudgeVerdict:
    return CurationJudgeVerdict(
        schema_version=2,
        outcome=decision.outcome,
        relation=decision.relation,
        target_memory_version_id=decision.target_memory_version_id,
        comparisons=(),
        applicability=decision.applicability,
        temporal_order=decision.temporal_order,
        reason_code=decision.reason_code,
        reason='derived decision',
    )


def _single_target_lattice() -> list[tuple[CurationJudgeInput, tuple[str, ...], tuple[str, ...]]]:
    rows: list[tuple[CurationJudgeInput, tuple[str, ...], tuple[str, ...]]] = []
    for relation, candidate_tier, target_tier, complete, conflict, same_visibility in itertools.product(
        _RELATIONS, _TIERS, _TIERS, (True, False), (True, False), (True, False)
    ):
        for candidate_at, target_at in itertools.product(_CANDIDATE_MOMENTS, _TARGET_MOMENTS):
            data = _judge_input(
                candidate_tier=candidate_tier,
                candidate_at=candidate_at,
                complete=complete,
                specs=(
                    _TargetSpec(
                        tier=target_tier,
                        same_visibility=same_visibility,
                        has_open_conflict=conflict,
                        target_at=target_at,
                    ),
                ),
            )
            for applicability in _APPLICABILITIES:
                rows.append((data, (relation,), (applicability,)))

    return rows


_PAIR_SPECS = (
    _TargetSpec(tier='supported', same_visibility=True, has_open_conflict=False, target_at=_EARLIER),
    _TargetSpec(tier='corroborated', same_visibility=True, has_open_conflict=True, target_at=_EQUAL),
    _TargetSpec(tier='none', same_visibility=True, has_open_conflict=False, target_at=_LATER),
    _TargetSpec(tier='supported', same_visibility=False, has_open_conflict=False, target_at=None),
)


def _two_target_lattice() -> list[tuple[CurationJudgeInput, tuple[str, ...], tuple[str, ...]]]:
    rows: list[tuple[CurationJudgeInput, tuple[str, ...], tuple[str, ...]]] = []
    for first_spec, second_spec, candidate_tier, complete in itertools.product(
        _PAIR_SPECS, _PAIR_SPECS, _TIERS, (True, False)
    ):
        data = _judge_input(
            candidate_tier=candidate_tier,
            candidate_at=_EQUAL,
            complete=complete,
            specs=(first_spec, second_spec),
        )
        for assignment in itertools.product(_RELATIONS, _RELATIONS):
            for applicability in itertools.product(_APPLICABILITIES, _APPLICABILITIES):
                rows.append((data, assignment, applicability))

    return rows


def _empty_refs_lattice() -> list[tuple[CurationJudgeInput, tuple[str, ...], tuple[str, ...]]]:
    rows: list[tuple[CurationJudgeInput, tuple[str, ...], tuple[str, ...]]] = []
    for relation, complete, candidate_has_refs, target_has_refs, target_at in itertools.product(
        _RELATIONS, (True, False), (True, False), (True, False), _TARGET_MOMENTS
    ):
        data = _judge_input(
            candidate_tier='supported',
            candidate_at=_EQUAL,
            complete=complete,
            candidate_has_refs=candidate_has_refs,
            specs=(
                _TargetSpec(
                    tier='supported',
                    same_visibility=True,
                    has_open_conflict=False,
                    target_at=target_at,
                    has_refs=target_has_refs,
                ),
            ),
        )
        for applicability in _APPLICABILITIES:
            rows.append((data, (relation,), (applicability,)))

    return rows


@pytest.fixture(scope='module')
def f_lattice() -> list[tuple[CurationJudgeInput, tuple[str, ...], tuple[str, ...]]]:
    return _single_target_lattice() + _two_target_lattice() + _empty_refs_lattice()


def _target_facts(facts: DerivationFacts, target_id: uuid.UUID | None) -> TargetFacts | None:
    return next((item for item in facts.targets if item.memory_version_id == target_id), None)


def test_derivation_never_raises_over_the_lattice(
    f_lattice: list[tuple[CurationJudgeInput, tuple[str, ...], tuple[str, ...]]],
) -> None:
    for data, assignment, applicability in f_lattice:
        facts = build_derivation_facts(data)
        derive_decision(facts, _relations(data, assignment), _applicability(data, applicability))


def test_every_derived_decision_passes_the_evidence_policy(
    f_lattice: list[tuple[CurationJudgeInput, tuple[str, ...], tuple[str, ...]]],
) -> None:
    for data, assignment, applicability in f_lattice:
        facts = build_derivation_facts(data)
        decision = derive_decision(facts, _relations(data, assignment), _applicability(data, applicability))
        if decision is None:
            continue
        curation_judge._apply_evidence_policy(_verdict(decision, data), data)


def test_mutation_outcomes_never_target_cross_visibility_entries(
    f_lattice: list[tuple[CurationJudgeInput, tuple[str, ...], tuple[str, ...]]],
) -> None:
    for data, assignment, applicability in f_lattice:
        facts = build_derivation_facts(data)
        decision = derive_decision(facts, _relations(data, assignment), _applicability(data, applicability))
        if decision is None or decision.outcome not in _MUTATIONS:
            continue
        target = _target_facts(facts, decision.target_memory_version_id)
        assert target is not None
        assert target.same_visibility


def test_merge_revise_supersede_never_touch_a_conflicted_target(
    f_lattice: list[tuple[CurationJudgeInput, tuple[str, ...], tuple[str, ...]]],
) -> None:
    for data, assignment, applicability in f_lattice:
        facts = build_derivation_facts(data)
        decision = derive_decision(facts, _relations(data, assignment), _applicability(data, applicability))
        if decision is None or decision.outcome not in _CONFLICT_BLOCKED:
            continue
        target = _target_facts(facts, decision.target_memory_version_id)
        assert target is not None
        assert not target.has_open_conflict


def test_mutation_outcomes_require_both_tiers_in_the_supported_set(
    f_lattice: list[tuple[CurationJudgeInput, tuple[str, ...], tuple[str, ...]]],
) -> None:
    for data, assignment, applicability in f_lattice:
        facts = build_derivation_facts(data)
        decision = derive_decision(facts, _relations(data, assignment), _applicability(data, applicability))
        if decision is None or decision.outcome not in _MUTATIONS:
            continue
        target = _target_facts(facts, decision.target_memory_version_id)
        assert facts.candidate_tier in _SUPPORTED
        assert target is not None
        assert target.tier in _SUPPORTED


def test_complete_comparison_always_yields_a_decision(
    f_lattice: list[tuple[CurationJudgeInput, tuple[str, ...], tuple[str, ...]]],
) -> None:
    for data, assignment, applicability in f_lattice:
        facts = build_derivation_facts(data)
        if not facts.comparison_complete:
            continue
        assert derive_decision(facts, _relations(data, assignment), _applicability(data, applicability)) is not None


def test_no_decision_implies_incomplete_comparison(
    f_lattice: list[tuple[CurationJudgeInput, tuple[str, ...], tuple[str, ...]]],
) -> None:
    for data, assignment, applicability in f_lattice:
        facts = build_derivation_facts(data)
        if derive_decision(facts, _relations(data, assignment), _applicability(data, applicability)) is None:
            assert facts.comparison_complete is False


def test_derivation_is_deterministic(
    f_lattice: list[tuple[CurationJudgeInput, tuple[str, ...], tuple[str, ...]]],
) -> None:
    for data, assignment, applicability in f_lattice:
        facts = build_derivation_facts(data)
        relations = _relations(data, assignment)
        values = _applicability(data, applicability)
        assert derive_decision(facts, relations, values) == derive_decision(facts, relations, values)


def test_empty_feasible_set_forbids_every_relation_assignment(
    f_lattice: list[tuple[CurationJudgeInput, tuple[str, ...], tuple[str, ...]]],
) -> None:
    checked = 0
    seen: set[int] = set()
    for data, _assignment, _applicability in f_lattice:
        if id(data) in seen:
            continue
        seen.add(id(data))
        facts = build_derivation_facts(data)
        if feasible_outcomes(facts):
            continue
        checked += 1
        for assignment in itertools.product(_RELATIONS, repeat=len(facts.targets)):
            for applicability in _APPLICABILITIES:
                assert derive_decision(facts, _relations(data, assignment), _applicability(data, applicability)) is None

    assert checked > 0


def test_ties_within_a_rung_resolve_to_shortlist_order() -> None:
    spec = _TargetSpec(tier='supported', same_visibility=True, has_open_conflict=False, target_at=_EQUAL)
    data = _judge_input(candidate_tier='supported', candidate_at=_EQUAL, complete=True, specs=(spec, spec))
    facts = build_derivation_facts(data)

    decision = derive_decision(
        facts, _relations(data, ('equivalent', 'equivalent')), _applicability(data, ('same', 'same'))
    )

    assert decision is not None
    assert decision.outcome == 'merge_evidence'
    assert decision.target_memory_version_id == data.shortlist.entries[0].memory_version_id


def test_open_conflict_outranks_a_merge_on_another_target() -> None:
    spec = _TargetSpec(tier='supported', same_visibility=True, has_open_conflict=False, target_at=_EQUAL)
    data = _judge_input(candidate_tier='supported', candidate_at=_EQUAL, complete=True, specs=(spec, spec))
    facts = build_derivation_facts(data)

    decision = derive_decision(
        facts, _relations(data, ('equivalent', 'mutually_incompatible')), _applicability(data, ('same', 'same'))
    )

    assert decision is not None
    assert decision.outcome == 'open_conflict'
    assert decision.target_memory_version_id == data.shortlist.entries[1].memory_version_id
    assert decision.rung == 1
    assert (data.shortlist.entries[0].memory_version_id, 'equivalent') in decision.suppressed_identity_relations


def test_supersede_outranks_revise_on_another_target() -> None:
    spec = _TargetSpec(tier='supported', same_visibility=True, has_open_conflict=False, target_at=_EARLIER)
    data = _judge_input(candidate_tier='corroborated', candidate_at=_EQUAL, complete=True, specs=(spec, spec))
    facts = build_derivation_facts(data)

    decision = derive_decision(
        facts, _relations(data, ('candidate_revises', 'candidate_supersedes')), _applicability(data, ('same', 'same'))
    )

    assert decision is not None
    assert decision.outcome == 'supersede_memory'
    assert decision.rung == 2
    assert decision.temporal_order == 'candidate_newer'
    assert decision.reason_code == 'ordered_replacement'


def test_revise_fires_when_no_supersede_relation_is_asserted() -> None:
    spec = _TargetSpec(tier='supported', same_visibility=True, has_open_conflict=False, target_at=_EARLIER)
    data = _judge_input(candidate_tier='corroborated', candidate_at=_EQUAL, complete=True, specs=(spec,))
    facts = build_derivation_facts(data)

    decision = derive_decision(facts, _relations(data, ('candidate_revises',)), _applicability(data, ('same',)))

    assert decision is not None
    assert decision.outcome == 'revise_memory'
    assert decision.rung == 3
    assert decision.relation == 'candidate_revises'
    assert decision.reason_code == 'same_subject_revision'


def test_redundant_target_outranks_publish_new() -> None:
    spec = _TargetSpec(tier='supported', same_visibility=False, has_open_conflict=False, target_at=_EQUAL)
    data = _judge_input(candidate_tier='supported', candidate_at=_EQUAL, complete=True, specs=(spec,))
    facts = build_derivation_facts(data)

    decision = derive_decision(facts, _relations(data, ('redundant',)), _applicability(data, ('same',)))

    assert decision is not None
    assert decision.outcome == 'reject_candidate'
    assert decision.relation == 'redundant'
    assert decision.reason_code == 'redundant_claim'
    assert decision.rung == 5
    assert decision.target_memory_version_id == data.shortlist.entries[0].memory_version_id


def test_redundant_target_below_tier_falls_through_to_publish_new() -> None:
    spec = _TargetSpec(tier='none', same_visibility=True, has_open_conflict=False, target_at=_EQUAL)
    data = _judge_input(candidate_tier='supported', candidate_at=_EQUAL, complete=True, specs=(spec,))
    facts = build_derivation_facts(data)

    decision = derive_decision(facts, _relations(data, ('redundant',)), _applicability(data, ('same',)))

    assert decision is not None
    assert decision.outcome == 'publish_new'
    assert decision.rung == 6
    assert decision.relation == 'compatible_distinct'
    assert decision.target_memory_version_id is None
    assert decision.suppressed_identity_relations == ((data.shortlist.entries[0].memory_version_id, 'redundant'),)


def test_publish_new_relation_is_unrelated_only_when_every_comparison_is_unrelated() -> None:
    spec = _TargetSpec(tier='none', same_visibility=True, has_open_conflict=False, target_at=None)
    data = _judge_input(candidate_tier='supported', candidate_at=_EQUAL, complete=True, specs=(spec,))
    facts = build_derivation_facts(data)

    applicability = _applicability(data, ('same',))

    assert derive_decision(facts, _relations(data, ('unrelated',)), applicability).relation == 'unrelated'
    distinct = derive_decision(facts, _relations(data, ('compatible_distinct',)), applicability)
    assert distinct.relation == 'compatible_distinct'


def test_empty_shortlist_publish_new_is_unrelated_and_not_applicable() -> None:
    data = _judge_input(candidate_tier='supported', candidate_at=_EQUAL, complete=True, specs=())
    facts = build_derivation_facts(data)

    decision = derive_decision(facts, {}, {})

    assert decision is not None
    assert decision.outcome == 'publish_new'
    assert decision.relation == 'unrelated'
    assert decision.temporal_order == 'not_applicable'
    assert decision.reason_code == 'distinct_claim'


def test_zero_evidence_candidate_claiming_supersession_is_rejected_as_unsupported() -> None:
    spec = _TargetSpec(tier='supported', same_visibility=True, has_open_conflict=False, target_at=_EARLIER)
    data = _judge_input(candidate_tier='none', candidate_at=_LATER, complete=True, specs=(spec,))
    facts = build_derivation_facts(data)

    decision = derive_decision(facts, _relations(data, ('candidate_supersedes',)), _applicability(data, ('same',)))

    assert decision is not None
    assert decision.outcome == 'reject_candidate'
    assert decision.relation == 'unsupported'
    assert decision.reason_code == 'unsupported_claim'
    assert decision.rung == 7
    assert decision.target_memory_version_id is None
    assert decision.temporal_order == 'not_applicable'


def test_open_conflict_is_skipped_when_comparison_is_incomplete() -> None:
    spec = _TargetSpec(tier='supported', same_visibility=True, has_open_conflict=False, target_at=_EQUAL)
    data = _judge_input(candidate_tier='supported', candidate_at=_EQUAL, complete=False, specs=(spec,))
    facts = build_derivation_facts(data)

    assert derive_decision(facts, _relations(data, ('mutually_incompatible',)), _applicability(data, ('same',))) is None


def test_open_conflict_is_skipped_when_precedence_is_deterministic() -> None:
    spec = _TargetSpec(tier='supported', same_visibility=True, has_open_conflict=False, target_at=_EARLIER)
    data = _judge_input(candidate_tier='supported', candidate_at=_EQUAL, complete=True, specs=(spec,))
    facts = build_derivation_facts(data)

    decision = derive_decision(facts, _relations(data, ('mutually_incompatible',)), _applicability(data, ('same',)))

    assert decision is not None
    assert decision.outcome == 'publish_new'


def test_open_conflict_is_skipped_when_either_side_has_no_refs() -> None:
    spec = _TargetSpec(
        tier='supported', same_visibility=True, has_open_conflict=False, target_at=_EQUAL, has_refs=False
    )
    data = _judge_input(candidate_tier='supported', candidate_at=_EQUAL, complete=True, specs=(spec,))
    facts = build_derivation_facts(data)

    decision = derive_decision(facts, _relations(data, ('mutually_incompatible',)), _applicability(data, ('same',)))

    assert decision is not None
    assert decision.outcome == 'publish_new'


def test_mutation_rungs_require_same_applicability() -> None:
    spec = _TargetSpec(tier='supported', same_visibility=True, has_open_conflict=False, target_at=_EARLIER)
    data = _judge_input(candidate_tier='corroborated', candidate_at=_EQUAL, complete=True, specs=(spec,))
    facts = build_derivation_facts(data)

    for relation in ('equivalent', 'candidate_revises', 'candidate_supersedes'):
        decision = derive_decision(facts, _relations(data, (relation,)), _applicability(data, ('different',)))
        assert decision is not None
        assert decision.outcome == 'publish_new'


def test_redundant_rung_ignores_applicability() -> None:
    spec = _TargetSpec(tier='supported', same_visibility=True, has_open_conflict=False, target_at=_EQUAL)
    data = _judge_input(candidate_tier='supported', candidate_at=_EQUAL, complete=True, specs=(spec,))
    facts = build_derivation_facts(data)

    decision = derive_decision(facts, _relations(data, ('redundant',)), _applicability(data, ('different',)))

    assert decision is not None
    assert decision.outcome == 'reject_candidate'
    assert decision.relation == 'redundant'


def test_temporal_order_reports_target_newer() -> None:
    spec = _TargetSpec(tier='supported', same_visibility=True, has_open_conflict=False, target_at=_LATER)
    data = _judge_input(candidate_tier='supported', candidate_at=_EQUAL, complete=True, specs=(spec,))
    facts = build_derivation_facts(data)

    decision = derive_decision(facts, _relations(data, ('equivalent',)), _applicability(data, ('same',)))

    assert decision is not None
    assert decision.outcome == 'merge_evidence'
    assert decision.temporal_order == 'target_newer'


def test_feasible_outcomes_is_empty_only_when_incomplete_and_no_target_rung_is_open() -> None:
    spec = _TargetSpec(tier='none', same_visibility=True, has_open_conflict=False, target_at=_EQUAL)
    data = _judge_input(candidate_tier='supported', candidate_at=_EQUAL, complete=False, specs=(spec,))

    assert feasible_outcomes(build_derivation_facts(data)) == frozenset()


def test_feasible_outcomes_lists_every_relation_reachable_outcome() -> None:
    spec = _TargetSpec(tier='supported', same_visibility=True, has_open_conflict=False, target_at=_EARLIER)
    data = _judge_input(candidate_tier='corroborated', candidate_at=_EQUAL, complete=True, specs=(spec,))

    assert feasible_outcomes(build_derivation_facts(data)) == frozenset(
        {'publish_new', 'merge_evidence', 'revise_memory', 'supersede_memory', 'reject_candidate'}
    )


def test_feasible_outcomes_is_never_empty_for_a_zero_evidence_candidate() -> None:
    spec = _TargetSpec(tier='none', same_visibility=True, has_open_conflict=False, target_at=None)
    data = _judge_input(candidate_tier='none', candidate_at=None, complete=False, specs=(spec,))

    assert feasible_outcomes(build_derivation_facts(data)) == frozenset({'reject_candidate'})


def test_build_derivation_facts_mirrors_the_judge_precedence_helpers() -> None:
    for target_at in _TARGET_MOMENTS:
        for candidate_at in _CANDIDATE_MOMENTS:
            spec = _TargetSpec(tier='supported', same_visibility=True, has_open_conflict=False, target_at=target_at)
            data = _judge_input(candidate_tier='supported', candidate_at=candidate_at, complete=True, specs=(spec,))
            facts = build_derivation_facts(data)
            target_id = data.shortlist.entries[0].memory_version_id

            assert facts.targets[0].candidate_precedes == curation_judge._candidate_precedes(data, target_id)
            assert facts.targets[0].deterministic_precedence == curation_judge._deterministic_precedence(
                data, target_id
            )


def test_facts_ignore_shortlist_entries_without_evidence() -> None:
    spec = _TargetSpec(tier='supported', same_visibility=True, has_open_conflict=False, target_at=_EQUAL)
    data = _judge_input(candidate_tier='supported', candidate_at=_EQUAL, complete=True, specs=(spec,))
    stripped = CurationEvidenceContext(candidate=data.evidence.candidate, targets={})
    facts = build_derivation_facts(
        CurationJudgeInput(
            organization_id=data.organization_id,
            project_id=data.project_id,
            candidate_id=data.candidate_id,
            candidate=data.candidate,
            effective_scope=data.effective_scope,
            shortlist=data.shortlist,
            evidence=stripped,
            request_id=data.request_id,
            trace_id=data.trace_id,
        )
    )

    assert facts.targets[0].tier == 'none'
    assert facts.targets[0].has_refs is False
    assert facts.targets[0].candidate_precedes is False


def test_applicability_is_evaluated_per_target() -> None:
    spec = _TargetSpec(tier='supported', same_visibility=True, has_open_conflict=False, target_at=_EQUAL)
    data = _judge_input(candidate_tier='supported', candidate_at=_EQUAL, complete=True, specs=(spec, spec))
    facts = build_derivation_facts(data)
    first, second = (entry.memory_version_id for entry in data.shortlist.entries)

    decision = derive_decision(
        facts,
        _relations(data, ('equivalent', 'equivalent')),
        _applicability(data, ('different', 'same')),
    )

    assert decision is not None
    assert decision.outcome == 'merge_evidence'
    assert decision.target_memory_version_id == second
    assert decision.applicability == 'same'
    assert decision.target_memory_version_id != first


def test_targetless_decision_reports_not_applicable_applicability() -> None:
    spec = _TargetSpec(tier='supported', same_visibility=True, has_open_conflict=False, target_at=_EQUAL)
    data = _judge_input(candidate_tier='supported', candidate_at=_EQUAL, complete=True, specs=(spec,))
    facts = build_derivation_facts(data)

    decision = derive_decision(facts, _relations(data, ('unrelated',)), _applicability(data, ('same',)))

    assert decision is not None
    assert decision.outcome == 'publish_new'
    assert decision.applicability == 'not_applicable'
