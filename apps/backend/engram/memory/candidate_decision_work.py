from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol

from django.db import transaction

from engram.core.models import (
    MemoryCandidate,
    MemoryCandidateSource,
    WorkflowSubjectType,
    WorkflowWork,
    WorkflowWorkType,
)
from engram.memory.workflow_work import (
    CreateWorkflowWorkInput,
    canonical_json_bytes,
    create_work,
    observation_content_digest,
    work_input_fingerprint,
)

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
_SOURCE_KEYS = (
    'window_input_hash',
    'session_sequence',
    'observation_id',
    'observation_digest',
    'stage_key',
    'anchors_hash',
)
_SHA256_LENGTH = 64


class CandidateDecisionWorkScopeError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CandidateDecisionWorkInput:
    candidate_id: uuid.UUID
    candidate_content_hash: str
    organization_id: uuid.UUID
    project_id: uuid.UUID
    team_id: uuid.UUID | None
    evidence_manifest_hash: str
    policy_version: int


class CandidateDecisionWorkBuilder(Protocol):
    def expected_input(self, *, candidate_id: uuid.UUID) -> CandidateDecisionWorkInput: ...

    def exact_work(self, *, value: CandidateDecisionWorkInput) -> WorkflowWork | None: ...


def _require_sha256(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != _SHA256_LENGTH
        or any(character not in '0123456789abcdef' for character in value)
    ):
        raise ValueError(f'{label} must be lowercase SHA-256')

    return value


def _source_value(
    source: MemoryCandidateSource | Mapping[str, object],
    candidate: MemoryCandidate,
) -> dict[str, object]:
    if isinstance(source, MemoryCandidateSource):
        if source.candidate_id != candidate.id:
            raise CandidateDecisionWorkScopeError('candidate source belongs to another candidate')
        if (
            source.organization_id,
            source.project_id,
            source.team_id,
        ) != (candidate.organization_id, candidate.project_id, candidate.team_id):
            raise CandidateDecisionWorkScopeError('candidate source scope does not match candidate')
        window = source.window
        observation = source.observation
        stage = source.stage
        if (
            (window.organization_id, window.project_id, window.team_id)
            != (candidate.organization_id, candidate.project_id, candidate.team_id)
            or (observation.organization_id, observation.project_id, observation.team_id)
            != (candidate.organization_id, candidate.project_id, candidate.team_id)
            or (stage.organization_id, stage.project_id, stage.team_id)
            != (candidate.organization_id, candidate.project_id, candidate.team_id)
        ):
            raise CandidateDecisionWorkScopeError('candidate source relation has foreign scope')
        value = {
            'window_input_hash': window.input_hash,
            'session_sequence': observation.session_sequence,
            'observation_id': str(observation.id),
            'observation_digest': observation_content_digest(observation),
            'stage_key': stage.stage_key,
            'anchors_hash': source.anchors_hash,
        }
    else:
        if set(source) != set(_SOURCE_KEYS):
            raise ValueError('candidate evidence entry has unexpected or missing fields')
        value = dict(source)

    for field in ('window_input_hash', 'observation_digest', 'stage_key', 'anchors_hash'):
        _require_sha256(value[field], f'candidate evidence {field}')
    sequence = value['session_sequence']
    if type(sequence) is not int or sequence <= 0:
        raise ValueError('candidate evidence session sequence must be positive')
    try:
        uuid.UUID(str(value['observation_id']))
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError('candidate evidence observation id must be a UUID') from error

    return value


def _candidate_sources(candidate: MemoryCandidate) -> list[MemoryCandidateSource]:
    return list(
        MemoryCandidateSource.objects.filter(candidate_id=candidate.id).select_related('window', 'observation', 'stage')
    )


def evidence_manifest(
    candidate: MemoryCandidate,
    *,
    sources: Iterable[MemoryCandidateSource | Mapping[str, object]] | None = None,
) -> tuple[list[dict[str, object]], str]:
    selected_sources = sources if sources is not None else _candidate_sources(candidate)
    entries = [_source_value(source, candidate) for source in selected_sources]
    entries.sort(key=lambda value: tuple(value[field] for field in _SOURCE_KEYS))
    ordered = list(entries)

    return ordered, hashlib.sha256(canonical_json_bytes(ordered)).hexdigest()


