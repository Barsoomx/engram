from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from pathlib import Path

from engram.memory.curation_judge import (
    ClaimEvidence,
    CurationEvidenceContext,
    CurationJudgeInput,
)
from engram.memory.curation_shortlist import CurationShortlist, CurationShortlistEntry
from engram.memory.deterministic_gates import EffectiveCandidateScope, SanitizedCandidateView
from engram.memory.evals.contract import CONTRACT_VERSION, SEMANTIC_OUTCOMES

_NAMESPACE = uuid.UUID('a5f0c0de-0000-4000-8000-00000000c0de')
_AUTHOR = 'cp5-eval'
_REASON = 'deterministic curation eval fixture verdict'
_EARLIER = '2026-07-01T00:00:00+00:00'
_EQUAL = '2026-07-05T00:00:00+00:00'
_LATER = '2026-07-10T00:00:00+00:00'

CURATION_V1_DIR = Path(__file__).resolve().parent / 'curation_v1'
CORPUS_PATH = CURATION_V1_DIR / 'corpus.jsonl'
RESPONSES_PATH = CURATION_V1_DIR / 'selected-policy-responses.jsonl'


def _uid(key: str) -> uuid.UUID:
    return uuid.uuid5(_NAMESPACE, key)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _refs(case_id: str, prefix: str, count: int) -> list[str]:
    return [f'{case_id}-{prefix}{index}' for index in range(1, count + 1)]


def _forbidden_except(allowed: str) -> list[str]:
    return [outcome for outcome in SEMANTIC_OUTCOMES if outcome != allowed]


def _candidate(
    case_id: str,
    *,
    title: str,
    body: str,
    tier: str,
    refs: list[str],
    kind: str = 'decision',
    content_hash: str | None = None,
    redaction_codes: tuple[str, ...] = (),
    observation_type: str = 'memory',
    latest: str | None = None,
) -> dict[str, object]:
    return {
        'title': title,
        'body': body,
        'kind': kind,
        'content_hash': content_hash or _hash(f'{case_id}:candidate:{body}'),
        'evidence': [{'ref': ref} for ref in refs],
        'redaction_codes': list(redaction_codes),
        'observation_type': observation_type,
        'evidence_tier': tier,
        'evidence_refs': list(refs),
        'latest_evidence_at': latest,
    }


def _entry(
    case_id: str,
    index: int,
    *,
    title: str,
    body: str,
    tier: str,
    refs: list[str],
    kind: str = 'decision',
    body_hash: str | None = None,
    scope: str = 'project',
    team_id: str | None = None,
    has_open_conflict: bool = False,
    latest: str | None = None,
) -> dict[str, object]:
    return {
        'memory_id': str(_uid(f'{case_id}:entry:{index}:memory')),
        'memory_version_id': str(_uid(f'{case_id}:entry:{index}:version')),
        'current_transition_id': str(_uid(f'{case_id}:entry:{index}:transition')),
        'visibility_scope': scope,
        'team_id': team_id,
        'title': title,
        'body': body,
        'kind': kind,
        'body_hash': body_hash or _hash(f'{case_id}:entry:{index}:{body}'),
        'has_open_conflict': has_open_conflict,
        'evidence_tier': tier,
        'evidence_refs': list(refs),
        'latest_evidence_at': latest,
    }


def _shortlist(case_id: str, entries: list[dict[str, object]], *, complete: bool = True) -> dict[str, object]:
    version_ids = [str(entry['memory_version_id']) for entry in entries]

    return {
        'manifest_hash': _hash(f'{case_id}:manifest:' + ','.join(version_ids)),
        'authorized_corpus_count': len(entries),
        'comparison_complete': complete,
        'entries': entries,
    }


def _scope(visibility: str = 'project', team_id: str | None = None) -> dict[str, object]:
    return {'visibility_scope': visibility, 'team_id': team_id}


