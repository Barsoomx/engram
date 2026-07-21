from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from engram.memory.curation_judge import CurationJudgeError, parse_curation_judge_verdict
from engram.memory.evals.contract import (
    CONTRACT_VERSION,
    DESTRUCTIVE_OUTCOMES,
    FROZEN_THRESHOLDS,
    SEMANTIC_OUTCOMES,
    FrozenThresholds,
)
from engram.memory.evals.corpus import build_judge_input, load_corpus, load_corpus_hash
from engram.memory.evals.gates import classify_deterministic_gate

_TARGET_OUTCOMES = frozenset({'merge_evidence', 'revise_memory', 'supersede_memory'})
_GATE_BUCKETS = frozenset({'exact_identity', 'deterministic_noise'})


@dataclass(frozen=True, slots=True)
class CaseResult:
    case_id: str
    bucket: str
    gold: str
    predicted: str
    predicted_target: str | None
    provider_called: bool
    fault_code: str | None
    correct: bool
    forbidden_hit: bool
    target_correct: bool | None
    leaked: bool


def _raw_verdict(case: dict[str, object], engine: str, responses: dict[str, object]) -> object | None:
    if engine == 'fixture':
        return case.get('fixture_verdict')

    return responses.get(str(case['case_id']))


def _as_text(raw: object) -> str:
    if isinstance(raw, str):
        return raw

    return json.dumps(raw)


def _score_case(case: dict[str, object], engine: str, responses: dict[str, object]) -> CaseResult:
    case_input = case['input']
    gate = classify_deterministic_gate(case_input)
    fault_code: str | None = None
    if gate.disposition == 'terminal':
        predicted = str(gate.outcome)
        predicted_target = str(gate.target_memory_version_id) if gate.target_memory_version_id else None
        provider_called = False
    else:
        provider_called = True
        raw = _raw_verdict(case, engine, responses)
        if raw is None:
            predicted = 'no_decision'
            predicted_target = None
            fault_code = 'response_missing'
        else:
            try:
                verdict = parse_curation_judge_verdict(_as_text(raw), build_judge_input(case_input))
                predicted = verdict.outcome
                predicted_target = (
                    str(verdict.target_memory_version_id) if verdict.target_memory_version_id is not None else None
                )
            except CurationJudgeError as error:
                predicted = 'no_decision'
                predicted_target = None
                fault_code = error.code

    allowed = set(case['allowed_outcomes'])
    forbidden = set(case['forbidden_outcomes'])
    expected_targets = set(case['expected_targets'])
    gold = str(case['primary_outcome'])
    expected_fault = case.get('expected_fault') is not None

    if expected_fault:
        correct = predicted == 'no_decision'
    else:
        correct = predicted in allowed and (not expected_targets or predicted_target in expected_targets)

    target_correct: bool | None = None
    if gold in _TARGET_OUTCOMES and expected_targets:
        target_correct = predicted == gold and predicted_target in expected_targets

    leaked = predicted_target is not None and predicted_target == case.get('out_of_scope_target')

    return CaseResult(
        case_id=str(case['case_id']),
        bucket=str(case['bucket']),
        gold=gold,
        predicted=predicted,
        predicted_target=predicted_target,
        provider_called=provider_called,
        fault_code=fault_code,
        correct=correct,
        forbidden_hit=predicted in forbidden,
        target_correct=target_correct,
        leaked=leaked,
    )


