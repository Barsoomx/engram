from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import datetime

from django.db import IntegrityError, transaction

from engram.core.models import (
    AgentSession,
    DistillationChunk,
    DistillationStage,
    DistillationStageKind,
    DistillationStageStatus,
    DistillationWindow,
    Observation,
    WorkflowRun,
    WorkflowRunOrigin,
    WorkflowSubjectType,
    WorkflowWork,
    WorkflowWorkType,
)
from engram.memory.candidate_parsing import truncate_with_marker
from engram.memory.observation_work import useful_observation_q
from engram.memory.services import MemoryWorkerError, redact_text, redact_value
from engram.memory.work_dispatch import queue_work_attempt
from engram.memory.work_execution import WorkClaim, fingerprint_matches, finish_work_claim, lock_work_fence
from engram.memory.workflow_work import canonical_json_bytes, observation_content_digest

WINDOW_MANIFEST_SCHEMA = 'distillation_window_manifest.v1'
CHUNK_MANIFEST_SCHEMA = 'distillation_chunk_manifest.v1'
_CONTRACT_VERSION = 1
_CHUNK_CONTRACT_VERSION = 1

_CONTRACT_INVALID = 'work_contract_invalid'
_SCOPE_INVALID = 'work_scope_invalid'
_FINGERPRINT_MISMATCH = 'work_fingerprint_mismatch'

_MAX_CALLS_ENV = 'ENGRAM_DISTILL_MAX_PROVIDER_CALLS_PER_ATTEMPT'
_DEFAULT_MAX_CALLS = 8
_MIN_MAX_CALLS = 1
_MAX_MAX_CALLS = 64

_BUDGET_ENV = 'ENGRAM_DISTILL_CHUNK_CHAR_BUDGET'
_DEFAULT_CHUNK_CHAR_BUDGET = 40000
_MIN_CHUNK_CHAR_BUDGET = 8000
_MAX_CHUNK_CHAR_BUDGET = 120000

_TARGET_ENV = 'ENGRAM_DISTILL_REDUCE_TARGET'
_DEFAULT_REDUCTION_TARGET = 12
_MIN_REDUCTION_TARGET = 1
_MAX_REDUCTION_TARGET = 64


@dataclass(frozen=True, slots=True)
class _ManifestEntry:
    observation: Observation
    payload: dict[str, object]


def max_provider_calls_per_attempt() -> int:
    raw = os.environ.get(_MAX_CALLS_ENV)
    if raw is None:
        return _DEFAULT_MAX_CALLS

    value = int(raw)
    if not _MIN_MAX_CALLS <= value <= _MAX_MAX_CALLS:
        raise ValueError(f'{_MAX_CALLS_ENV} must be within {_MIN_MAX_CALLS}..{_MAX_MAX_CALLS}')

    return value


def _frozen_chunk_char_budget() -> int:
    raw = os.environ.get(_BUDGET_ENV)
    value = int(raw) if raw is not None else _DEFAULT_CHUNK_CHAR_BUDGET
    if not _MIN_CHUNK_CHAR_BUDGET <= value <= _MAX_CHUNK_CHAR_BUDGET:
        raise ValueError(f'{_BUDGET_ENV} must be within {_MIN_CHUNK_CHAR_BUDGET}..{_MAX_CHUNK_CHAR_BUDGET}')

    return value


def _frozen_reduction_target() -> int:
    raw = os.environ.get(_TARGET_ENV)
    value = int(raw) if raw is not None else _DEFAULT_REDUCTION_TARGET
    if not _MIN_REDUCTION_TARGET <= value <= _MAX_REDUCTION_TARGET:
        raise ValueError(f'{_TARGET_ENV} must be within {_MIN_REDUCTION_TARGET}..{_MAX_REDUCTION_TARGET}')

    return value


def _verify_work_contract(work: WorkflowWork) -> None:
    if work.work_type != WorkflowWorkType.SESSION_DISTILLATION:
        raise MemoryWorkerError('work is not a session distillation root', code=_CONTRACT_INVALID)

    if work.subject_type != WorkflowSubjectType.AGENT_SESSION:
        raise MemoryWorkerError('work subject is not an agent session', code=_CONTRACT_INVALID)

    if work.contract_version != 1:
        raise MemoryWorkerError('unsupported work contract version', code=_CONTRACT_INVALID)

    snapshot = work.input_snapshot
    if not isinstance(snapshot, dict) or snapshot.get('schema') != 'session_distillation_input/v1':
        raise MemoryWorkerError('unsupported session snapshot schema', code=_CONTRACT_INVALID)

    if not fingerprint_matches(work):
        raise MemoryWorkerError('work fingerprint does not match its immutable snapshot', code=_FINGERPRINT_MISMATCH)

    return