def _input(
    case_id: str,
    candidate: dict[str, object],
    scope: dict[str, object],
    shortlist: dict[str, object],
) -> dict[str, object]:
    return {
        'organization_id': str(_uid(f'{case_id}:organization')),
        'project_id': str(_uid(f'{case_id}:project')),
        'candidate_id': str(_uid(f'{case_id}:candidate-id')),
        'request_id': f'{case_id}-request',
        'trace_id': f'{case_id}-trace',
        'candidate': candidate,
        'effective_scope': scope,
        'shortlist': shortlist,
    }


def _verdict(
    *,
    outcome: str,
    relation: str,
    target: str | None,
    candidate_refs: list[str],
    comparisons: list[dict[str, object]],
    applicability: str = 'same',
    temporal_order: str = 'unordered',
    reason_code: str = 'distinct_claim',
) -> dict[str, object]:
    return {
        'schema_version': 1,
        'outcome': outcome,
        'relation': relation,
        'target_memory_version_id': target,
        'candidate_evidence_refs': candidate_refs,
        'comparisons': comparisons,
        'applicability': applicability,
        'temporal_order': temporal_order,
        'reason_code': reason_code,
        'reason': _REASON,
    }


def _comparison(entry: dict[str, object], relation: str, refs: list[str]) -> dict[str, object]:
    return {
        'memory_version_id': str(entry['memory_version_id']),
        'relation': relation,
        'target_evidence_refs': refs,
    }


def _case(
    case_id: str,
    bucket: str,
    *,
    gate: str,
    allowed_outcomes: list[str],
    forbidden_outcomes: list[str],
    primary_outcome: str,
    case_input: dict[str, object],
    fixture_verdict: object | None,
    expected_targets: list[str] | None = None,
    min_evidence_tier: str = 'supported',
    open_conflict_valid: bool = False,
    scope_control: str = 'in_scope',
    out_of_scope_target: str | None = None,
    expected_fault: str | None = None,
) -> dict[str, object]:
    return {
        'case_id': case_id,
        'bucket': bucket,
        'author': _AUTHOR,
        'contract_version': CONTRACT_VERSION,
        'scope_control': scope_control,
        'gate': gate,
        'allowed_outcomes': allowed_outcomes,
        'forbidden_outcomes': forbidden_outcomes,
        'primary_outcome': primary_outcome,
        'expected_targets': expected_targets or [],
        'min_evidence_tier': min_evidence_tier,
        'open_conflict_valid': open_conflict_valid,
        'out_of_scope_target': out_of_scope_target,
        'expected_fault': expected_fault,
        'source_hash': _hash(json.dumps(case_input, sort_keys=True)),
        'input': case_input,
        'fixture_verdict': fixture_verdict,
    }


def _control_for(index: int, count: int) -> str:
    if index == count - 1:
        return 'cross_team'
    if index == count - 2:
        return 'cross_project'

    return 'in_scope'


def _exact_identity_cases() -> list[dict[str, object]]:
    cases: list[dict[str, object]] = []
    for index in range(15):
        case_id = f'exact-{index:03d}'
        is_merge = index < 8
        shared = _hash(f'{case_id}:shared-body')
        if is_merge:
            candidate = _candidate(
                case_id,
                title='Deploy pipeline owner',
                body='The release pipeline is owned by the platform team.',
                tier='corroborated',
                refs=_refs(case_id, 'c', 2),
                content_hash=shared,
            )
            entry = _entry(
                case_id,
                0,
                title='Deploy pipeline owner',
                body='The release pipeline is owned by the platform team.',
                tier='supported',
                refs=[f'{case_id}-c1'],
                body_hash=shared,
            )
            case_input = _input(case_id, candidate, _scope(), _shortlist(case_id, [entry]))
            cases.append(
                _case(
                    case_id,
                    'exact_identity',
                    gate='exact_identity',
                    allowed_outcomes=['merge_evidence'],
                    forbidden_outcomes=_forbidden_except('merge_evidence'),
                    primary_outcome='merge_evidence',
                    case_input=case_input,
                    fixture_verdict=None,
                    expected_targets=[str(entry['memory_version_id'])],
                    min_evidence_tier='corroborated',
                )
            )
        else:
            candidate = _candidate(
                case_id,
                title='Cache eviction policy',
                body='Cache entries evict after ten minutes of inactivity.',
                tier='supported',
                refs=[f'{case_id}-c1'],
                content_hash=shared,
            )
            entry = _entry(
                case_id,
                0,
                title='Cache eviction policy',
                body='Cache entries evict after ten minutes of inactivity.',
                tier='corroborated',
                refs=[f'{case_id}-c1', f'{case_id}-c2'],
                body_hash=shared,
            )
            case_input = _input(case_id, candidate, _scope(), _shortlist(case_id, [entry]))
            cases.append(
                _case(
                    case_id,
                    'exact_identity',
                    gate='exact_duplicate',
                    allowed_outcomes=['reject_candidate'],
                    forbidden_outcomes=_forbidden_except('reject_candidate'),
                    primary_outcome='reject_candidate',
                    case_input=case_input,
                    fixture_verdict=None,
                    min_evidence_tier='supported',
                )
            )

    return cases


