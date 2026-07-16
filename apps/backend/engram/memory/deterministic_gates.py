from __future__ import annotations

import hashlib
import re
import unicodedata
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from django.core.exceptions import ObjectDoesNotExist
from django.db import DatabaseError
from django.db.models import Exists, F, OuterRef

from engram.core.models import (
    CurationReasonCode,
    MemoryCandidate,
    MemoryCandidateSource,
    MemoryConflict,
    MemoryStatus,
    MemoryVersion,
    MemoryVersionSource,
    VisibilityScope,
    WorkflowSubjectType,
    WorkflowWork,
    WorkflowWorkDisposition,
    WorkflowWorkType,
)
from engram.core.redaction import REDACTED_VALUE, SECRET_STRING_RE, redact_value
from engram.memory.candidate_decision_work import (
    CandidateDecisionWorkScopeError,
    build_candidate_decision_input,
    candidate_decision_snapshot,
)
from engram.memory.workflow_work import canonical_json_bytes, work_input_fingerprint

DETERMINISTIC_POLICY_VERSION = 'deterministic_policy.v1'
_SNAPSHOT_KEYS = frozenset(
    {
        'schema',
        'candidate_id',
        'candidate_content_hash',
        'organization_id',
        'project_id',
        'team_id',
        'evidence_manifest_hash',
        'policy_version',
    }
)
_LIFECYCLE_TYPES = frozenset({'session_start', 'session_end', 'session_lifecycle'})
_WHITESPACE_RE = re.compile(r'\s+')


class DeterministicGateDisposition(StrEnum):
    CONTINUE = 'continue'
    TERMINAL = 'terminal'
    RETRY = 'retry'


class DeterministicTerminalOutcome(StrEnum):
    MERGE_EVIDENCE = 'merge_evidence'
    REJECT_CANDIDATE = 'reject_candidate'


@dataclass(frozen=True, slots=True)
class EffectiveCandidateScope:
    visibility_scope: str
    team_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class SanitizedCandidateView:
    title: str
    body: str
    kind: str
    evidence: tuple[dict[str, object], ...]
    content_hash: str
    redaction_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DeterministicGateResult:
    disposition: DeterministicGateDisposition
    policy_version: str
    sanitized_candidate: SanitizedCandidateView | None = None
    effective_scope: EffectiveCandidateScope | None = None
    terminal_outcome: DeterministicTerminalOutcome | None = None
    reason_code: str | None = None
    operational_reason: str | None = None
    target_memory_version_id: uuid.UUID | None = None
    requires_transition: bool | None = None

    def __post_init__(self) -> None:  # noqa: C901
        if self.policy_version != DETERMINISTIC_POLICY_VERSION:
            raise ValueError('unsupported deterministic policy version')
        if self.disposition == DeterministicGateDisposition.CONTINUE:
            if self.sanitized_candidate is None or self.effective_scope is None:
                raise ValueError('continue requires sanitized candidate and effective scope')
            if any(
                value is not None
                for value in (
                    self.terminal_outcome,
                    self.reason_code,
                    self.operational_reason,
                    self.target_memory_version_id,
                    self.requires_transition,
                )
            ):
                raise ValueError('continue cannot carry terminal or operational fields')
        elif self.disposition == DeterministicGateDisposition.TERMINAL:
            if self.sanitized_candidate is None or self.effective_scope is None:
                raise ValueError('terminal requires sanitized candidate and effective scope')
            if self.terminal_outcome is None or not self.reason_code or self.operational_reason is not None:
                raise ValueError('terminal requires semantic outcome and reason')
            if self.terminal_outcome == DeterministicTerminalOutcome.REJECT_CANDIDATE:
                if self.target_memory_version_id is not None or self.requires_transition is not None:
                    raise ValueError('rejection cannot target a memory transition')
            elif self.terminal_outcome == DeterministicTerminalOutcome.MERGE_EVIDENCE:
                if self.target_memory_version_id is None or self.requires_transition is None:
                    raise ValueError('merge requires target and transition flag')
            else:
                raise ValueError('unsupported deterministic terminal outcome')
        elif self.disposition == DeterministicGateDisposition.RETRY:
            if not self.operational_reason or any(
                value is not None
                for value in (
                    self.sanitized_candidate,
                    self.effective_scope,
                    self.terminal_outcome,
                    self.reason_code,
                    self.target_memory_version_id,
                    self.requires_transition,
                )
            ):
                raise ValueError('retry carries only an operational reason')
        else:
            raise ValueError('unsupported deterministic gate disposition')