def _load_session(work: WorkflowWork) -> AgentSession:
    try:
        session = AgentSession.objects.get(
            id=work.subject_id,
            organization_id=work.organization_id,
            project_id=work.project_id,
        )
    except AgentSession.DoesNotExist as error:
        raise MemoryWorkerError('session is outside the declared work scope', code=_SCOPE_INVALID) from error

    if work.team_id is not None and session.team_id != work.team_id:
        raise MemoryWorkerError('session team does not match work team', code=_SCOPE_INVALID)

    if str(session.id) != work.input_snapshot.get('session_id'):
        raise MemoryWorkerError('session snapshot subject does not match work subject', code=_SCOPE_INVALID)

    return session


def _read_prefix(work: WorkflowWork, session: AgentSession, *, lower: int, upper: int) -> list[Observation]:
    observations = (
        Observation.objects.filter(
            organization_id=work.organization_id,
            project_id=work.project_id,
            session_id=session.id,
            session_sequence__gt=lower,
            session_sequence__lte=upper,
        )
        .filter(useful_observation_q())
        .order_by('session_sequence', 'id')
    )

    return list(observations)


def render_observation_block(observation: Observation, cap: int) -> str:
    block = '\n'.join(
        [
            f'Observation: {observation.id}',
            f'Title: {redact_text(observation.title)}',
            f'Body: {redact_text(observation.body)}',
            f'Facts: {redact_value(observation.facts)}',
            f'Narrative: {redact_text(observation.narrative)}',
            f'Concepts: {redact_value(observation.concepts)}',
            f'Files read: {redact_value(observation.files_read)}',
            f'Files modified: {redact_value(observation.files_modified)}',
        ]
    )

    return truncate_with_marker(block, cap)


def _manifest_entries(observations: list[Observation]) -> list[_ManifestEntry]:
    entries: list[_ManifestEntry] = []
    for observation in observations:
        payload = {
            'observation_id': str(observation.id),
            'session_sequence': observation.session_sequence,
            'content_digest': observation_content_digest(observation),
        }
        entries.append(_ManifestEntry(observation=observation, payload=payload))

    return entries


def _window_manifest(work: WorkflowWork, entries: list[_ManifestEntry], *, lower: int, upper: int) -> dict[str, object]:
    return {
        'schema': WINDOW_MANIFEST_SCHEMA,
        'work_id': str(work.id),
        'work_input_fingerprint': work.input_fingerprint,
        'lower_sequence_exclusive': lower,
        'upper_sequence_inclusive': upper,
        'observations': [entry.payload for entry in entries],
    }


def _sha256(value: dict[str, object]) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _plan_chunks(entries: list[_ManifestEntry], budget: int) -> list[list[_ManifestEntry]]:
    chunks: list[list[_ManifestEntry]] = []
    current: list[_ManifestEntry] = []
    current_chars = 0
    for entry in entries:
        block_chars = len(render_observation_block(entry.observation, budget))
        separator = 2 if current else 0
        if current and current_chars + separator + block_chars > budget:
            chunks.append(current)
            current = []
            current_chars = 0
            separator = 0
        current.append(entry)
        current_chars += separator + block_chars
    if current:
        chunks.append(current)

    return chunks


def _chunk_manifest(window_input_hash: str, ordinal: int, entries: list[_ManifestEntry]) -> dict[str, object]:
    return {
        'schema': CHUNK_MANIFEST_SCHEMA,
        'window_input_hash': window_input_hash,
        'ordinal': ordinal,
        'observations': [entry.payload for entry in entries],
    }


