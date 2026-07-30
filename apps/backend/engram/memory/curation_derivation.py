from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from engram.memory.curation_judge import CurationJudgeInput

_SUPPORTED_TIERS = frozenset({'supported', 'corroborated'})
_IDENTITY_RELATIONS = frozenset(
    {'equivalent', 'candidate_revises', 'candidate_supersedes', 'redundant', 'mutually_incompatible'}
)
_REASON_CODES = {
    ('publish_new', 'unrelated'): 'distinct_claim',
    ('publish_new', 'compatible_distinct'): 'distinct_claim',
    ('merge_evidence', 'equivalent'): 'equivalent_claim',
    ('revise_memory', 'candidate_revises'): 'same_subject_revision',
    ('supersede_memory', 'candidate_supersedes'): 'ordered_replacement',
    ('reject_candidate', 'redundant'): 'redundant_claim',
    ('reject_candidate', 'unsupported'): 'unsupported_claim',
    ('open_conflict', 'mutually_incompatible'): 'same_scope_contradiction',
}


@dataclass(frozen=True, slots=True)
class TargetFacts:
    memory_version_id: UUID
    tier: str
    has_refs: bool
    same_visibility: bool
    has_open_conflict: bool
    candidate_precedes: bool
    target_precedes: bool

    @property
    def deterministic_precedence(self) -> bool:
        return self.candidate_precedes or self.target_precedes

    @property
    def temporal_order(self) -> str:
        if self.candidate_precedes:
            return 'candidate_newer'
        if self.target_precedes:
            return 'target_newer'

        return 'unordered'


@dataclass(frozen=True, slots=True)
class DerivationFacts:
    candidate_tier: str
    candidate_has_refs: bool
    comparison_complete: bool
    targets: tuple[TargetFacts, ...]


@dataclass(frozen=True, slots=True)
class DerivedDecision:
    outcome: str
    relation: str
    target_memory_version_id: UUID | None
    temporal_order: str
    applicability: str
    reason_code: str
    rung: int
    suppressed_identity_relations: tuple[tuple[UUID, str], ...]


def build_derivation_facts(data: CurationJudgeInput) -> DerivationFacts:
    candidate = data.evidence.candidate
    candidate_at = candidate.latest_evidence_at
    candidate_pair = (data.effective_scope.visibility_scope, data.effective_scope.team_id)
    targets: list[TargetFacts] = []
    for entry in data.shortlist.entries:
        target = data.evidence.targets.get(entry.memory_version_id)
        target_at = target.latest_evidence_at if target is not None else None
        ordered = candidate_at is not None and target_at is not None
        targets.append(
            TargetFacts(
                memory_version_id=entry.memory_version_id,
                tier=target.tier if target is not None else 'none',
                has_refs=bool(target.refs) if target is not None else False,
                same_visibility=(entry.visibility_scope, entry.team_id) == candidate_pair,
                has_open_conflict=bool(entry.has_open_conflict),
                candidate_precedes=ordered and candidate_at > target_at,
                target_precedes=ordered and target_at > candidate_at,
            )
        )

    return DerivationFacts(
        candidate_tier=candidate.tier,
        candidate_has_refs=bool(candidate.refs),
        comparison_complete=bool(data.shortlist.comparison_complete),
        targets=tuple(targets),
    )


def _conflict_eligible(facts: DerivationFacts, target: TargetFacts) -> bool:
    return (
        target.same_visibility
        and facts.candidate_tier in _SUPPORTED_TIERS
        and target.tier in _SUPPORTED_TIERS
        and facts.candidate_has_refs
        and target.has_refs
        and facts.comparison_complete
        and not target.deterministic_precedence
    )


def _revise_eligible(facts: DerivationFacts, target: TargetFacts) -> bool:
    return (
        target.same_visibility
        and not target.has_open_conflict
        and facts.candidate_tier == 'corroborated'
        and target.tier in _SUPPORTED_TIERS
        and target.candidate_precedes
    )


def _supersede_eligible(facts: DerivationFacts, target: TargetFacts) -> bool:
    return _revise_eligible(facts, target) and facts.comparison_complete