def _noise_case(case_id: str, gate: str, candidate: dict[str, object], scope: dict[str, object]) -> dict[str, object]:
    case_input = _input(case_id, candidate, scope, _shortlist(case_id, []))

    return _case(
        case_id,
        'deterministic_noise',
        gate=gate,
        allowed_outcomes=['reject_candidate'],
        forbidden_outcomes=['publish_new', 'merge_evidence', 'revise_memory', 'supersede_memory', 'open_conflict'],
        primary_outcome='reject_candidate',
        case_input=case_input,
        fixture_verdict=None,
        min_evidence_tier='none',
    )


def _deterministic_noise_cases() -> list[dict[str, object]]:
    cases: list[dict[str, object]] = []
    for offset in range(3):
        case_id = f'noise-empty-{offset:03d}'
        candidate = _candidate(case_id, title='   ', body='   ', tier='none', refs=[])
        cases.append(_noise_case(case_id, 'noise_empty', candidate, _scope()))
    for offset in range(3):
        case_id = f'noise-echo-{offset:03d}'
        candidate = _candidate(case_id, title='Nightly backup runs', body='Nightly backup runs', tier='none', refs=[])
        cases.append(_noise_case(case_id, 'noise_title_echo', candidate, _scope()))
    for offset in range(3):
        case_id = f'noise-redaction-{offset:03d}'
        candidate = _candidate(
            case_id,
            title='Token rotated',
            body='[REDACTED] [REDACTED]',
            tier='none',
            refs=[],
            redaction_codes=('secret_value',),
        )
        cases.append(_noise_case(case_id, 'noise_redaction_only', candidate, _scope()))
    for offset in range(2):
        case_id = f'noise-lifecycle-{offset:03d}'
        candidate = _candidate(
            case_id,
            title='Session ended',
            body='Session concluded without durable decisions.',
            tier='none',
            refs=[],
            observation_type='session_end',
        )
        cases.append(_noise_case(case_id, 'noise_lifecycle_only', candidate, _scope()))
    for offset in range(2):
        case_id = f'noise-session-{offset:03d}'
        candidate = _candidate(
            case_id,
            title='Scratch note',
            body='A transient scratch note tied to this session only.',
            tier='supported',
            refs=[f'{case_id}-c1'],
        )
        cases.append(_noise_case(case_id, 'non_durable_session_scope', candidate, _scope('session')))
    for offset in range(2):
        case_id = f'noise-unsafe-{offset:03d}'
        candidate = _candidate(
            case_id,
            title='Gateway credential',
            body='Gateway calls authenticate with api key sk-abcdefghij0123456789 in the header.',
            tier='supported',
            refs=[f'{case_id}-c1'],
        )
        cases.append(_noise_case(case_id, 'unsafe_after_redaction', candidate, _scope()))

    return cases


