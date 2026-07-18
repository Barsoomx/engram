from __future__ import annotations

import json

import pytest

from engram.memory.curation_judge import CurationJudgeError, parse_curation_judge_verdict
from engram.memory.evals.contract import (
    BUCKET_MINIMUMS,
    CONTRACT_VERSION,
    MINIMUM_CASES,
    SEMANTIC_BUCKETS,
)
from engram.memory.evals.corpus import (
    build_corpus,
    build_judge_input,
    corpus_hash,
    corpus_jsonl_lines,
    load_corpus,
    load_corpus_hash,
)

_REQUIRED_KEYS = {
    'case_id',
    'bucket',
    'author',
    'contract_version',
    'scope_control',
    'gate',
    'allowed_outcomes',
    'forbidden_outcomes',
    'primary_outcome',
    'expected_targets',
    'min_evidence_tier',
    'open_conflict_valid',
    'out_of_scope_target',
    'expected_fault',
    'source_hash',
    'input',
    'fixture_verdict',
}


@pytest.fixture
def f_corpus() -> list[dict[str, object]]:
    return build_corpus()


def _bucket_counts(cases: list[dict[str, object]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for case in cases:
        counts[str(case['bucket'])] = counts.get(str(case['bucket']), 0) + 1

    return counts


def test_corpus_meets_minimum_case_count(f_corpus: list[dict[str, object]]) -> None:
    assert len(f_corpus) >= MINIMUM_CASES


def test_corpus_meets_every_bucket_minimum(f_corpus: list[dict[str, object]]) -> None:
    counts = _bucket_counts(f_corpus)
    for bucket, minimum in BUCKET_MINIMUMS.items():
        assert counts.get(bucket, 0) >= minimum


def test_every_case_has_required_schema_keys(f_corpus: list[dict[str, object]]) -> None:
    for case in f_corpus:
        assert set(case.keys()) == _REQUIRED_KEYS
        assert case['contract_version'] == CONTRACT_VERSION


def test_case_ids_are_unique(f_corpus: list[dict[str, object]]) -> None:
    ids = [case['case_id'] for case in f_corpus]
    assert len(ids) == len(set(ids))


def test_every_semantic_bucket_has_cross_project_and_cross_team_controls(
    f_corpus: list[dict[str, object]],
) -> None:
    for bucket in SEMANTIC_BUCKETS:
        controls = {str(case['scope_control']) for case in f_corpus if case['bucket'] == bucket}
        assert 'cross_project' in controls
        assert 'cross_team' in controls


def test_negative_controls_record_excluded_out_of_scope_target(
    f_corpus: list[dict[str, object]],
) -> None:
    for case in f_corpus:
        if case['scope_control'] in ('cross_project', 'cross_team'):
            assert case['out_of_scope_target'] is not None
            version_ids = {entry['memory_version_id'] for entry in case['input']['shortlist']['entries']}
            assert case['out_of_scope_target'] not in version_ids


def test_semantic_fixture_verdicts_parse_and_faults_reject(f_corpus: list[dict[str, object]]) -> None:
    for case in f_corpus:
        if case['gate'] != 'semantic' or case['fixture_verdict'] is None:
            continue
        judge_input = build_judge_input(case['input'])
        raw = case['fixture_verdict']
        text = raw if isinstance(raw, str) else json.dumps(raw)
        if case['bucket'] == 'provider_fault':
            with pytest.raises(CurationJudgeError):
                parse_curation_judge_verdict(text, judge_input)
        else:
            verdict = parse_curation_judge_verdict(text, judge_input)
            assert verdict.outcome == case['primary_outcome']


def test_committed_corpus_matches_generator(f_corpus: list[dict[str, object]]) -> None:
    assert load_corpus() == f_corpus


def test_committed_corpus_hash_is_stable(f_corpus: list[dict[str, object]]) -> None:
    assert load_corpus_hash() == corpus_hash(corpus_jsonl_lines(f_corpus))
