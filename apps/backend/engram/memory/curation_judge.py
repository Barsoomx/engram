from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime

from engram.core.models import (
    MemoryCandidate,
    MemoryCandidateSource,
    MemoryCandidateSourceKind,
    MemoryVersionSource,
)
from engram.core.redaction import SECRET_STRING_RE, redact_value
from engram.memory.candidate_parsing import truncate_with_marker
from engram.memory.curation_shortlist import CurationShortlist, CurationShortlistEntry
from engram.memory.deterministic_gates import EffectiveCandidateScope, SanitizedCandidateView
from engram.memory.distillation_provenance import ProvenanceContractError, canonical_source_manifest
from engram.memory.workflow_work import observation_content_digest
from engram.model_policy.errors import ModelPolicyError
from engram.model_policy.services import (
    ProviderCallInput,
    ResolveModelPolicy,
    ResolveModelPolicyInput,
    get_provider_gateway,
)


class CurationJudgeError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ClaimEvidence:
    tier: str
    refs: tuple[str, ...]
    latest_evidence_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CurationEvidenceContext:
    candidate: ClaimEvidence
    targets: dict[uuid.UUID, ClaimEvidence]


@dataclass(frozen=True, slots=True)
class CurationJudgeInput:
    organization_id: uuid.UUID
    project_id: uuid.UUID
    candidate_id: uuid.UUID
    candidate: SanitizedCandidateView
    effective_scope: EffectiveCandidateScope
    shortlist: CurationShortlist
    evidence: CurationEvidenceContext
    request_id: str
    trace_id: str


@dataclass(frozen=True, slots=True)
class CurationJudgeComparisonV1:
    memory_version_id: uuid.UUID
    relation: str
    target_evidence_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CurationJudgeVerdictV1:
    schema_version: int
    outcome: str
    relation: str
    target_memory_version_id: uuid.UUID | None
    candidate_evidence_refs: tuple[str, ...]
    comparisons: tuple[CurationJudgeComparisonV1, ...]
    applicability: str
    temporal_order: str
    reason_code: str
    reason: str


@dataclass(frozen=True, slots=True)
class CurationJudgeResult:
    verdict: CurationJudgeVerdictV1
    provider_call_record_id: uuid.UUID
    policy_id: uuid.UUID
    policy_version: int
    response_hash: str
    fallback_used: bool
    comparison_manifest_hash: str
    authorized_corpus_count: int
    comparison_complete: bool


_LIFECYCLE_TYPES = frozenset({'session_start', 'session_end', 'session_lifecycle'})
_GROUP_TOKEN_PREFIX = 'curation-evidence-group:v1:'
_MAX_EVIDENCE_REFS = 16
_MAX_CLAIM_SNAPSHOT_CHARS = 2000

_TOP_KEYS = frozenset(
    {
        'schema_version',
        'outcome',
        'relation',
        'target_memory_version_id',
        'candidate_evidence_refs',
        'comparisons',
        'applicability',
        'temporal_order',
        'reason_code',
        'reason',
    }
)
_COMPARISON_KEYS = frozenset({'memory_version_id', 'relation', 'target_evidence_refs'})
_OUTCOMES = frozenset(
    {'publish_new', 'merge_evidence', 'revise_memory', 'supersede_memory', 'reject_candidate', 'open_conflict'}
)
_RELATIONS = frozenset(
    {
        'unrelated',
        'compatible_distinct',
        'equivalent',
        'candidate_revises',
        'candidate_supersedes',
        'redundant',
        'unsupported',
        'mutually_incompatible',
    }
)
_REASON_CODES = frozenset(
    {
        'distinct_claim',
        'equivalent_claim',
        'same_subject_revision',
        'ordered_replacement',
        'redundant_claim',
        'unsupported_claim',
        'same_scope_contradiction',
    }
)
_APPLICABILITY = frozenset({'same', 'different'})
_TEMPORAL_ORDERS = frozenset({'candidate_newer', 'target_newer', 'unordered', 'not_applicable'})
_ALLOWED_COMBINATIONS = {
    ('publish_new', 'unrelated'): False,
    ('publish_new', 'compatible_distinct'): False,
    ('merge_evidence', 'equivalent'): True,
    ('revise_memory', 'candidate_revises'): True,
    ('supersede_memory', 'candidate_supersedes'): True,
    ('reject_candidate', 'redundant'): True,
    ('reject_candidate', 'unsupported'): False,
    ('open_conflict', 'mutually_incompatible'): True,
}
_SUPPORTED_TIERS = frozenset({'supported', 'corroborated'})
_MUTATION_OUTCOMES = frozenset({'merge_evidence', 'open_conflict', 'revise_memory', 'supersede_memory'})
_IDENTITY_RELATIONS = frozenset(
    {'equivalent', 'candidate_revises', 'candidate_supersedes', 'redundant', 'mutually_incompatible'}
)
_TARGETLESS_OUTCOMES = frozenset({'publish_new', 'reject_candidate'})