def _publish_case(
    case_id: str,
    bucket: str,
    *,
    scope_control: str,
    with_neighbor: bool,
    out_of_scope_target: str | None,
    forbidden_outcomes: list[str],
) -> dict[str, object]:
    candidate = _candidate(
        case_id,
        title='Ingestion retry budget',
        body='Ingestion retries cap at five attempts before parking work.',
        tier='supported',
        refs=[f'{case_id}-c1'],
    )
    entries: list[dict[str, object]] = []
    comparisons: list[dict[str, object]] = []
    if with_neighbor:
        entry = _entry(
            case_id,
            0,
            title='Ingestion timeout budget',
            body='Ingestion timeouts abort a request after thirty seconds.',
            tier='supported',
            refs=[f'{case_id}-t1'],
        )
        entries.append(entry)
        comparisons.append(_comparison(entry, 'compatible_distinct', []))
    case_input = _input(case_id, candidate, _scope(), _shortlist(case_id, entries))
    verdict = _verdict(
        outcome='publish_new',
        relation='compatible_distinct' if with_neighbor else 'unrelated',
        target=None,
        candidate_refs=[f'{case_id}-c1'],
        comparisons=comparisons,
        reason_code='distinct_claim',
    )

    return _case(
        case_id,
        bucket,
        gate='semantic',
        allowed_outcomes=['publish_new'],
        forbidden_outcomes=forbidden_outcomes,
        primary_outcome='publish_new',
        case_input=case_input,
        fixture_verdict=verdict,
        min_evidence_tier='supported',
        scope_control=scope_control,
        out_of_scope_target=out_of_scope_target,
    )


def _out_of_scope_version(case_id: str) -> str:
    return str(_uid(f'{case_id}:out-of-scope:version'))


def _compatible_new_cases() -> list[dict[str, object]]:
    cases: list[dict[str, object]] = []
    count = 20
    for index in range(count):
        case_id = f'publish-{index:03d}'
        control = _control_for(index, count)
        cases.append(
            _publish_case(
                case_id,
                'compatible_new',
                scope_control=control,
                with_neighbor=index % 2 == 0,
                out_of_scope_target=_out_of_scope_version(case_id) if control != 'in_scope' else None,
                forbidden_outcomes=['merge_evidence', 'revise_memory', 'supersede_memory', 'open_conflict'],
            )
        )

    return cases


def _target_case(
    case_id: str,
    bucket: str,
    *,
    outcome: str,
    relation: str,
    reason_code: str,
    candidate_tier: str,
    target_tier: str,
    applicability: str,
    temporal_order: str,
    candidate_latest: str | None,
    target_latest: str | None,
    forbidden_outcomes: list[str],
    open_conflict_valid: bool,
    min_evidence_tier: str,
    complete: bool = True,
) -> dict[str, object]:
    candidate_refs = _refs(case_id, 'c', 2 if candidate_tier == 'corroborated' else 1)
    target_refs = _refs(case_id, 't', 2 if target_tier == 'corroborated' else 1)
    candidate = _candidate(
        case_id,
        title='Primary datastore choice',
        body='The primary datastore for sessions is Postgres with pgvector.',
        tier=candidate_tier,
        refs=candidate_refs,
        latest=candidate_latest,
    )
    entry = _entry(
        case_id,
        0,
        title='Primary datastore choice',
        body='The primary datastore for sessions is Postgres.',
        tier=target_tier,
        refs=target_refs,
        latest=target_latest,
    )
    shortlist = _shortlist(case_id, [entry], complete=complete)
    case_input = _input(case_id, candidate, _scope(), shortlist)
    verdict = _verdict(
        outcome=outcome,
        relation=relation,
        target=str(entry['memory_version_id']),
        candidate_refs=candidate_refs,
        comparisons=[_comparison(entry, relation, target_refs)],
        applicability=applicability,
        temporal_order=temporal_order,
        reason_code=reason_code,
    )

    return _case(
        case_id,
        bucket,
        gate='semantic',
        allowed_outcomes=[outcome],
        forbidden_outcomes=forbidden_outcomes,
        primary_outcome=outcome,
        case_input=case_input,
        fixture_verdict=verdict,
        expected_targets=[str(entry['memory_version_id'])],
        min_evidence_tier=min_evidence_tier,
        open_conflict_valid=open_conflict_valid,
    )