def _persist_plan(
    work: WorkflowWork,
    session: AgentSession,
    *,
    lower: int,
    upper: int,
    input_hash: str,
    observation_count: int,
    budget: int,
    target: int,
    chunk_plans: list[list[_ManifestEntry]],
) -> DistillationWindow:
    planned: list[tuple[list[_ManifestEntry], dict[str, object], str]] = []
    for ordinal, entries in enumerate(chunk_plans):
        manifest = _chunk_manifest(input_hash, ordinal, entries)
        planned.append((entries, manifest, _sha256(manifest)))
    try:
        with transaction.atomic():
            window = DistillationWindow.objects.create(
                organization_id=work.organization_id,
                project_id=work.project_id,
                team_id=work.team_id,
                work=work,
                session=session,
                contract_version=_CONTRACT_VERSION,
                lower_sequence_exclusive=lower,
                upper_sequence_inclusive=upper,
                observation_count=observation_count,
                input_hash=input_hash,
                chunk_char_budget=budget,
                reduction_target=target,
                chunk_contract_version=_CHUNK_CONTRACT_VERSION,
            )
            for ordinal, (entries, manifest, manifest_hash) in enumerate(planned):
                DistillationChunk.objects.create(
                    organization_id=work.organization_id,
                    project_id=work.project_id,
                    team_id=work.team_id,
                    window=window,
                    ordinal=ordinal,
                    first_sequence=entries[0].payload['session_sequence'],
                    last_sequence=entries[-1].payload['session_sequence'],
                    observation_count=len(entries),
                    input_manifest=manifest,
                    input_hash=manifest_hash,
                )

            return window
    except IntegrityError:
        existing = DistillationWindow.objects.filter(work=work).first()
        if existing is None:
            raise

        expected = [(manifest, manifest_hash) for _entries, manifest, manifest_hash in planned]

        return _verify_existing(existing, input_hash, expected)


def _verify_existing(
    window: DistillationWindow,
    input_hash: str,
    expected_chunk_manifests: list[tuple[dict[str, object], str]] | None = None,
) -> DistillationWindow:
    if window.input_hash != input_hash:
        raise MemoryWorkerError(
            'existing window plan does not match the recomputed generation',
            code=_FINGERPRINT_MISMATCH,
        )

    if expected_chunk_manifests is not None:
        persisted = list(window.chunks.order_by('ordinal'))
        mismatch = len(persisted) != len(expected_chunk_manifests) or any(
            chunk.input_manifest != manifest or chunk.input_hash != manifest_hash
            for chunk, (manifest, manifest_hash) in zip(persisted, expected_chunk_manifests, strict=True)
        )
        if mismatch:
            raise MemoryWorkerError(
                'existing window chunk plan does not match the recomputed generation',
                code=_FINGERPRINT_MISMATCH,
            )

    return window


def materialize_distillation_window(work: WorkflowWork) -> DistillationWindow:
    _verify_work_contract(work)
    session = _load_session(work)

    snapshot = work.input_snapshot
    lower = snapshot['lower_sequence_exclusive']
    upper = snapshot['upper_sequence_inclusive']

    existing = DistillationWindow.objects.filter(work=work).first()
    observations = _read_prefix(work, session, lower=lower, upper=upper)
    if not observations:
        raise MemoryWorkerError('session distillation window has no useful observations', code=_CONTRACT_INVALID)

    budget = _frozen_chunk_char_budget()
    target = _frozen_reduction_target()
    entries = _manifest_entries(observations)
    manifest = _window_manifest(work, entries, lower=lower, upper=upper)
    input_hash = _sha256(manifest)

    if existing is not None:
        return _verify_existing(existing, input_hash)

    chunk_plans = _plan_chunks(entries, budget)

    return _persist_plan(
        work,
        session,
        lower=lower,
        upper=upper,
        input_hash=input_hash,
        observation_count=len(observations),
        budget=budget,
        target=target,
        chunk_plans=chunk_plans,
    )


def next_distillation_stage(window: DistillationWindow) -> DistillationChunk | None:
    complete_chunk_ids = set(
        DistillationStage.objects.filter(
            window=window,
            stage_kind=DistillationStageKind.EXTRACT,
            status=DistillationStageStatus.COMPLETE,
        ).values_list('chunk_id', flat=True)
    )
    for chunk in window.chunks.order_by('ordinal'):
        if chunk.id not in complete_chunk_ids:
            return chunk

    return None


def continue_distillation_work(*, work: WorkflowWork, claim: WorkClaim, now: datetime) -> WorkflowRun:
    if claim.work_id != work.id:
        raise ValueError('claim does not belong to the continued work')

    with transaction.atomic():
        lock_work_fence(claim=claim, now=now)
        finish_work_claim(claim=claim, now=now, completion='continue_required')

        return queue_work_attempt(work_id=work.id, now=now, origin=WorkflowRunOrigin.RECONCILIATION)