def _group_token(input_hash: str) -> str:
    return hashlib.sha256(f'{_GROUP_TOKEN_PREFIX}{input_hash}'.encode()).hexdigest()[:32]


def _claim_evidence(hashes: set[str], latest_evidence_at: datetime | None) -> ClaimEvidence:
    tokens = sorted(_group_token(value) for value in hashes)
    if not hashes:
        tier = 'none'
    elif len(hashes) == 1:
        tier = 'supported'
    else:
        tier = 'corroborated'

    return ClaimEvidence(
        tier=tier,
        refs=tuple(tokens[:_MAX_EVIDENCE_REFS]),
        latest_evidence_at=latest_evidence_at,
    )


def _source_evidence_time(source: MemoryCandidateSource) -> datetime | None:
    if source.observation_id is None:
        return None

    observation = source.observation

    return observation.observed_at or observation.created_at


def _newer(current: datetime | None, candidate: datetime | None) -> datetime | None:
    if candidate is None:
        return current
    if current is None or candidate > current:
        return candidate

    return current


def _eligible_group_hash(source: MemoryCandidateSource) -> str | None:
    if source.source_kind == MemoryCandidateSourceKind.AGENT_PROPOSAL:
        from engram.memory.import_provenance import ImportProvenanceError, _validated_agent_anchors

        try:
            _validated_agent_anchors(source)
        except ImportProvenanceError as error:
            raise CurationJudgeError('transition_dependency_unavailable') from error

        return source.anchors_hash
    if source.source_kind != MemoryCandidateSourceKind.DISTILLATION:
        return None
    if source.window_id is None or source.stage_id is None:
        return None
    observation = source.observation
    metadata = observation.source_metadata or {}
    if observation.observation_type in _LIFECYCLE_TYPES or metadata.get('event_type') in _LIFECYCLE_TYPES:
        return None
    anchors = source.anchors
    try:
        manifest = canonical_source_manifest(anchors)
    except ProvenanceContractError as error:
        raise CurationJudgeError('transition_dependency_unavailable') from error
    if manifest != source.anchors_hash:
        raise CurationJudgeError('transition_dependency_unavailable')
    if anchors.get('observation_digest') != observation_content_digest(observation):
        raise CurationJudgeError('transition_dependency_unavailable')

    return source.window.input_hash


def _candidate_group_hashes(candidate_id: uuid.UUID) -> tuple[set[str], datetime | None]:
    sources = MemoryCandidateSource.objects.select_related('window', 'observation', 'stage').filter(
        candidate_id=candidate_id
    )
    hashes: set[str] = set()
    latest: datetime | None = None
    for source in sources:
        value = _eligible_group_hash(source)
        if value is not None:
            hashes.add(value)
            latest = _newer(latest, _source_evidence_time(source))

    return hashes, latest


