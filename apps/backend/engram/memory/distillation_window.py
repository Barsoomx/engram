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
    DistillationStagePolicyRole,
    DistillationStageStatus,
    DistillationWindow,
    Observation,
    WorkflowRun,
    WorkflowRunOrigin,
    WorkflowSubjectType,
    WorkflowWorkType,
)
from engram.memory.candidate_parsing import truncate_with_marker
from engram.memory.observation_work import useful_observation_q
from engram.memory.services import redact_text, redact_value
from engram.memory.work_dispatch import queue_work_attempt
from engram.memory.work_execution import WorkClaim, fingerprint_matches, finish_work_claim, lock_work_fence
from engram.memory.work_failures import INVALID_INPUT
from engram.memory.workflow_work import canonical_json_bytes, observation_content_digest
from engram.model_policy.services import ResolveModelPolicy, ResolveModelPolicyInput

WINDOW_MANIFEST_SCHEMA = 'distillation_window_manifest.v1'
CHUNK_MANIFEST_SCHEMA = 'distillation_chunk_manifest.v1'
STAGE_TARGET_SCHEMA = 'distillation_stage_target.v1'
_CONTRACT_VERSION = 1
_CHUNK_CONTRACT_VERSION = 1
_EXTRACT_PROMPT_CONTRACT = 'distill_extract.v1'
_CURATION_TASK_TYPE = 'curation'

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


class DistillationInputError(ValueError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.failure_class = INVALID_INPUT


@dataclass(frozen=True, slots=True)
class _ManifestEntry:
    observation: Observation
    payload: dict[str, object]
    block_chars: int


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


def _verify_work_contract(work: object) -> None:
    if work.work_type != WorkflowWorkType.SESSION_DISTILLATION:
        raise DistillationInputError('work is not a session distillation root')

    if work.subject_type != WorkflowSubjectType.AGENT_SESSION:
        raise DistillationInputError('work subject is not an agent session')

    if work.contract_version != 1:
        raise DistillationInputError('unsupported work contract version')

    snapshot = work.input_snapshot
    if not isinstance(snapshot, dict) or snapshot.get('schema') != 'session_distillation_input/v1':
        raise DistillationInputError('unsupported session snapshot schema')

    if not fingerprint_matches(work):
        raise DistillationInputError('work fingerprint does not match its immutable snapshot')

    return


def _load_session(work: object) -> AgentSession:
    try:
        session = AgentSession.objects.get(
            id=work.subject_id,
            organization_id=work.organization_id,
            project_id=work.project_id,
        )
    except AgentSession.DoesNotExist as error:
        raise DistillationInputError('session is outside the declared work scope') from error

    if work.team_id is not None and session.team_id != work.team_id:
        raise DistillationInputError('session team does not match work team')

    if str(session.id) != work.input_snapshot.get('session_id'):
        raise DistillationInputError('session snapshot subject does not match work subject')

    return session


def _read_prefix(work: object, session: AgentSession, *, lower: int, upper: int) -> list[Observation]:
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


def _render_block(observation: Observation, cap: int) -> str:
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


def _manifest_entries(observations: list[Observation], budget: int) -> list[_ManifestEntry]:
    entries: list[_ManifestEntry] = []
    for observation in observations:
        payload = {
            'observation_id': str(observation.id),
            'session_sequence': observation.session_sequence,
            'content_digest': observation_content_digest(observation),
        }
        block = _render_block(observation, budget)
        entries.append(_ManifestEntry(observation=observation, payload=payload, block_chars=len(block)))

    return entries


def _window_manifest(work: object, entries: list[_ManifestEntry], *, lower: int, upper: int) -> dict[str, object]:
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
        separator = 2 if current else 0
        if current and current_chars + separator + entry.block_chars > budget:
            chunks.append(current)
            current = []
            current_chars = 0
            separator = 0
        current.append(entry)
        current_chars += separator + entry.block_chars
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
    work: object,
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
            for ordinal, entries in enumerate(chunk_plans):
                manifest = _chunk_manifest(input_hash, ordinal, entries)
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
                    input_hash=_sha256(manifest),
                )

            return window
    except IntegrityError:
        existing = DistillationWindow.objects.filter(work=work).first()
        if existing is None:
            raise

        return _verify_existing(existing, input_hash)