def _semantic_bucket_cases(
    bucket: str,
    prefix: str,
    total: int,
    *,
    outcome: str,
    relation: str,
    reason_code: str,
    candidate_tier: str,
    target_tier: str,
    applicability: str,
    temporal_order: str,
    candidate_latest: str | None,
    target_latest: str | None,
    forbidden_outcomes: list[str],
    control_forbidden: list[str],
    open_conflict_valid: bool,
    min_evidence_tier: str,
    complete: bool = True,
) -> list[dict[str, object]]:
    cases: list[dict[str, object]] = []
    for index in range(total):
        case_id = f'{prefix}-{index:03d}'
        control = _control_for(index, total)
        if control == 'in_scope':
            cases.append(
                _target_case(
                    case_id,
                    bucket,
                    outcome=outcome,
                    relation=relation,
                    reason_code=reason_code,
                    candidate_tier=candidate_tier,
                    target_tier=target_tier,
                    applicability=applicability,
                    temporal_order=temporal_order,
                    candidate_latest=candidate_latest,
                    target_latest=target_latest,
                    forbidden_outcomes=forbidden_outcomes,
                    open_conflict_valid=open_conflict_valid,
                    min_evidence_tier=min_evidence_tier,
                    complete=complete,
                )
            )
        else:
            cases.append(
                _publish_case(
                    case_id,
                    bucket,
                    scope_control=control,
                    with_neighbor=False,
                    out_of_scope_target=_out_of_scope_version(case_id),
                    forbidden_outcomes=control_forbidden,
                )
            )

    return cases


def _lookalike_cases() -> list[dict[str, object]]:
    cases: list[dict[str, object]] = []
    total = 10
    for index in range(total):
        case_id = f'lookalike-{index:03d}'
        control = _control_for(index, total)
        if control == 'in_scope':
            cases.append(
                _publish_case(
                    case_id,
                    'lookalike_non_conflict',
                    scope_control='in_scope',
                    with_neighbor=True,
                    out_of_scope_target=None,
                    forbidden_outcomes=['open_conflict', 'merge_evidence', 'revise_memory', 'supersede_memory'],
                )
            )
        else:
            cases.append(
                _publish_case(
                    case_id,
                    'lookalike_non_conflict',
                    scope_control=control,
                    with_neighbor=False,
                    out_of_scope_target=_out_of_scope_version(case_id),
                    forbidden_outcomes=['open_conflict', 'merge_evidence', 'revise_memory', 'supersede_memory'],
                )
            )

    return cases


def _fault_cases() -> list[dict[str, object]]:
    cases: list[dict[str, object]] = []
    templates = [
        ('fault-json', '```json\n{invalid json without close'),
        ('fault-schema', {'schema_version': 1, 'outcome': 'publish_new'}),
        ('fault-target', 'invented_target'),
        ('fault-evidence', 'invented_evidence'),
        ('fault-policy', 'weak_destructive'),
    ]
    for index, (prefix, marker) in enumerate(templates):
        case_id = f'{prefix}-{index:03d}'
        candidate = _candidate(
            case_id,
            title='Retention window',
            body='Audit logs are retained for ninety days before archival.',
            tier='corroborated' if marker != 'weak_destructive' else 'none',
            refs=_refs(case_id, 'c', 2) if marker != 'weak_destructive' else [],
            latest=_LATER,
        )
        entry = _entry(
            case_id,
            0,
            title='Retention window',
            body='Audit logs are retained for sixty days.',
            tier='supported',
            refs=[f'{case_id}-t1'],
            latest=_EARLIER,
        )
        shortlist = _shortlist(case_id, [entry])
        case_input = _input(case_id, candidate, _scope(), shortlist)
        fixture_verdict: object
        if marker == '```json\n{invalid json without close':
            fixture_verdict = marker
        elif isinstance(marker, dict):
            fixture_verdict = marker
        elif marker == 'invented_target':
            fixture_verdict = _verdict(
                outcome='merge_evidence',
                relation='equivalent',
                target=str(_uid(f'{case_id}:phantom')),
                candidate_refs=_refs(case_id, 'c', 2),
                comparisons=[_comparison(entry, 'equivalent', [f'{case_id}-t1'])],
            )
        elif marker == 'invented_evidence':
            fixture_verdict = _verdict(
                outcome='merge_evidence',
                relation='equivalent',
                target=str(entry['memory_version_id']),
                candidate_refs=[f'{case_id}-phantom-ref'],
                comparisons=[_comparison(entry, 'equivalent', [f'{case_id}-t1'])],
            )
        else:
            fixture_verdict = _verdict(
                outcome='supersede_memory',
                relation='candidate_supersedes',
                target=str(entry['memory_version_id']),
                candidate_refs=[],
                comparisons=[_comparison(entry, 'candidate_supersedes', [f'{case_id}-t1'])],
                applicability='same',
                temporal_order='candidate_newer',
                reason_code='ordered_replacement',
            )
        cases.append(
            _case(
                case_id,
                'provider_fault',
                gate='semantic',
                allowed_outcomes=[],
                forbidden_outcomes=[
                    'publish_new',
                    'merge_evidence',
                    'revise_memory',
                    'supersede_memory',
                    'reject_candidate',
                    'open_conflict',
                ],
                primary_outcome='no_decision',
                case_input=case_input,
                fixture_verdict=fixture_verdict,
                min_evidence_tier='none',
                expected_fault='no_semantic_decision',
                scope_control='in_scope',
            )
        )

    return cases