def _traverse_target(
    version_id: uuid.UUID,
    hashes: set[str],
    times: list[datetime],
    path: set[uuid.UUID],
    resolved: set[uuid.UUID],
) -> None:
    if version_id in resolved:
        return
    if version_id in path:
        raise CurationJudgeError('transition_dependency_unavailable')

    path.add(version_id)
    rows = list(
        MemoryVersionSource.objects.select_related(
            'candidate_source',
            'candidate_source__window',
            'candidate_source__observation',
            'candidate_source__stage',
        ).filter(memory_version_id=version_id)
    )
    if not rows:
        raise CurationJudgeError('transition_dependency_unavailable')

    for row in rows:
        if row.candidate_source_id is not None:
            value = _eligible_group_hash(row.candidate_source)
            if value is not None:
                hashes.add(value)
                moment = _source_evidence_time(row.candidate_source)
                if moment is not None:
                    times.append(moment)
        elif row.source_memory_version_id is not None:
            _traverse_target(row.source_memory_version_id, hashes, times, path, resolved)

    path.discard(version_id)
    resolved.add(version_id)


def _target_evidence(version_id: uuid.UUID) -> ClaimEvidence:
    hashes: set[str] = set()
    times: list[datetime] = []
    _traverse_target(version_id, hashes, times, set(), set())

    return _claim_evidence(hashes, max(times) if times else None)


def build_curation_evidence_context(candidate_id: uuid.UUID, shortlist: CurationShortlist) -> CurationEvidenceContext:
    candidate_hashes, candidate_latest = _candidate_group_hashes(candidate_id)
    candidate = _claim_evidence(candidate_hashes, candidate_latest)
    targets = {entry.memory_version_id: _target_evidence(entry.memory_version_id) for entry in shortlist.entries}

    return CurationEvidenceContext(candidate=candidate, targets=targets)


def _is_enum(value: object, allowed: frozenset[str]) -> bool:
    return isinstance(value, str) and value in allowed


def _parse_uuid(value: object) -> uuid.UUID:
    if not isinstance(value, str):
        raise CurationJudgeError('judge_invalid_output')
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as error:
        raise CurationJudgeError('judge_invalid_output') from error


def _validate_refs(value: object, allowed: set[str]) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise CurationJudgeError('judge_invalid_output')
    if len(value) > _MAX_EVIDENCE_REFS:
        raise CurationJudgeError('judge_invalid_output')
    if len(set(value)) != len(value):
        raise CurationJudgeError('judge_invalid_output')
    if any(item not in allowed for item in value):
        raise CurationJudgeError('judge_reference_invalid')

    return tuple(value)


def _validate_comparisons(
    value: object,
    data: CurationJudgeInput,
    version_ids: tuple[uuid.UUID, ...],
) -> tuple[CurationJudgeComparisonV1, ...]:
    if not isinstance(value, list):
        raise CurationJudgeError('judge_invalid_output')

    parsed: list[tuple[uuid.UUID, str, object]] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != _COMPARISON_KEYS:
            raise CurationJudgeError('judge_invalid_output')
        if not _is_enum(item['relation'], _RELATIONS):
            raise CurationJudgeError('judge_invalid_output')
        parsed.append((_parse_uuid(item['memory_version_id']), item['relation'], item['target_evidence_refs']))

    if tuple(entry[0] for entry in parsed) != version_ids:
        raise CurationJudgeError('judge_invalid_output')

    comparisons: list[CurationJudgeComparisonV1] = []
    for version_id, relation, refs in parsed:
        allowed = set(data.evidence.targets[version_id].refs) if version_id in data.evidence.targets else set()
        comparisons.append(CurationJudgeComparisonV1(version_id, relation, _validate_refs(refs, allowed)))

    return tuple(comparisons)


def _candidate_precedes(data: CurationJudgeInput, target_id: uuid.UUID | None) -> bool:
    if target_id is None:
        return False
    target = data.evidence.targets.get(target_id)
    if target is None:
        return False
    candidate_at = data.evidence.candidate.latest_evidence_at
    target_at = target.latest_evidence_at
    if candidate_at is None or target_at is None:
        return False

    return candidate_at > target_at


