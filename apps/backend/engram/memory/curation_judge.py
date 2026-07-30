from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime

import structlog

from engram.core.models import (
    MemoryCandidate,
    MemoryCandidateSource,
    MemoryCandidateSourceKind,
    MemoryVersionSource,
)
from engram.core.redaction import SECRET_STRING_RE, redact_value
from engram.memory.candidate_parsing import strip_json_fence, truncate_with_marker
from engram.memory.curation_derivation import (
    DerivationFacts,
    DerivedDecision,
    build_derivation_facts,
    derive_decision,
    feasible_outcomes,
)
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

logger = structlog.get_logger(__name__)


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
    attempt: int = 0


@dataclass(frozen=True, slots=True)
class CurationJudgeComparison:
    memory_version_id: uuid.UUID
    relation: str
    applicability: str


@dataclass(frozen=True, slots=True)
class CurationJudgeVerdict:
    schema_version: int
    outcome: str
    relation: str
    target_memory_version_id: uuid.UUID | None
    comparisons: tuple[CurationJudgeComparison, ...]
    applicability: str
    temporal_order: str
    reason_code: str
    reason: str


@dataclass(frozen=True, slots=True)
class CurationJudgeResult:
    verdict: CurationJudgeVerdict
    provider_call_record_id: uuid.UUID
    policy_id: uuid.UUID
    policy_version: int
    response_hash: str
    fallback_used: bool
    comparison_manifest_hash: str
    authorized_corpus_count: int
    comparison_complete: bool


_SCHEMA_VERSION = 2
_RESPONSE_KIND = 'curation_decision_v2'
_LIFECYCLE_TYPES = frozenset({'session_start', 'session_end', 'session_lifecycle'})
_GROUP_TOKEN_PREFIX = 'curation-evidence-group:v1:'
_MAX_EVIDENCE_REFS = 16
_MAX_CLAIM_SNAPSHOT_CHARS = 2000

_TOP_KEYS = frozenset({'schema_version', 'comparisons', 'reason'})
_COMPARISON_KEYS = frozenset({'index', 'relation', 'applicability'})
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
_APPLICABILITY = frozenset({'same', 'different'})
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

_CURATION_JUDGE_SYSTEM_PROMPT = (
    'You are the memory curation judge. The user message is a curation_judge_input.v2 JSON envelope, and the '
    'accompanying instructions define the exact output contract you must follow. '
    'Report how the candidate relates to every listed comparison; the system derives the outcome from your '
    'relations and from facts you cannot see.'
)


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


def _eligible_group_pair(source: MemoryCandidateSource) -> tuple[str, str] | None:
    if source.source_kind == MemoryCandidateSourceKind.AGENT_PROPOSAL:
        from engram.memory.import_provenance import ImportProvenanceError, _validated_agent_anchors

        try:
            _validated_agent_anchors(source)
        except ImportProvenanceError as error:
            raise CurationJudgeError('transition_dependency_unavailable') from error

        return source.anchors_hash, source.anchors_hash
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
    digest = observation_content_digest(observation)
    if anchors.get('observation_digest') != digest:
        raise CurationJudgeError('transition_dependency_unavailable')

    return source.window.input_hash, digest


def _independence_group_hashes(pairs: list[tuple[str, str]]) -> set[str]:
    windows: set[str] = set()
    observation_windows: dict[str, list[str]] = {}
    for window_hash, observation_digest in pairs:
        windows.add(window_hash)
        observation_windows.setdefault(observation_digest, []).append(window_hash)

    parent = {window_hash: window_hash for window_hash in windows}

    def find(node: str) -> str:
        root = node
        while parent[root] != root:
            root = parent[root]
        while parent[node] != root:
            parent[node], node = root, parent[node]

        return root

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    for shared_windows in observation_windows.values():
        first = shared_windows[0]
        for other in shared_windows[1:]:
            union(first, other)

    return {find(window_hash) for window_hash in windows}


def _candidate_group_hashes(candidate_id: uuid.UUID) -> tuple[set[str], datetime | None]:
    sources = MemoryCandidateSource.objects.select_related('window', 'observation', 'stage').filter(
        candidate_id=candidate_id
    )
    pairs: list[tuple[str, str]] = []
    latest: datetime | None = None
    for source in sources:
        pair = _eligible_group_pair(source)
        if pair is not None:
            pairs.append(pair)
            latest = _newer(latest, _source_evidence_time(source))

    return _independence_group_hashes(pairs), latest