def _verify_existing(window: DistillationWindow, input_hash: str) -> DistillationWindow:
    if window.input_hash != input_hash:
        raise DistillationInputError('existing window plan does not match the recomputed generation')

    return window


def materialize_distillation_window(work: object) -> DistillationWindow:
    _verify_work_contract(work)
    session = _load_session(work)

    snapshot = work.input_snapshot
    lower = snapshot['lower_sequence_exclusive']
    upper = snapshot['upper_sequence_inclusive']

    existing = DistillationWindow.objects.filter(work=work).first()
    observations = _read_prefix(work, session, lower=lower, upper=upper)
    if not observations:
        raise DistillationInputError('session distillation window has no useful observations')

    budget = _frozen_chunk_char_budget()
    target = _frozen_reduction_target()
    entries = _manifest_entries(observations, budget)
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


def _resolve_curation_policy(window: DistillationWindow) -> object:
    resolved = ResolveModelPolicy().execute(
        ResolveModelPolicyInput(
            organization_id=window.organization_id,
            project_id=window.project_id,
            team_id=window.team_id,
            task_type=_CURATION_TASK_TYPE,
        )
    )

    return resolved.policy


def _stage_target_projection(window: DistillationWindow, chunk: DistillationChunk) -> dict[str, object]:
    return {
        'schema': STAGE_TARGET_SCHEMA,
        'work_id': str(window.work_id),
        'work_input_fingerprint': window.work.input_fingerprint,
        'window_input_hash': window.input_hash,
        'stage_kind': DistillationStageKind.EXTRACT,
        'level': 0,
        'ordinal': chunk.ordinal,
        'chunk_ordinal': chunk.ordinal,
        'input_hash': chunk.input_hash,
        'prompt_contract': _EXTRACT_PROMPT_CONTRACT,
    }


def _first_uncovered_chunk(window: DistillationWindow) -> DistillationChunk | None:
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


def next_distillation_stage(window: DistillationWindow) -> DistillationStage | None:
    chunk = _first_uncovered_chunk(window)
    if chunk is None:
        return None

    policy = _resolve_curation_policy(window)
    target = _stage_target_projection(window, chunk)
    target_key = hashlib.sha256(canonical_json_bytes(target)).hexdigest()
    stage_identity = {
        **target,
        'policy_id': str(policy.id),
        'policy_version': policy.version,
        'policy_role': DistillationStagePolicyRole.PRIMARY,
    }
    stage_key = hashlib.sha256(canonical_json_bytes(stage_identity)).hexdigest()

    with transaction.atomic():
        stage, _created = DistillationStage.objects.get_or_create(
            organization_id=window.organization_id,
            project_id=window.project_id,
            stage_key=stage_key,
            defaults={
                'team_id': window.team_id,
                'window': window,
                'chunk': chunk,
                'stage_kind': DistillationStageKind.EXTRACT,
                'level': 0,
                'ordinal': chunk.ordinal,
                'target_key': target_key,
                'input_hash': chunk.input_hash,
                'input_manifest': {
                    'schema': STAGE_TARGET_SCHEMA,
                    'chunk_ordinal': chunk.ordinal,
                    'chunk_input_hash': chunk.input_hash,
                },
                'prompt_contract': _EXTRACT_PROMPT_CONTRACT,
                'policy_id': policy.id,
                'policy_version': policy.version,
                'policy_role': DistillationStagePolicyRole.PRIMARY,
                'status': DistillationStageStatus.REQUIRED,
                'attempt_count': 0,
            },
        )

    return stage


def continue_distillation_work(*, work: object, claim: WorkClaim, now: datetime) -> WorkflowRun:
    with transaction.atomic():
        lock_work_fence(claim=claim, now=now)
        finish_work_claim(claim=claim, now=now, completion='continue_required')

        return queue_work_attempt(work_id=work.id, now=now, origin=WorkflowRunOrigin.RECONCILIATION)