def _deterministic_precedence(data: CurationJudgeInput, target_id: uuid.UUID | None) -> bool:
    if target_id is None:
        return False
    target = data.evidence.targets.get(target_id)
    if target is None:
        return False
    candidate_at = data.evidence.candidate.latest_evidence_at
    target_at = target.latest_evidence_at
    if candidate_at is None or target_at is None:
        return False

    return candidate_at != target_at


def _apply_evidence_policy(verdict: CurationJudgeVerdictV1, data: CurationJudgeInput) -> None:  # noqa: C901
    key = (verdict.outcome, verdict.relation)
    if key not in _ALLOWED_COMBINATIONS:
        raise CurationJudgeError('judge_invalid_output')

    target_required = _ALLOWED_COMBINATIONS[key]
    target_id = verdict.target_memory_version_id
    if target_required != (target_id is not None):
        raise CurationJudgeError('judge_invalid_output')

    candidate_tier = data.evidence.candidate.tier
    complete = data.shortlist.comparison_complete
    target_tier = 'none'
    entry: CurationShortlistEntry | None = None
    if target_id is not None:
        if target_id in data.evidence.targets:
            target_tier = data.evidence.targets[target_id].tier
        entry = next((item for item in data.shortlist.entries if item.memory_version_id == target_id), None)

    outcome = verdict.outcome
    if outcome in _MUTATION_OUTCOMES and entry is not None:
        candidate_pair = (data.effective_scope.visibility_scope, data.effective_scope.team_id)
        if (entry.visibility_scope, entry.team_id) != candidate_pair:
            raise CurationJudgeError('judge_cross_visibility_denied')

    if outcome == 'publish_new':
        ok = candidate_tier in _SUPPORTED_TIERS and complete
    elif outcome == 'merge_evidence':
        ok = candidate_tier in _SUPPORTED_TIERS and target_tier in _SUPPORTED_TIERS and verdict.applicability == 'same'
    elif outcome == 'revise_memory':
        if entry is not None and entry.has_open_conflict:
            raise CurationJudgeError('judge_policy_denied')

        ok = (
            candidate_tier == 'corroborated'
            and target_tier in _SUPPORTED_TIERS
            and verdict.applicability == 'same'
            and verdict.temporal_order == 'candidate_newer'
            and _candidate_precedes(data, target_id)
        )
    elif outcome == 'supersede_memory':
        if entry is not None and entry.has_open_conflict:
            raise CurationJudgeError('judge_policy_denied')

        ok = (
            candidate_tier == 'corroborated'
            and target_tier in _SUPPORTED_TIERS
            and complete
            and verdict.applicability == 'same'
            and verdict.temporal_order == 'candidate_newer'
            and _candidate_precedes(data, target_id)
        )
    elif outcome == 'reject_candidate' and verdict.relation == 'redundant':
        ok = target_tier in _SUPPORTED_TIERS
    elif outcome == 'reject_candidate':
        ok = candidate_tier == 'none'
    else:
        target_refs = data.evidence.targets[target_id].refs if target_id in data.evidence.targets else ()
        ok = (
            candidate_tier in _SUPPORTED_TIERS
            and target_tier in _SUPPORTED_TIERS
            and bool(data.evidence.candidate.refs)
            and bool(target_refs)
            and complete
            and verdict.applicability == 'same'
            and verdict.temporal_order == 'unordered'
            and not _deterministic_precedence(data, target_id)
        )

    if not ok:
        raise CurationJudgeError('judge_policy_denied')


