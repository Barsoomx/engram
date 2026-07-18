from __future__ import annotations

import copy

import pytest

from engram.memory.evals.corpus import build_corpus
from engram.memory.evals.scorer import score_corpus

_DIGEST = 'test-corpus-digest'
_REQUIRED_REPORT_KEYS = {
    'contract_version',
    'engine',
    'corpus_hash',
    'case_count',
    'bucket_counts',
    'confusion_matrix',
    'forbidden_transition_count',
    'cross_scope_leakage',
    'failure_induced_semantic_decisions',
    'forbidden_or_similarity_destructive',
    'unresolved_skip',
    'deterministic_gate_accuracy',
    'destructive_precision',
    'conflict_recall',
    'conflict_precision',
    'target_accuracy',
    'macro_f1',
    'convergence',
    'thresholds',
    'passed',
}


@pytest.fixture
def f_cases() -> list[dict[str, object]]:
    return build_corpus()


def _fixture_responses(cases: list[dict[str, object]]) -> dict[str, object]:
    return {
        str(case['case_id']): case['fixture_verdict']
        for case in cases
        if case['gate'] == 'semantic' and case['fixture_verdict'] is not None and case['bucket'] != 'provider_fault'
    }


def test_fixture_engine_passes_every_frozen_threshold(f_cases: list[dict[str, object]]) -> None:
    report = score_corpus(engine='fixture', cases=f_cases, corpus_digest=_DIGEST)

    assert report['passed'] is True
    assert all(check['passed'] for check in report['thresholds'])
    assert report['cross_scope_leakage'] == 0
    assert report['failure_induced_semantic_decisions'] == 0
    assert report['forbidden_transition_count'] == 0
    assert report['conflict_recall'] == 1.0
    assert report['destructive_precision'] == 1.0
    assert report['deterministic_gate_accuracy'] == 1.0
    assert report['convergence'] == 1.0
    assert report['macro_f1'] >= 0.92


def test_report_exposes_every_required_metric_field(f_cases: list[dict[str, object]]) -> None:
    report = score_corpus(engine='fixture', cases=f_cases, corpus_digest=_DIGEST)

    assert set(report.keys()) == _REQUIRED_REPORT_KEYS
    assert report['contract_version'] == 'curation_v1'
    assert report['corpus_hash'] == _DIGEST


def test_provider_fault_cases_never_produce_semantic_decision(f_cases: list[dict[str, object]]) -> None:
    faults = [case for case in f_cases if case['bucket'] == 'provider_fault']
    report = score_corpus(engine='fixture', cases=faults, corpus_digest=_DIGEST)

    assert report['failure_induced_semantic_decisions'] == 0
    for gold in report['confusion_matrix'].values():
        assert sum(gold.values()) == 0


def test_missing_semantic_decision_fails_unresolved_skip_threshold(f_cases: list[dict[str, object]]) -> None:
    perturbed = copy.deepcopy(f_cases)
    for case in perturbed:
        if case['bucket'] == 'equivalent_merge' and case['scope_control'] == 'in_scope':
            case['fixture_verdict'] = None
            break

    report = score_corpus(engine='fixture', cases=perturbed, corpus_digest=_DIGEST)

    assert report['passed'] is False
    failed = {check['metric'] for check in report['thresholds'] if not check['passed']}
    assert 'unresolved_skip' in failed


def test_responses_engine_scores_captured_provider_verdicts(f_cases: list[dict[str, object]]) -> None:
    responses = _fixture_responses(f_cases)

    report = score_corpus(engine='responses', cases=f_cases, responses=responses, corpus_digest=_DIGEST)

    assert report['engine'] == 'responses'
    assert report['passed'] is True
    assert report['convergence'] >= 0.98


def test_bad_provider_forbidden_transition_fails_gate(f_cases: list[dict[str, object]]) -> None:
    responses = _fixture_responses(f_cases)
    target = next(
        case for case in f_cases if case['bucket'] == 'equivalent_merge' and case['scope_control'] == 'in_scope'
    )
    entry = target['input']['shortlist']['entries'][0]
    responses[str(target['case_id'])] = {
        'schema_version': 1,
        'outcome': 'open_conflict',
        'relation': 'mutually_incompatible',
        'target_memory_version_id': entry['memory_version_id'],
        'candidate_evidence_refs': list(target['input']['candidate']['evidence_refs']),
        'comparisons': [
            {
                'memory_version_id': entry['memory_version_id'],
                'relation': 'mutually_incompatible',
                'target_evidence_refs': list(entry['evidence_refs']),
            }
        ],
        'applicability': 'same',
        'temporal_order': 'unordered',
        'reason_code': 'same_scope_contradiction',
        'reason': 'injected incompatible verdict',
    }

    report = score_corpus(engine='responses', cases=f_cases, responses=responses, corpus_digest=_DIGEST)

    assert report['passed'] is False
    assert report['forbidden_transition_count'] >= 1