def _retry(reason: str) -> DeterministicGateResult:
    return DeterministicGateResult(
        disposition=DeterministicGateDisposition.RETRY,
        policy_version=DETERMINISTIC_POLICY_VERSION,
        operational_reason=reason,
    )


def _normalize(value: object) -> str:
    return _WHITESPACE_RE.sub(' ', unicodedata.normalize('NFKC', str(value)).casefold()).strip()


def _strings(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _strings(item)


def _contains_key(value: object, wanted: str) -> bool:
    if isinstance(value, dict):
        return wanted in value or any(_contains_key(item, wanted) for item in value.values())
    if isinstance(value, list | tuple):
        return any(_contains_key(item, wanted) for item in value)
    return False


def _evidence_entries(value: object) -> list[object]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    return []


def _field_changed(before: object, after: object) -> bool:
    return canonical_json_bytes(before) != canonical_json_bytes(after)


def _redact_candidate(candidate: MemoryCandidate) -> SanitizedCandidateView:
    title_result = redact_value(candidate.title)
    body_result = redact_value(candidate.body)
    kind_result = redact_value(candidate.kind)
    evidence_result = redact_value(candidate.evidence)
    evidence = evidence_result.value
    if not isinstance(evidence, list):
        evidence = [evidence] if isinstance(evidence, dict) else []

    codes: set[str] = set()
    if title_result.redacted:
        codes.add('title')
    if body_result.redacted:
        codes.add('body')
    if evidence_result.redacted:
        codes.add('evidence')
    if _contains_key(candidate.evidence, 'exact_terms') and _field_changed(
        _extract_key(candidate.evidence, 'exact_terms'), _extract_key(evidence, 'exact_terms')
    ):
        codes.add('exact_terms')
    if _contains_key(candidate.evidence, 'file_paths') and _field_changed(
        _extract_key(candidate.evidence, 'file_paths'), _extract_key(evidence, 'file_paths')
    ):
        codes.add('file_paths')

    sanitized = {
        'title': str(title_result.value),
        'body': str(body_result.value),
        'kind': str(kind_result.value),
        'evidence': evidence,
    }
    content_hash = hashlib.sha256(canonical_json_bytes(sanitized)).hexdigest()
    return SanitizedCandidateView(
        title=sanitized['title'],
        body=sanitized['body'],
        kind=sanitized['kind'],
        evidence=tuple(item for item in evidence if isinstance(item, dict)),
        content_hash=content_hash,
        redaction_codes=tuple(sorted(codes)),
    )


def _extract_key(value: object, wanted: str) -> object:
    found: list[object] = []
    if isinstance(value, dict):
        if wanted in value:
            found.append(value[wanted])
        for item in value.values():
            found.append(_extract_key(item, wanted))
    elif isinstance(value, list | tuple):
        for item in value:
            found.append(_extract_key(item, wanted))
    return found


def _scope_for(candidate: MemoryCandidate, sources: list[MemoryCandidateSource]) -> EffectiveCandidateScope:
    if candidate.visibility_scope == VisibilityScope.SESSION:
        return EffectiveCandidateScope(VisibilityScope.PROJECT, None)
    source_teams = {source.team_id for source in sources}
    if candidate.visibility_scope == VisibilityScope.PROJECT:
        return EffectiveCandidateScope(VisibilityScope.PROJECT, None)
    if candidate.visibility_scope == VisibilityScope.TEAM:
        if not sources and candidate.team_id is not None:
            return EffectiveCandidateScope(VisibilityScope.TEAM, candidate.team_id)
        if candidate.team_id is None or source_teams != {candidate.team_id}:
            raise CandidateDecisionWorkScopeError('team scope is not preserved by evidence')
        return EffectiveCandidateScope(VisibilityScope.TEAM, candidate.team_id)
    if candidate.visibility_scope == VisibilityScope.ORGANIZATION:
        if len(source_teams) == 1 and None not in source_teams:
            return EffectiveCandidateScope(VisibilityScope.TEAM, next(iter(source_teams)))
        return EffectiveCandidateScope(VisibilityScope.PROJECT, None)
    raise CandidateDecisionWorkScopeError('unsupported candidate visibility scope')


def _validate_sources(candidate: MemoryCandidate, sources: list[MemoryCandidateSource]) -> None:  # noqa: C901
    expected_scope = (candidate.organization_id, candidate.project_id, candidate.team_id)
    for source in sources:
        if (
            source.candidate_id != candidate.id
            or (source.organization_id, source.project_id, source.team_id) != expected_scope
        ):
            raise CandidateDecisionWorkScopeError('candidate source has foreign scope')
        observation = source.observation
        if (observation.organization_id, observation.project_id, observation.team_id) != expected_scope:
            raise CandidateDecisionWorkScopeError('candidate source observation has foreign scope')
        if source.source_kind == 'distillation':
            if source.window is None or source.stage is None or source.import_source is not None:
                raise CandidateDecisionWorkScopeError('invalid distillation source relation')
            if (source.window.organization_id, source.window.project_id, source.window.team_id) != expected_scope:
                raise CandidateDecisionWorkScopeError('candidate source window has foreign scope')
            if (source.stage.organization_id, source.stage.project_id, source.stage.team_id) != expected_scope:
                raise CandidateDecisionWorkScopeError('candidate source stage has foreign scope')
            if source.stage.window_id != source.window_id:
                raise CandidateDecisionWorkScopeError('candidate source stage has impossible window relation')
        elif source.source_kind == 'import':
            if source.window is not None or source.stage is not None or source.import_source is None:
                raise CandidateDecisionWorkScopeError('invalid import source relation')
            if (source.import_source.organization_id, source.import_source.project_id) != expected_scope[:2]:
                raise CandidateDecisionWorkScopeError('candidate import source has foreign scope')
            if source.import_source.observation_id != source.observation_id:
                raise CandidateDecisionWorkScopeError('candidate import source has impossible observation relation')
        else:
            raise CandidateDecisionWorkScopeError('unsupported candidate source kind')


def _claim_bytes(view: SanitizedCandidateView, scope: EffectiveCandidateScope) -> bytes:
    return canonical_json_bytes(
        {
            'title': _normalize(view.title),
            'body': _normalize(view.body),
            'kind': _normalize(view.kind),
            'visibility_scope': scope.visibility_scope,
            'team_id': str(scope.team_id) if scope.team_id is not None else None,
        }
    )


def _active_versions(candidate: MemoryCandidate, scope: EffectiveCandidateScope) -> list[MemoryVersion]:
    active = MemoryVersion.objects.filter(
        memory__current_version=F('version'),
        memory__status__in=(MemoryStatus.APPROVED, MemoryStatus.CONFLICT),
        memory__stale=False,
        memory__refuted=False,
    ).select_related('memory', 'memory__team')
    version_scoped = list(
        active.filter(
            organization_id=candidate.organization_id,
            project_id=candidate.project_id,
        )
    )
    for version in version_scoped:
        if (
            version.memory.organization_id != candidate.organization_id
            or version.memory.project_id != candidate.project_id
            or (version.memory.team_id is not None and version.memory.team.organization_id != candidate.organization_id)
        ):
            raise CandidateDecisionWorkScopeError('current memory version has foreign scope')

    memory_scoped = list(
        active.filter(
            memory__organization_id=candidate.organization_id,
            memory__project_id=candidate.project_id,
        )
    )
    if any(
        version.organization_id != candidate.organization_id or version.project_id != candidate.project_id
        for version in memory_scoped
    ):
        raise CandidateDecisionWorkScopeError('current memory version scope disagrees with memory scope')

    query = active.filter(
        organization_id=candidate.organization_id,
        project_id=candidate.project_id,
        memory__organization_id=candidate.organization_id,
        memory__project_id=candidate.project_id,
    )
    if scope.visibility_scope == VisibilityScope.PROJECT:
        query = query.filter(memory__visibility_scope=VisibilityScope.PROJECT)
    elif scope.visibility_scope == VisibilityScope.TEAM:
        query = query.filter(memory__visibility_scope=VisibilityScope.TEAM, memory__team_id=scope.team_id)
    else:
        query = query.none()
    open_conflict = MemoryConflict.objects.filter(memory_version_id=OuterRef('pk'), resolved_transition__isnull=True)
    query = query.filter(~Exists(open_conflict))
    return list(query)


class EvaluateDeterministicCandidateGates:
    def execute(self, work_id: uuid.UUID) -> DeterministicGateResult:  # noqa: C901
        try:
            work = WorkflowWork.objects.get(id=work_id)
        except WorkflowWork.DoesNotExist:
            return _retry('stale_decision')
        except DatabaseError:
            return _retry('evidence_unavailable')

        try:
            candidate, sources, scope = self._load_candidate(work)
            view = _redact_candidate(candidate)
            if candidate.visibility_scope == VisibilityScope.SESSION:
                return self._reject(view, scope, CurationReasonCode.NON_DURABLE_SESSION_SCOPE)
            if not sources:
                return self._reject(view, scope, CurationReasonCode.UNSUPPORTED_PROVENANCE)
            if any(
                SECRET_STRING_RE.search(item)
                for item in _strings({'title': view.title, 'body': view.body, 'evidence': view.evidence})
            ):
                return self._reject(view, scope, CurationReasonCode.UNSAFE_CONTENT_AFTER_REDACTION)
            result = self._evaluate_noise(candidate, sources, view, scope)
            if result is not None:
                return result
            claim = _claim_bytes(view, scope)
            claim_hash = hashlib.sha256(claim).hexdigest()
            matches: list[MemoryVersion] = []
            for version in _active_versions(candidate, scope):
                memory_claim = canonical_json_bytes(
                    {
                        'title': _normalize(version.memory.title),
                        'body': _normalize(version.body),
                        'kind': _normalize(version.memory.kind),
                        'visibility_scope': scope.visibility_scope,
                        'team_id': str(scope.team_id) if scope.team_id is not None else None,
                    }
                )
                memory_hash = hashlib.sha256(memory_claim).hexdigest()
                if memory_hash == claim_hash and memory_claim != claim:
                    return _retry('stale_decision')
                if memory_hash == claim_hash and memory_claim == claim:
                    matches.append(version)
            if len(matches) > 1:
                return _retry('stale_decision')
            if not matches:
                return DeterministicGateResult(
                    disposition=DeterministicGateDisposition.CONTINUE,
                    policy_version=DETERMINISTIC_POLICY_VERSION,
                    sanitized_candidate=view,
                    effective_scope=scope,
                )
            target = matches[0]
            attached = set(
                MemoryVersionSource.objects.filter(
                    memory_version_id=target.id,
                    candidate_source__isnull=False,
                ).values_list('candidate_source_id', flat=True)
            )
            missing = {source.id for source in sources} - attached
            reason = (
                CurationReasonCode.EXACT_IDENTITY if missing else CurationReasonCode.EXACT_DUPLICATE_NO_NEW_EVIDENCE
            )
            return self._merge(view, scope, reason, target.id, bool(missing))
        except DatabaseError:
            return _retry('evidence_unavailable')
        except (
            CandidateDecisionWorkScopeError,
            ObjectDoesNotExist,
            ValueError,
            KeyError,
            AttributeError,
            TypeError,
        ):
            return _retry('stale_decision')

    def _load_candidate(  # noqa: C901
        self, work: WorkflowWork
    ) -> tuple[MemoryCandidate, list[MemoryCandidateSource], EffectiveCandidateScope]:
        if (
            work.work_type != WorkflowWorkType.CANDIDATE_DECISION
            or work.subject_type != WorkflowSubjectType.MEMORY_CANDIDATE
            or work.contract_version != 1
            or work.disposition != WorkflowWorkDisposition.REQUIRED
            or not isinstance(work.input_snapshot, dict)
            or set(work.input_snapshot) != _SNAPSHOT_KEYS
        ):
            raise CandidateDecisionWorkScopeError('invalid candidate decision work')
        snapshot = work.input_snapshot
        if snapshot.get('schema') != 'candidate_decision_input/v1' or snapshot.get('policy_version') != 1:
            raise CandidateDecisionWorkScopeError('invalid candidate decision snapshot')
        candidate_id = uuid.UUID(str(snapshot['candidate_id']))
        if candidate_id != work.subject_id:
            raise CandidateDecisionWorkScopeError('work candidate does not match snapshot')
        if snapshot.get('organization_id') != str(work.organization_id) or snapshot.get('project_id') != str(
            work.project_id
        ):
            raise CandidateDecisionWorkScopeError('work scope does not match snapshot')
        candidate = MemoryCandidate.objects.get(
            id=candidate_id,
            organization_id=work.organization_id,
            project_id=work.project_id,
        )
        if candidate.decision_work_contract_version != 1 or candidate.content_hash != snapshot.get(
            'candidate_content_hash'
        ):
            raise CandidateDecisionWorkScopeError('candidate snapshot is stale')
        if (
            snapshot.get('team_id') != (str(candidate.team_id) if candidate.team_id else None)
            or work.team_id != candidate.team_id
        ):
            raise CandidateDecisionWorkScopeError('candidate team does not match work')
        sources = list(
            MemoryCandidateSource.objects.filter(candidate_id=candidate.id).select_related(
                'window', 'observation', 'stage', 'import_source'
            )
        )
        _validate_sources(candidate, sources)
        expected = build_candidate_decision_input(candidate, sources=sources)
        if candidate_decision_snapshot(expected) != snapshot:
            raise CandidateDecisionWorkScopeError('candidate decision manifest is stale')
        expected_fingerprint = work_input_fingerprint(
            work_type=work.work_type,
            subject_type=work.subject_type,
            subject_id=work.subject_id,
            contract_version=work.contract_version,
            occurrence_key=work.occurrence_key,
            input_snapshot=snapshot,
        )
        if expected_fingerprint != work.input_fingerprint:
            raise CandidateDecisionWorkScopeError('candidate decision fingerprint is stale')
        observation_ids = {str(source.observation_id) for source in sources}
        for entry in _evidence_entries(candidate.evidence):
            if isinstance(entry, dict):
                values = entry.get('supporting_observation_ids', [])
                if isinstance(values, str):
                    values = [values]
                if isinstance(values, list) and any(str(item) not in observation_ids for item in values):
                    raise CandidateDecisionWorkScopeError('candidate evidence references missing observation')
        return candidate, sources, _scope_for(candidate, sources)

    def _evaluate_noise(
        self,
        candidate: MemoryCandidate,
        sources: list[MemoryCandidateSource],
        view: SanitizedCandidateView,
        scope: EffectiveCandidateScope,
    ) -> DeterministicGateResult | None:
        title = _normalize(view.title)
        body = _normalize(view.body)
        if not title and not body:
            return self._reject(view, scope, CurationReasonCode.NOISE_EMPTY)
        if (
            not title.replace(_normalize(REDACTED_VALUE), '')
            and not body.replace(_normalize(REDACTED_VALUE), '')
            and not ({'title', 'body'} & set(view.redaction_codes))
        ):
            return self._reject(view, scope, CurationReasonCode.NOISE_REDACTION_ONLY)
        if not title.replace(_normalize(REDACTED_VALUE), '') and not body.replace(_normalize(REDACTED_VALUE), ''):
            return self._reject(view, scope, CurationReasonCode.UNSAFE_CONTENT_AFTER_REDACTION)
        if title and title == body:
            return self._reject(view, scope, CurationReasonCode.NOISE_TITLE_ECHO)
        entries = _evidence_entries(candidate.evidence)
        for entry in entries:
            if isinstance(entry, dict) and entry.get('parse_fallback') is True:
                values = entry.get('supporting_observation_ids', [])
                if isinstance(values, str):
                    values = [values]
                matching = any(str(item) == str(source.observation_id) for item in values for source in sources)
                if not matching:
                    return self._reject(view, scope, CurationReasonCode.NOISE_PARSE_WRAPPER)
        if sources and all(
            source.observation.observation_type in _LIFECYCLE_TYPES
            or source.observation.source_metadata.get('event_type') in _LIFECYCLE_TYPES
            for source in sources
        ):
            return self._reject(view, scope, CurationReasonCode.NOISE_LIFECYCLE_ONLY)
        return None

    def _reject(
        self, view: SanitizedCandidateView, scope: EffectiveCandidateScope, reason: str
    ) -> DeterministicGateResult:
        return DeterministicGateResult(
            disposition=DeterministicGateDisposition.TERMINAL,
            policy_version=DETERMINISTIC_POLICY_VERSION,
            sanitized_candidate=view,
            effective_scope=scope,
            terminal_outcome=DeterministicTerminalOutcome.REJECT_CANDIDATE,
            reason_code=reason,
        )

    def _merge(
        self,
        view: SanitizedCandidateView,
        scope: EffectiveCandidateScope,
        reason: str,
        target_id: uuid.UUID,
        requires_transition: bool,
    ) -> DeterministicGateResult:
        return DeterministicGateResult(
            disposition=DeterministicGateDisposition.TERMINAL,
            policy_version=DETERMINISTIC_POLICY_VERSION,
            sanitized_candidate=view,
            effective_scope=scope,
            terminal_outcome=DeterministicTerminalOutcome.MERGE_EVIDENCE,
            reason_code=reason,
            target_memory_version_id=target_id,
            requires_transition=requires_transition,
        )