def _ratio(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 1.0

    return numerator / denominator


def _macro_f1(results: list[CaseResult]) -> float:
    scored = [result for result in results if result.gold in SEMANTIC_OUTCOMES]
    total = 0.0
    for outcome in SEMANTIC_OUTCOMES:
        tp = sum(1 for result in scored if result.gold == outcome and result.predicted == outcome)
        fp = sum(1 for result in scored if result.gold != outcome and result.predicted == outcome)
        fn = sum(1 for result in scored if result.gold == outcome and result.predicted != outcome)
        precision = _ratio(tp, tp + fp)
        recall = _ratio(tp, tp + fn)
        if precision + recall > 0:
            total += 2 * precision * recall / (precision + recall)

    return total / len(SEMANTIC_OUTCOMES)


def _confusion(results: list[CaseResult]) -> dict[str, dict[str, int]]:
    labels = (*SEMANTIC_OUTCOMES, 'no_decision')
    matrix = {gold: dict.fromkeys(labels, 0) for gold in SEMANTIC_OUTCOMES}
    for result in results:
        if result.gold in SEMANTIC_OUTCOMES:
            matrix[result.gold][result.predicted] += 1

    return matrix


def _metrics(results: list[CaseResult], engine: str, corpus_digest: str) -> dict[str, object]:
    bucket_counts: dict[str, int] = {}
    for result in results:
        bucket_counts[result.bucket] = bucket_counts.get(result.bucket, 0) + 1

    gate_cases = [result for result in results if result.bucket in _GATE_BUCKETS]
    healthy_non_conflict = [
        result for result in results if result.gold in SEMANTIC_OUTCOMES and result.gold != 'open_conflict'
    ]
    conflict_gold = [result for result in results if result.gold == 'open_conflict']
    conflict_predicted = [result for result in results if result.predicted == 'open_conflict']
    destructive_predicted = [result for result in results if result.predicted in DESTRUCTIVE_OUTCOMES]
    target_scored = [result for result in results if result.target_correct is not None]

    return {
        'contract_version': CONTRACT_VERSION,
        'engine': engine,
        'corpus_hash': corpus_digest,
        'case_count': len(results),
        'bucket_counts': bucket_counts,
        'confusion_matrix': _confusion(results),
        'forbidden_transition_count': sum(1 for result in results if result.forbidden_hit),
        'cross_scope_leakage': sum(1 for result in results if result.leaked),
        'failure_induced_semantic_decisions': sum(
            1 for result in results if result.bucket == 'provider_fault' and result.predicted in SEMANTIC_OUTCOMES
        ),
        'forbidden_or_similarity_destructive': sum(
            1 for result in results if result.predicted in DESTRUCTIVE_OUTCOMES and result.forbidden_hit
        ),
        'unresolved_skip': sum(
            1 for result in results if result.predicted == 'no_decision' and result.bucket != 'provider_fault'
        ),
        'deterministic_gate_accuracy': _ratio(sum(1 for result in gate_cases if result.correct), len(gate_cases)),
        'destructive_precision': _ratio(
            sum(1 for result in destructive_predicted if result.correct), len(destructive_predicted)
        ),
        'conflict_recall': _ratio(
            sum(1 for result in conflict_gold if result.predicted == 'open_conflict'), len(conflict_gold)
        ),
        'conflict_precision': _ratio(
            sum(1 for result in conflict_predicted if result.gold == 'open_conflict'), len(conflict_predicted)
        ),
        'target_accuracy': _ratio(sum(1 for result in target_scored if result.target_correct), len(target_scored)),
        'macro_f1': _macro_f1(results),
        'convergence': _ratio(sum(1 for result in healthy_non_conflict if result.correct), len(healthy_non_conflict)),
    }


def _evaluate_thresholds(
    metrics: dict[str, object], engine: str, thresholds: FrozenThresholds
) -> tuple[bool, list[dict[str, object]]]:
    convergence_min = thresholds.fixture_convergence_min if engine == 'fixture' else thresholds.provider_convergence_min
    checks = [
        ('cross_scope_leakage', metrics['cross_scope_leakage'], thresholds.cross_scope_leakage_max, 'max'),
        (
            'failure_induced_semantic_decisions',
            metrics['failure_induced_semantic_decisions'],
            thresholds.failure_semantic_decisions_max,
            'max',
        ),
        (
            'forbidden_or_similarity_destructive',
            metrics['forbidden_or_similarity_destructive'],
            thresholds.forbidden_destructive_max,
            'max',
        ),
        (
            'forbidden_transition_count',
            metrics['forbidden_transition_count'],
            thresholds.forbidden_destructive_max,
            'max',
        ),
        ('unresolved_skip', metrics['unresolved_skip'], thresholds.unresolved_skip_max, 'max'),
        (
            'deterministic_gate_accuracy',
            metrics['deterministic_gate_accuracy'],
            thresholds.deterministic_gate_accuracy_min,
            'min',
        ),
        ('destructive_precision', metrics['destructive_precision'], thresholds.destructive_precision_min, 'min'),
        ('conflict_recall', metrics['conflict_recall'], thresholds.conflict_recall_min, 'min'),
        ('conflict_precision', metrics['conflict_precision'], thresholds.conflict_precision_min, 'min'),
        ('target_accuracy', metrics['target_accuracy'], thresholds.target_accuracy_min, 'min'),
        ('macro_f1', metrics['macro_f1'], thresholds.macro_f1_min, 'min'),
        ('convergence', metrics['convergence'], convergence_min, 'min'),
    ]
    evaluations: list[dict[str, object]] = []
    passed = True
    for name, value, bound, direction in checks:
        ok = value <= bound if direction == 'max' else value >= bound
        passed = passed and ok
        evaluations.append({'metric': name, 'value': value, 'bound': bound, 'direction': direction, 'passed': ok})

    return passed, evaluations


def load_responses(path: Path) -> dict[str, object]:
    responses: dict[str, object] = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        responses[str(row['case_id'])] = row['verdict']

    return responses


def score_corpus(
    *,
    engine: str,
    cases: list[dict[str, object]] | None = None,
    responses: dict[str, object] | None = None,
    corpus_digest: str | None = None,
    thresholds: FrozenThresholds = FROZEN_THRESHOLDS,
) -> dict[str, object]:
    corpus_cases = cases if cases is not None else load_corpus()
    digest = corpus_digest if corpus_digest is not None else load_corpus_hash()
    response_map = responses or {}
    results = [_score_case(case, engine, response_map) for case in corpus_cases]
    metrics = _metrics(results, engine, digest)
    passed, evaluations = _evaluate_thresholds(metrics, engine, thresholds)
    report = dict(metrics)
    report['thresholds'] = evaluations
    report['passed'] = passed

    return report
