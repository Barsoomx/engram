from __future__ import annotations

from dataclasses import dataclass

CONTRACT_VERSION = 'curation_v1'

SEMANTIC_OUTCOMES = (
    'publish_new',
    'merge_evidence',
    'revise_memory',
    'supersede_memory',
    'reject_candidate',
    'open_conflict',
)
DESTRUCTIVE_OUTCOMES = frozenset({'revise_memory', 'supersede_memory', 'reject_candidate'})

BUCKETS = (
    'exact_identity',
    'deterministic_noise',
    'compatible_new',
    'equivalent_merge',
    'revision',
    'safe_supersession',
    'genuine_conflict',
    'lookalike_non_conflict',
    'provider_fault',
)
BUCKET_MINIMUMS = {
    'exact_identity': 15,
    'deterministic_noise': 15,
    'compatible_new': 20,
    'equivalent_merge': 15,
    'revision': 15,
    'safe_supersession': 10,
    'genuine_conflict': 15,
    'lookalike_non_conflict': 10,
    'provider_fault': 5,
}
MINIMUM_CASES = 120
SEMANTIC_BUCKETS = frozenset(
    {
        'compatible_new',
        'equivalent_merge',
        'revision',
        'safe_supersession',
        'genuine_conflict',
        'lookalike_non_conflict',
    }
)


@dataclass(frozen=True, slots=True)
class FrozenThresholds:
    cross_scope_leakage_max: int = 0
    failure_semantic_decisions_max: int = 0
    forbidden_destructive_max: int = 0
    unresolved_skip_max: int = 0
    deterministic_gate_accuracy_min: float = 1.0
    destructive_precision_min: float = 1.0
    conflict_recall_min: float = 1.0
    conflict_precision_min: float = 0.95
    target_accuracy_min: float = 0.95
    macro_f1_min: float = 0.92
    fixture_convergence_min: float = 1.0
    provider_convergence_min: float = 0.98


FROZEN_THRESHOLDS = FrozenThresholds()