def build_corpus() -> list[dict[str, object]]:
    cases: list[dict[str, object]] = []
    cases.extend(_exact_identity_cases())
    cases.extend(_deterministic_noise_cases())
    cases.extend(_compatible_new_cases())
    cases.extend(
        _semantic_bucket_cases(
            'equivalent_merge',
            'merge',
            15,
            outcome='merge_evidence',
            relation='equivalent',
            reason_code='equivalent_claim',
            candidate_tier='supported',
            target_tier='supported',
            applicability='same',
            temporal_order='unordered',
            candidate_latest=_EQUAL,
            target_latest=_EQUAL,
            forbidden_outcomes=_forbidden_except('merge_evidence'),
            control_forbidden=['merge_evidence', 'revise_memory', 'supersede_memory', 'open_conflict'],
            open_conflict_valid=False,
            min_evidence_tier='supported',
        )
    )
    cases.extend(
        _semantic_bucket_cases(
            'revision',
            'revise',
            15,
            outcome='revise_memory',
            relation='candidate_revises',
            reason_code='same_subject_revision',
            candidate_tier='corroborated',
            target_tier='supported',
            applicability='same',
            temporal_order='candidate_newer',
            candidate_latest=_LATER,
            target_latest=_EARLIER,
            forbidden_outcomes=_forbidden_except('revise_memory'),
            control_forbidden=['merge_evidence', 'revise_memory', 'supersede_memory', 'open_conflict'],
            open_conflict_valid=False,
            min_evidence_tier='corroborated',
        )
    )
    cases.extend(
        _semantic_bucket_cases(
            'safe_supersession',
            'supersede',
            10,
            outcome='supersede_memory',
            relation='candidate_supersedes',
            reason_code='ordered_replacement',
            candidate_tier='corroborated',
            target_tier='supported',
            applicability='same',
            temporal_order='candidate_newer',
            candidate_latest=_LATER,
            target_latest=_EARLIER,
            forbidden_outcomes=['publish_new', 'merge_evidence', 'revise_memory', 'open_conflict', 'reject_candidate'],
            control_forbidden=['merge_evidence', 'revise_memory', 'supersede_memory', 'open_conflict'],
            open_conflict_valid=False,
            min_evidence_tier='corroborated',
        )
    )
    cases.extend(
        _semantic_bucket_cases(
            'genuine_conflict',
            'conflict',
            15,
            outcome='open_conflict',
            relation='mutually_incompatible',
            reason_code='same_scope_contradiction',
            candidate_tier='supported',
            target_tier='supported',
            applicability='same',
            temporal_order='unordered',
            candidate_latest=_EQUAL,
            target_latest=_EQUAL,
            forbidden_outcomes=_forbidden_except('open_conflict'),
            control_forbidden=['merge_evidence', 'revise_memory', 'supersede_memory', 'open_conflict'],
            open_conflict_valid=True,
            min_evidence_tier='supported',
        )
    )
    cases.extend(_lookalike_cases())
    cases.extend(_fault_cases())

    return cases