def parse_curation_judge_verdict(raw: str, data: CurationJudgeInput) -> CurationJudgeVerdictV1:  # noqa: C901
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise CurationJudgeError('judge_invalid_output') from error

    if not isinstance(payload, dict) or set(payload) != _TOP_KEYS:
        raise CurationJudgeError('judge_invalid_output')
    if type(payload['schema_version']) is not int or payload['schema_version'] != 1:
        raise CurationJudgeError('judge_invalid_output')
    if not (
        _is_enum(payload['outcome'], _OUTCOMES)
        and _is_enum(payload['relation'], _RELATIONS)
        and _is_enum(payload['applicability'], _APPLICABILITY)
        and _is_enum(payload['temporal_order'], _TEMPORAL_ORDERS)
        and _is_enum(payload['reason_code'], _REASON_CODES)
    ):
        raise CurationJudgeError('judge_invalid_output')

    version_ids = tuple(entry.memory_version_id for entry in data.shortlist.entries)
    target_raw = payload['target_memory_version_id']
    target_id: uuid.UUID | None = None
    if target_raw is not None:
        target_id = _parse_uuid(target_raw)
        if target_id not in set(version_ids):
            raise CurationJudgeError('judge_invalid_output')

    candidate_refs = _validate_refs(payload['candidate_evidence_refs'], set(data.evidence.candidate.refs))
    comparisons = _validate_comparisons(payload['comparisons'], data, version_ids)

    if target_id is not None:
        selected = next((item for item in comparisons if item.memory_version_id == target_id), None)
        if selected is None or selected.relation != payload['relation']:
            raise CurationJudgeError('judge_invalid_output')
    elif payload['outcome'] in _TARGETLESS_OUTCOMES:
        candidate_pair = (data.effective_scope.visibility_scope, data.effective_scope.team_id)
        entries_by_version = {entry.memory_version_id: entry for entry in data.shortlist.entries}
        for comparison in comparisons:
            if comparison.relation not in _IDENTITY_RELATIONS:
                continue
            entry = entries_by_version.get(comparison.memory_version_id)
            if entry is None or (entry.visibility_scope, entry.team_id) == candidate_pair:
                raise CurationJudgeError('judge_invalid_output')

    reason = payload['reason']
    if not isinstance(reason, str) or not (1 <= len(reason) <= 500) or SECRET_STRING_RE.search(reason):
        raise CurationJudgeError('judge_invalid_output')

    verdict = CurationJudgeVerdictV1(
        schema_version=1,
        outcome=payload['outcome'],
        relation=payload['relation'],
        target_memory_version_id=target_id,
        candidate_evidence_refs=candidate_refs,
        comparisons=comparisons,
        applicability=payload['applicability'],
        temporal_order=payload['temporal_order'],
        reason_code=payload['reason_code'],
        reason=reason,
    )
    _apply_evidence_policy(verdict, data)

    return verdict


def _iter_strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _iter_strings(item)]
    if isinstance(value, (list, tuple)):
        return [text for item in value for text in _iter_strings(item)]

    return []


def _bounded(text: str) -> str:
    return truncate_with_marker(text, _MAX_CLAIM_SNAPSHOT_CHARS)