def _traverse_target(
    version_id: uuid.UUID,
    pairs: list[tuple[str, str]],
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
            pair = _eligible_group_pair(row.candidate_source)
            if pair is not None:
                pairs.append(pair)
                moment = _source_evidence_time(row.candidate_source)
                if moment is not None:
                    times.append(moment)
        elif row.source_memory_version_id is not None:
            _traverse_target(row.source_memory_version_id, pairs, times, path, resolved)

    path.discard(version_id)
    resolved.add(version_id)


def _target_evidence(version_id: uuid.UUID) -> ClaimEvidence:
    pairs: list[tuple[str, str]] = []
    times: list[datetime] = []
    _traverse_target(version_id, pairs, times, set(), set())

    return _claim_evidence(_independence_group_hashes(pairs), max(times) if times else None)


def build_curation_evidence_context(candidate_id: uuid.UUID, shortlist: CurationShortlist) -> CurationEvidenceContext:
    candidate_hashes, candidate_latest = _candidate_group_hashes(candidate_id)
    candidate = _claim_evidence(candidate_hashes, candidate_latest)
    targets = {entry.memory_version_id: _target_evidence(entry.memory_version_id) for entry in shortlist.entries}

    return CurationEvidenceContext(candidate=candidate, targets=targets)


def _is_enum(value: object, allowed: frozenset[str]) -> bool:
    return isinstance(value, str) and value in allowed


def _parse_index(value: object, count: int) -> int:
    if type(value) is not int or not (1 <= value <= count):
        raise CurationJudgeError('judge_invalid_output')

    return value


def _validate_comparisons(
    value: object,
    version_ids: tuple[uuid.UUID, ...],
) -> tuple[CurationJudgeComparison, ...]:
    if not isinstance(value, list):
        raise CurationJudgeError('judge_invalid_output')

    parsed: dict[int, CurationJudgeComparison] = {}
    for item in value:
        if not isinstance(item, dict) or set(item) != _COMPARISON_KEYS:
            raise CurationJudgeError('judge_invalid_output')
        if not _is_enum(item['relation'], _RELATIONS) or not _is_enum(item['applicability'], _APPLICABILITY):
            raise CurationJudgeError('judge_invalid_output')
        index = _parse_index(item['index'], len(version_ids))
        if index in parsed:
            raise CurationJudgeError('judge_invalid_output')
        parsed[index] = CurationJudgeComparison(version_ids[index - 1], item['relation'], item['applicability'])

    if len(parsed) != len(version_ids):
        raise CurationJudgeError('judge_invalid_output')

    return tuple(parsed[index] for index in sorted(parsed))


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


def _apply_evidence_policy(verdict: CurationJudgeVerdict, data: CurationJudgeInput) -> None:  # noqa: C901
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
        if entry is not None and entry.has_open_conflict:
            raise CurationJudgeError('judge_policy_denied')

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


def parse_curation_judge_verdict(raw: str, data: CurationJudgeInput) -> CurationJudgeVerdict:
    try:
        payload = json.loads(strip_json_fence(raw))
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise CurationJudgeError('judge_invalid_output') from error

    if not isinstance(payload, dict) or set(payload) != _TOP_KEYS:
        raise CurationJudgeError('judge_invalid_output')
    if type(payload['schema_version']) is not int or payload['schema_version'] != _SCHEMA_VERSION:
        raise CurationJudgeError('judge_invalid_output')

    version_ids = tuple(entry.memory_version_id for entry in data.shortlist.entries)
    comparisons = _validate_comparisons(payload['comparisons'], version_ids)

    reason = payload['reason']
    if not isinstance(reason, str) or not (1 <= len(reason) <= 500) or SECRET_STRING_RE.search(reason):
        raise CurationJudgeError('judge_invalid_output')

    facts = build_derivation_facts(data)
    relations = {item.memory_version_id: item.relation for item in comparisons}
    applicability = {item.memory_version_id: item.applicability for item in comparisons}
    decision = derive_decision(facts, relations, applicability)
    if decision is None:
        raise CurationJudgeError('curation_infeasible')

    verdict = CurationJudgeVerdict(
        schema_version=_SCHEMA_VERSION,
        outcome=decision.outcome,
        relation=decision.relation,
        target_memory_version_id=decision.target_memory_version_id,
        comparisons=comparisons,
        applicability=decision.applicability,
        temporal_order=decision.temporal_order,
        reason_code=decision.reason_code,
        reason=reason,
    )
    _apply_evidence_policy(verdict, data)
    _log_derived_decision(decision, facts)

    return verdict


def _log_derived_decision(decision: DerivedDecision, facts: DerivationFacts) -> None:
    target = next(
        (item for item in facts.targets if item.memory_version_id == decision.target_memory_version_id),
        None,
    )
    logger.info(
        'curation_decision_derived',
        outcome=decision.outcome,
        rung=decision.rung,
        candidate_tier=facts.candidate_tier,
        target_tier=target.tier if target is not None else 'none',
        comparison_complete=facts.comparison_complete,
        suppressed_identity_relation_count=len(decision.suppressed_identity_relations),
    )


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
    }
    comparisons = []
    for index, entry in enumerate(data.shortlist.entries, start=1):
        target = data.evidence.targets.get(entry.memory_version_id)
        comparisons.append(
            {
                'index': index,
                'visibility_scope': entry.visibility_scope,
                'team_id': str(entry.team_id) if entry.team_id is not None else None,
                'has_open_conflict': entry.has_open_conflict,
                'evidence_tier': target.tier if target is not None else 'none',
                'claim': {
                    'title': _bounded(entry.title),
                    'body': _bounded(entry.body),
                    'kind': entry.kind,
                    'body_hash': entry.body_hash,
                },
            }
        )
    envelope = {
        'schema': 'curation_judge_input.v2',
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
        return error.code == 'judge_invalid_output'

    status = error.http_status
    if status is None:
        return bool(error.retryable) and error.code in {'provider_timeout', 'provider_unreachable'}

    return status in (408, 429) or 500 <= status <= 599


class JudgeCurationCandidate:
    def execute(self, data: CurationJudgeInput) -> CurationJudgeResult:
        if not feasible_outcomes(build_derivation_facts(data)):
            raise CurationJudgeError('curation_infeasible')

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
                system_prompt=_CURATION_JUDGE_SYSTEM_PROMPT,
                response_kind=_RESPONSE_KIND,
                attempt=data.attempt,
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