def corpus_jsonl_lines(cases: list[dict[str, object]]) -> list[str]:
    return [json.dumps(case, sort_keys=True, ensure_ascii=False) for case in cases]


def corpus_hash(lines: list[str]) -> str:
    digest = hashlib.sha256()
    for line in lines:
        digest.update(line.encode())
        digest.update(b'\n')

    return digest.hexdigest()


def load_corpus(path: Path | None = None) -> list[dict[str, object]]:
    target = path or CORPUS_PATH
    cases: list[dict[str, object]] = []
    for line in target.read_text(encoding='utf-8').splitlines():
        if line.strip():
            cases.append(json.loads(line))

    return cases


def load_corpus_hash(path: Path | None = None) -> str:
    target = path or CORPUS_PATH
    lines = [line for line in target.read_text(encoding='utf-8').splitlines() if line.strip()]

    return corpus_hash(lines)


def _parse_dt(value: object) -> datetime | None:
    if value is None:
        return None

    return datetime.fromisoformat(str(value))


def _claim(evidence: dict[str, object]) -> ClaimEvidence:
    return ClaimEvidence(
        tier=str(evidence['evidence_tier']),
        refs=tuple(str(ref) for ref in evidence['evidence_refs']),
        latest_evidence_at=_parse_dt(evidence.get('latest_evidence_at')),
    )


def build_judge_input(case_input: dict[str, object]) -> CurationJudgeInput:
    candidate = case_input['candidate']
    scope = case_input['effective_scope']
    shortlist = case_input['shortlist']
    entries = tuple(
        CurationShortlistEntry(
            memory_id=uuid.UUID(str(entry['memory_id'])),
            memory_version_id=uuid.UUID(str(entry['memory_version_id'])),
            current_transition_id=uuid.UUID(str(entry['current_transition_id'])),
            visibility_scope=str(entry['visibility_scope']),
            team_id=uuid.UUID(str(entry['team_id'])) if entry.get('team_id') else None,
            title=str(entry['title']),
            body=str(entry['body']),
            kind=str(entry['kind']),
            body_hash=str(entry['body_hash']),
            exact_overlap=0,
            vector_distance=None,
            lexical_rank=None,
            trigram_similarity=None,
            has_open_conflict=bool(entry['has_open_conflict']),
        )
        for entry in shortlist['entries']
    )
    evidence = CurationEvidenceContext(
        candidate=_claim(candidate),
        targets={uuid.UUID(str(entry['memory_version_id'])): _claim(entry) for entry in shortlist['entries']},
    )
    sanitized = SanitizedCandidateView(
        title=str(candidate['title']),
        body=str(candidate['body']),
        kind=str(candidate['kind']),
        evidence=tuple(candidate.get('evidence') or ()),
        content_hash=str(candidate['content_hash']),
        redaction_codes=tuple(str(code) for code in candidate.get('redaction_codes') or ()),
    )

    return CurationJudgeInput(
        organization_id=uuid.UUID(str(case_input['organization_id'])),
        project_id=uuid.UUID(str(case_input['project_id'])),
        candidate_id=uuid.UUID(str(case_input['candidate_id'])),
        candidate=sanitized,
        effective_scope=EffectiveCandidateScope(
            visibility_scope=str(scope['visibility_scope']),
            team_id=uuid.UUID(str(scope['team_id'])) if scope.get('team_id') else None,
        ),
        shortlist=CurationShortlist(
            entries=entries,
            manifest_hash=str(shortlist['manifest_hash']),
            authorized_corpus_count=int(shortlist['authorized_corpus_count']),
            comparison_complete=bool(shortlist['comparison_complete']),
        ),
        evidence=evidence,
        request_id=str(case_input['request_id']),
        trace_id=str(case_input['trace_id']),
    )