def build_curation_judge_prompt(data: CurationJudgeInput) -> str:
    candidate_block = {
        'claim': {
            'title': _bounded(data.candidate.title),
            'body': _bounded(data.candidate.body),
            'kind': data.candidate.kind,
        },
        'content_hash': data.candidate.content_hash,
        'evidence_tier': data.evidence.candidate.tier,
        'evidence_refs': list(data.evidence.candidate.refs),
    }
    comparisons = []
    for entry in data.shortlist.entries:
        target = data.evidence.targets.get(entry.memory_version_id)
        comparisons.append(
            {
                'memory_version_id': str(entry.memory_version_id),
                'current_transition_id': str(entry.current_transition_id),
                'visibility_scope': entry.visibility_scope,
                'team_id': str(entry.team_id) if entry.team_id is not None else None,
                'has_open_conflict': entry.has_open_conflict,
                'evidence_tier': target.tier if target is not None else 'none',
                'evidence_refs': list(target.refs) if target is not None else [],
                'claim': {
                    'title': _bounded(entry.title),
                    'body': _bounded(entry.body),
                    'kind': entry.kind,
                    'body_hash': entry.body_hash,
                },
            }
        )
    envelope = {
        'schema': 'curation_judge_input.v1',
        'candidate': candidate_block,
        'effective_scope': {
            'visibility_scope': data.effective_scope.visibility_scope,
            'team_id': str(data.effective_scope.team_id) if data.effective_scope.team_id is not None else None,
        },
        'comparison_manifest_hash': data.shortlist.manifest_hash,
        'authorized_corpus_count': data.shortlist.authorized_corpus_count,
        'comparison_complete': data.shortlist.comparison_complete,
        'comparisons': comparisons,
    }
    redacted = redact_value(envelope).value
    if any(SECRET_STRING_RE.search(text) for text in _iter_strings(redacted)):
        raise CurationJudgeError('judge_invalid_output')

    return json.dumps(redacted, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def _fallback_eligible(error: CurationJudgeError | ModelPolicyError) -> bool:
    if isinstance(error, CurationJudgeError):
        return error.code in {'judge_invalid_output', 'judge_reference_invalid'}

    status = error.http_status
    if status is None:
        return bool(error.retryable) and error.code in {'provider_timeout', 'provider_unreachable'}

    return status in (408, 429) or 500 <= status <= 599


class JudgeCurationCandidate:
    def execute(self, data: CurationJudgeInput) -> CurationJudgeResult:
        team_id = self._candidate_team(data)
        prompt = build_curation_judge_prompt(data)
        primary = self._resolve_policy(data, team_id, 'curation')
        try:
            return self._attempt(data, primary, prompt, team_id, data.request_id, fallback_used=False)
        except (CurationJudgeError, ModelPolicyError) as error:
            if not (getattr(primary, 'fallback_enabled', False) and _fallback_eligible(error)):
                raise

            fallback = self._resolve_policy(data, team_id, 'generation')
            if fallback.id == primary.id:
                raise

            return self._attempt(data, fallback, prompt, team_id, f'{data.request_id}:fallback', fallback_used=True)

    def _candidate_team(self, data: CurationJudgeInput) -> uuid.UUID | None:
        return (
            MemoryCandidate.objects.filter(
                id=data.candidate_id,
                organization_id=data.organization_id,
                project_id=data.project_id,
            )
            .values_list('team_id', flat=True)
            .get()
        )

    def _resolve_policy(self, data: CurationJudgeInput, team_id: uuid.UUID | None, task_type: str) -> object:
        resolved = ResolveModelPolicy().execute(
            ResolveModelPolicyInput(
                organization_id=data.organization_id,
                project_id=data.project_id,
                team_id=team_id,
                task_type=task_type,
            )
        )

        return resolved.policy

    def _attempt(
        self,
        data: CurationJudgeInput,
        policy: object,
        prompt: str,
        team_id: uuid.UUID | None,
        request_id: str,
        *,
        fallback_used: bool,
    ) -> CurationJudgeResult:
        gateway = get_provider_gateway(policy)
        result = gateway.call(
            ProviderCallInput(
                organization_id=data.organization_id,
                project_id=data.project_id,
                team_id=team_id,
                policy=policy,
                request_id=request_id,
                trace_id=data.trace_id,
                prompt=prompt,
                response_kind='curation_decision_v1',
            )
        )
        verdict = parse_curation_judge_verdict(result.generated_body, data)

        return CurationJudgeResult(
            verdict=verdict,
            provider_call_record_id=result.call_record_id,
            policy_id=policy.id,
            policy_version=policy.version,
            response_hash=hashlib.sha256(result.generated_body.encode()).hexdigest(),
            fallback_used=fallback_used,
            comparison_manifest_hash=data.shortlist.manifest_hash,
            authorized_corpus_count=data.shortlist.authorized_corpus_count,
            comparison_complete=data.shortlist.comparison_complete,
        )