def _merge_eligible(facts: DerivationFacts, target: TargetFacts) -> bool:
    return (
        target.same_visibility
        and not target.has_open_conflict
        and facts.candidate_tier in _SUPPORTED_TIERS
        and target.tier in _SUPPORTED_TIERS
    )


def _redundant_eligible(_facts: DerivationFacts, target: TargetFacts) -> bool:
    return target.tier in _SUPPORTED_TIERS


_LADDER = (
    (1, 'open_conflict', 'mutually_incompatible', _conflict_eligible, True),
    (2, 'supersede_memory', 'candidate_supersedes', _supersede_eligible, True),
    (3, 'revise_memory', 'candidate_revises', _revise_eligible, True),
    (4, 'merge_evidence', 'equivalent', _merge_eligible, True),
    (5, 'reject_candidate', 'redundant', _redundant_eligible, False),
)


def feasible_outcomes(facts: DerivationFacts) -> frozenset[str]:
    outcomes: set[str] = set()
    if facts.candidate_tier in _SUPPORTED_TIERS and facts.comparison_complete:
        outcomes.add('publish_new')
    if facts.candidate_tier == 'none':
        outcomes.add('reject_candidate')
    for target in facts.targets:
        for _rung, outcome, _relation, eligible, _requires_same in _LADDER:
            if eligible(facts, target):
                outcomes.add(outcome)

    return frozenset(outcomes)


def _select_target(
    facts: DerivationFacts,
    relations: dict[UUID, str],
    applicability: dict[UUID, str],
) -> tuple[int, str, str, TargetFacts] | None:
    for rung, outcome, relation, eligible, requires_same in _LADDER:
        for target in facts.targets:
            if relations.get(target.memory_version_id) != relation:
                continue
            if requires_same and applicability.get(target.memory_version_id) != 'same':
                continue
            if eligible(facts, target):
                return rung, outcome, relation, target

    return None


def _publish_relation(facts: DerivationFacts, relations: dict[UUID, str]) -> str:
    if all(relations.get(target.memory_version_id) == 'unrelated' for target in facts.targets):
        return 'unrelated'

    return 'compatible_distinct'


def _suppressed(
    facts: DerivationFacts,
    relations: dict[UUID, str],
    selected_id: UUID | None,
) -> tuple[tuple[UUID, str], ...]:
    return tuple(
        (target.memory_version_id, relations[target.memory_version_id])
        for target in facts.targets
        if target.memory_version_id != selected_id and relations.get(target.memory_version_id) in _IDENTITY_RELATIONS
    )


def derive_decision(
    facts: DerivationFacts,
    relations: dict[UUID, str],
    applicability: dict[UUID, str],
) -> DerivedDecision | None:
    selected = _select_target(facts, relations, applicability)
    if selected is not None:
        rung, outcome, relation, target = selected

        return DerivedDecision(
            outcome=outcome,
            relation=relation,
            target_memory_version_id=target.memory_version_id,
            temporal_order=target.temporal_order,
            applicability=applicability.get(target.memory_version_id, 'not_applicable'),
            reason_code=_REASON_CODES[(outcome, relation)],
            rung=rung,
            suppressed_identity_relations=_suppressed(facts, relations, target.memory_version_id),
        )

    if facts.candidate_tier in _SUPPORTED_TIERS and facts.comparison_complete:
        relation = _publish_relation(facts, relations)

        return DerivedDecision(
            outcome='publish_new',
            relation=relation,
            target_memory_version_id=None,
            temporal_order='not_applicable',
            applicability='not_applicable',
            reason_code=_REASON_CODES[('publish_new', relation)],
            rung=6,
            suppressed_identity_relations=_suppressed(facts, relations, None),
        )

    if facts.candidate_tier == 'none':
        return DerivedDecision(
            outcome='reject_candidate',
            relation='unsupported',
            target_memory_version_id=None,
            temporal_order='not_applicable',
            applicability='not_applicable',
            reason_code=_REASON_CODES[('reject_candidate', 'unsupported')],
            rung=7,
            suppressed_identity_relations=_suppressed(facts, relations, None),
        )

    return None
