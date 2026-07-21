from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from engram.memory.evals.corpus import RESPONSES_PATH, load_corpus


def _run(*args: str) -> dict[str, object]:
    out = StringIO()
    call_command('engram_curator_eval', *args, stdout=out)

    return json.loads(out.getvalue())


def test_fixture_engine_emits_passing_json_report() -> None:
    report = _run('--engine', 'fixture', '--format', 'json')

    assert report['engine'] == 'fixture'
    assert report['passed'] is True
    assert report['case_count'] >= 120


def test_responses_engine_scores_committed_provider_artifact() -> None:
    report = _run('--responses', str(RESPONSES_PATH), '--format', 'json')

    assert report['engine'] == 'responses'
    assert report['passed'] is True


def test_threshold_miss_exits_nonzero(tmp_path: Path) -> None:
    cases = load_corpus()
    target = next(
        case for case in cases if case['bucket'] == 'equivalent_merge' and case['scope_control'] == 'in_scope'
    )
    entry = target['input']['shortlist']['entries'][0]
    lines: list[str] = []
    for case in cases:
        if case['gate'] != 'semantic' or case['fixture_verdict'] is None or case['bucket'] == 'provider_fault':
            continue
        verdict = case['fixture_verdict']
        if case['case_id'] == target['case_id']:
            verdict = {
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
        lines.append(json.dumps({'case_id': case['case_id'], 'verdict': verdict}))
    responses_file = tmp_path / 'responses.jsonl'
    responses_file.write_text('\n'.join(lines), encoding='utf-8')

    with pytest.raises(CommandError):
        call_command('engram_curator_eval', '--responses', str(responses_file), stdout=StringIO())