def build_candidate_decision_input(
    candidate: MemoryCandidate,
    *,
    sources: Iterable[MemoryCandidateSource | Mapping[str, object]] | None = None,
    policy_version: int = 1,
) -> CandidateDecisionWorkInput:
    if not isinstance(candidate, MemoryCandidate):
        raise TypeError('candidate must be a MemoryCandidate')
    if type(policy_version) is not int or policy_version != 1:
        raise ValueError('candidate decision policy version must be 1')
    candidate_content_hash = _require_sha256(candidate.content_hash, 'candidate content hash')
    _manifest, manifest_hash = evidence_manifest(candidate, sources=sources)

    return CandidateDecisionWorkInput(
        candidate_id=candidate.id,
        candidate_content_hash=candidate_content_hash,
        organization_id=candidate.organization_id,
        project_id=candidate.project_id,
        team_id=candidate.team_id,
        evidence_manifest_hash=manifest_hash,
        policy_version=policy_version,
    )


def candidate_decision_snapshot(value: CandidateDecisionWorkInput) -> dict[str, object]:
    return {
        'schema': 'candidate_decision_input/v1',
        'candidate_id': str(value.candidate_id),
        'candidate_content_hash': value.candidate_content_hash,
        'organization_id': str(value.organization_id),
        'project_id': str(value.project_id),
        'team_id': str(value.team_id) if value.team_id is not None else None,
        'evidence_manifest_hash': value.evidence_manifest_hash,
        'policy_version': value.policy_version,
    }


def _work_data(value: CandidateDecisionWorkInput) -> CreateWorkflowWorkInput:
    return CreateWorkflowWorkInput(
        organization_id=value.organization_id,
        project_id=value.project_id,
        work_type=WorkflowWorkType.CANDIDATE_DECISION,
        subject_type=WorkflowSubjectType.MEMORY_CANDIDATE,
        subject_id=value.candidate_id,
        input_snapshot=candidate_decision_snapshot(value),
    )


class DatabaseCandidateDecisionWorkBuilder:
    def expected_input(self, *, candidate_id: uuid.UUID) -> CandidateDecisionWorkInput:
        try:
            candidate = MemoryCandidate.objects.get(id=candidate_id)
        except MemoryCandidate.DoesNotExist as error:
            raise CandidateDecisionWorkScopeError('memory candidate not found') from error

        return build_candidate_decision_input(candidate)

    def exact_work(self, *, value: CandidateDecisionWorkInput) -> WorkflowWork | None:
        data = _work_data(value)
        fingerprint = work_input_fingerprint(
            work_type=data.work_type,
            subject_type=data.subject_type,
            subject_id=data.subject_id,
            contract_version=data.contract_version,
            occurrence_key=data.occurrence_key,
            input_snapshot=data.input_snapshot,
        )
        return WorkflowWork.objects.filter(
            organization_id=value.organization_id,
            project_id=value.project_id,
            work_type=data.work_type,
            subject_type=data.subject_type,
            subject_id=value.candidate_id,
            contract_version=data.contract_version,
            occurrence_key='',
            input_fingerprint=fingerprint,
        ).first()


_BUILDER = DatabaseCandidateDecisionWorkBuilder()


def get_candidate_decision_work_builder() -> CandidateDecisionWorkBuilder:
    return _BUILDER


def ensure_candidate_decision_work_locked(
    candidate: MemoryCandidate,
    *,
    sources: Iterable[MemoryCandidateSource | Mapping[str, object]] | None = None,
) -> tuple[WorkflowWork, bool]:
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError('locked candidate decision work creation requires an active transaction')
    value = build_candidate_decision_input(candidate, sources=sources)
    return create_work(_work_data(value))


def ensure_candidate_decision_work(candidate_id: uuid.UUID) -> tuple[WorkflowWork, bool]:
    with transaction.atomic():
        try:
            candidate = MemoryCandidate.objects.select_for_update().get(id=candidate_id)
        except MemoryCandidate.DoesNotExist as error:
            raise CandidateDecisionWorkScopeError('memory candidate not found') from error

        return ensure_candidate_decision_work_locked(candidate)


create_or_reuse_candidate_decision_work = ensure_candidate_decision_work
create_or_reuse_candidate_decision_work_locked = ensure_candidate_decision_work_locked
