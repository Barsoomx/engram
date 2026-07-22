from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Protocol

import structlog
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from engram.core.models import (
    AuditResult,
    DistillationChunk,
    DistillationStage,
    DistillationStageKind,
    DistillationStagePolicyRole,
    DistillationStageStatus,
    DistillationWindow,
    Observation,
    WorkflowWork,
    clamp_memory_kind,
)
from engram.core.redaction import redact_value
from engram.memory import work_execution
from engram.memory.distillation_window import max_provider_calls_per_attempt, render_observation_block
from engram.memory.services import MemoryWorkerError
from engram.memory.work_execution import (
    StaleWorkFenceError,
    WorkClaim,
    execution_configuration_fingerprint,
    lock_work_fence,
)
from engram.memory.work_failures import (
    CONFIGURATION,
    INVALID_INPUT,
    PROVIDER_TRANSIENT,
    ClassifiedWorkFailure,
    translate_failure,
)
from engram.memory.workflow_work import canonical_json_bytes, observation_content_digest
from engram.model_policy.errors import ModelPolicyError, ProviderSecretError
from engram.model_policy.models import ModelPolicy, ProviderCallRecord
from engram.model_policy.services import (
    ProviderCallInput,
    ProviderCallResult,
    ResolveModelPolicy,
    ResolveModelPolicyInput,
    get_provider_gateway,
    is_truncated_finish_reason,
)

logger = structlog.get_logger(__name__)

EXTRACT_PROMPT_CONTRACT = 'distill_extract.v1'
PROVIDER_OUTPUT_MALFORMED = 'provider_output_malformed'
PROVIDER_OUTPUT_TRUNCATED = 'provider_output_truncated'
_EXTRACT_REUSE_SCHEMA = 'distill_extract_reuse.v1'
_TRUNCATION_FAILURE_DETAIL = 'reduction provider output was truncated at the completion cap'
_RESPONSE_PREFIX_LIMIT = 2000


def extract_reuse_key(chunk: DistillationChunk) -> str:
    observations = chunk.input_manifest['observations']
    projection = {
        'schema': _EXTRACT_REUSE_SCHEMA,
        'prompt_contract': EXTRACT_PROMPT_CONTRACT,
        'chunk_char_budget': chunk.window.chunk_char_budget,
        'observations': [
            {'observation_id': entry['observation_id'], 'content_digest': entry['content_digest']}
            for entry in observations
        ],
    }

    return hashlib.sha256(canonical_json_bytes(projection)).hexdigest()


STAGE_COMPLETED = 'completed'
STAGE_RETRY = 'retry'
STAGE_BLOCKED = 'blocked'
STAGE_CONTINUATION = 'continuation'

_MAX_MEMORIES = 12
_MAX_TITLE = 255
_MAX_BODY = 3000
_FALLBACK_FAILURE_CODES = frozenset(
    {
        'dependency_timeout',
        'dependency_unreachable',
        'provider_rate_limited',
        'provider_timeout',
        'provider_unavailable',
        'provider_unreachable',
    }
)
_LEASE_SAFE_MARGIN = timedelta(seconds=30)

_TARGET_SCHEMA = 'distillation_stage_target/v1'
_IDENTITY_SCHEMA = 'distillation_stage_identity/v1'

_EXTRACT_SYSTEM_PROMPT = (
    'You distill session observations into durable engineering memories following the '
    'distill_extract.v1 contract. '
    'Return exactly one JSON object and nothing else: no prose, no markdown code fences. '
    'The object must contain exactly these keys and no additional properties: '
    'memories (array of at most 8 objects); '
    'no_signal_observation_ids (array of observation ids, unique, may be empty). '
    'Each memories entry must contain exactly these keys and no additional properties: '
    'title (non-blank string, at most 255 characters); '
    'body (non-blank string, at most 2000 characters); '
    'confidence (a JSON number between 0 and 1, never a string); '
    'supporting_observation_ids (non-empty array of unique observation ids); '
    'kind (optional, one of: decision, convention, gotcha, architecture, incident). '
    'Observation ids must be copied verbatim from the Observation: lines of the input; '
    'never invent, alter, or abbreviate an id. '
    'Put each observation id that supports a memory in that memory supporting_observation_ids, and list '
    'observation ids that carry no durable signal in no_signal_observation_ids. '
    'The same observation id may support more than one memory. '
    'Record only durable, reusable engineering knowledge. When unsure whether an observation '
    'carries durable signal, put its id in no_signal_observation_ids. '
    'If nothing durable was learned, return an empty memories array.'
)


class ExtractionContractError(Exception):
    pass


class ProviderStageOutputError(Exception):
    pass


class ProviderGateway(Protocol):
    def call(self, data: ProviderCallInput) -> ProviderCallResult: ...


@dataclass(frozen=True, slots=True)
class ProviderStageTarget:
    window_id: uuid.UUID
    chunk_id: uuid.UUID | None
    stage_kind: str
    level: int
    ordinal: int
    input_manifest: dict[str, object]
    input_hash: str
    prompt_contract: str


@dataclass(frozen=True, slots=True)
class PreparedProviderStageCall:
    prompt: str
    system_prompt: str
    response_kind: str


class ProviderStageContract(Protocol):
    stage_kind: str
    prompt_contract: str

    def prepare_call(self, stage: DistillationStage) -> PreparedProviderStageCall: ...

    def normalize_output(self, raw_body: str, *, stage: DistillationStage) -> dict[str, object]: ...


@dataclass(frozen=True, slots=True)
class ExtractedMemory:
    title: str
    body: str
    confidence: Decimal
    supporting_observation_ids: tuple[str, ...]
    kind: str = ''


@dataclass(frozen=True, slots=True)
class ExtractionOutput:
    memories: tuple[ExtractedMemory, ...]
    no_signal_observation_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StageExecutionResult:
    status: str
    stage: DistillationStage
    failure: ClassifiedWorkFailure | None = None
    fallback_used: bool = False
    provider_call_ids: tuple[str, ...] = field(default_factory=tuple)
    started_provider_calls: int = 0


@dataclass(frozen=True, slots=True)
class _CompletedOutcome:
    stage: DistillationStage
    provider_call_ids: tuple[str, ...] = ()
    started_calls: int = 0


@dataclass(frozen=True, slots=True)
class _MalformedOutcome:
    response_hash: str
    response_size: int
    response_prefix: str = ''
    error_detail: str = ''
    provider_call_ids: tuple[str, ...] = ()
    started_calls: int = 1


@dataclass(frozen=True, slots=True)
class _TruncatedOutcome:
    response_hash: str
    response_size: int
    response_prefix: str
    provider_call_ids: tuple[str, ...] = ()
    started_calls: int = 1


@dataclass(frozen=True, slots=True)
class _ProviderErrorOutcome:
    error: BaseException
    provider_call_ids: tuple[str, ...] = ()
    started_calls: int = 1


def _fresh_now(initial: datetime) -> datetime:
    return max(initial, timezone.now())


def _lease_allows_provider_call(claim: WorkClaim, *, now: datetime) -> bool:
    return now + _LEASE_SAFE_MARGIN < claim.lease_expires_at


def stage_target_key(
    *,
    work_id: str,
    work_input_fingerprint: str,
    window_input_hash: str,
    stage_kind: str,
    level: int,
    ordinal: int,
    chunk_ordinal: int | None,
    input_hash: str,
    prompt_contract: str,
) -> str:
    projection = {
        'schema': _TARGET_SCHEMA,
        'work_id': work_id,
        'work_input_fingerprint': work_input_fingerprint,
        'window_input_hash': window_input_hash,
        'stage_kind': stage_kind,
        'level': level,
        'ordinal': ordinal,
        'chunk_ordinal': chunk_ordinal,
        'input_hash': input_hash,
        'prompt_contract': prompt_contract,
    }

    return hashlib.sha256(canonical_json_bytes(projection)).hexdigest()


def stage_key(*, target_key: str, policy_id: str, policy_version: int, policy_role: str) -> str:
    projection = {
        'schema': _IDENTITY_SCHEMA,
        'target_key': target_key,
        'policy_id': policy_id,
        'policy_version': policy_version,
        'policy_role': _role_value(policy_role),
    }

    return hashlib.sha256(canonical_json_bytes(projection)).hexdigest()


def _role_value(policy_role: str) -> str:
    return getattr(policy_role, 'value', policy_role)


def _parse_confidence(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ExtractionContractError('confidence must be numeric')

    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ExtractionContractError('confidence must be numeric') from error
    if not parsed.is_finite():
        raise ExtractionContractError('confidence must be finite')
    if parsed < 0 or parsed > 1:
        raise ExtractionContractError('confidence must be within [0, 1]')

    return parsed


def _parse_kind(value: object) -> str:
    if not isinstance(value, str):
        raise ExtractionContractError('kind must be a string')

    clamped = clamp_memory_kind(value)
    if clamped == '':
        raise ExtractionContractError('kind is unknown or not permitted')

    return clamped


def _parse_supporting_ids(value: object, chunk_observation_ids: frozenset[str]) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ExtractionContractError('supporting_observation_ids must be a list')

    if not value:
        raise ExtractionContractError('supporting_observation_ids must be non-empty')

    seen: set[str] = set()
    valid: list[str] = []
    for entry in value:
        if not isinstance(entry, str):
            raise ExtractionContractError('supporting_observation_ids must contain strings')

        if entry in seen:
            raise ExtractionContractError('supporting_observation_ids must be duplicate-free')

        seen.add(entry)
        if entry in chunk_observation_ids:
            valid.append(entry)

    return tuple(valid)


def _validate_no_signal_ids(value: object) -> None:
    if not isinstance(value, list):
        raise ExtractionContractError('no_signal_observation_ids must be a list')

    for entry in value:
        if not isinstance(entry, str):
            raise ExtractionContractError('no_signal_observation_ids must contain strings')

    return


def _parse_memory(item: object, chunk_observation_ids: frozenset[str]) -> ExtractedMemory:
    if not isinstance(item, dict):
        raise ExtractionContractError('memory must be an object')

    keys = set(item.keys())
    if not {'title', 'body', 'confidence', 'supporting_observation_ids'} <= keys:
        raise ExtractionContractError('memory is missing required keys')

    if not keys <= {'title', 'body', 'confidence', 'supporting_observation_ids', 'kind'}:
        raise ExtractionContractError('memory has unknown keys')

    title = item['title']
    if not isinstance(title, str) or not title.strip() or len(title) > _MAX_TITLE:
        raise ExtractionContractError('memory title is invalid')

    body = item['body']
    if not isinstance(body, str) or not body.strip() or len(body) > _MAX_BODY:
        raise ExtractionContractError('memory body is invalid')

    supporting = _parse_supporting_ids(item['supporting_observation_ids'], chunk_observation_ids)
    kind = _parse_kind(item['kind']) if 'kind' in item else ''

    return ExtractedMemory(
        title=title,
        body=body,
        confidence=_parse_confidence(item['confidence']),
        supporting_observation_ids=supporting,
        kind=kind,
    )


def parse_extraction_output(raw_body: str, *, chunk_observation_ids: frozenset[str]) -> ExtractionOutput:
    try:
        parsed = json.loads(raw_body)
    except (json.JSONDecodeError, TypeError) as error:
        raise ExtractionContractError('extraction output is not valid JSON') from error

    if not isinstance(parsed, dict):
        raise ExtractionContractError('extraction output must be a JSON object')

    if set(parsed.keys()) != {'memories', 'no_signal_observation_ids'}:
        raise ExtractionContractError('extraction output has unexpected keys')

    memories_raw = parsed['memories']
    if not isinstance(memories_raw, list):
        raise ExtractionContractError('memories must be a list')

    if len(memories_raw) > _MAX_MEMORIES:
        raise ExtractionContractError('too many memories')

    _validate_no_signal_ids(parsed['no_signal_observation_ids'])
    parsed_memories = tuple(_parse_memory(item, chunk_observation_ids) for item in memories_raw)
    memories = tuple(memory for memory in parsed_memories if memory.supporting_observation_ids)

    supporting_union: set[str] = set()
    for memory in memories:
        supporting_union.update(memory.supporting_observation_ids)

    no_signal = tuple(sorted(chunk_observation_ids - supporting_union))

    return ExtractionOutput(memories=memories, no_signal_observation_ids=no_signal)


def _resolve_policy(scope: DistillationWindow | DistillationStage, task_type: str) -> ModelPolicy:
    resolved = ResolveModelPolicy().execute(
        ResolveModelPolicyInput(
            organization_id=scope.organization_id,
            project_id=scope.project_id,
            team_id=scope.team_id,
            task_type=task_type,
        )
    )

    return resolved.policy


def _resolve_primary_policy(window: DistillationWindow) -> ModelPolicy:
    for task_type in ('curation', 'generation'):
        try:
            return _resolve_policy(window, task_type)
        except ModelPolicyError:
            continue

    raise ModelPolicyError('model_policy_not_found', 'no distillation policy resolves for the stage scope')


def resolve_reduction_policy(window: DistillationWindow) -> ModelPolicy:
    return _resolve_primary_policy(window)


def _resolve_fallback_policy(stage: DistillationStage) -> ModelPolicy | None:
    try:
        return _resolve_policy(stage, 'generation')
    except ModelPolicyError:
        return None


def _refresh_live_policy(stage: DistillationStage) -> ModelPolicy:
    try:
        policy = ModelPolicy.objects.select_related('secret').get(
            id=stage.policy_id,
            organization_id=stage.organization_id,
            active=True,
            secret__active=True,
        )
    except ModelPolicy.DoesNotExist as error:
        raise ModelPolicyError('model_policy_not_found', 'model policy is no longer active') from error

    if policy.version != stage.policy_version:
        raise ModelPolicyError('model_policy_not_found', 'model policy version is stale')
    if policy.project_id is not None and policy.project_id != stage.project_id:
        raise ModelPolicyError('policy_scope_mismatch', 'model policy project scope is invalid')
    if policy.team_id is not None and policy.team_id != stage.team_id:
        raise ModelPolicyError('policy_scope_mismatch', 'model policy team scope is invalid')

    if not policy.secret.envelopes.filter(active=True).exists():
        raise ProviderSecretError('provider secret has no active envelope')

    return policy


def _validate_target_shape(target: ProviderStageTarget) -> None:
    if target.stage_kind == DistillationStageKind.EXTRACT:
        if target.chunk_id is None or target.level != 0:
            raise ValueError('extraction stage target shape is invalid')
    elif target.stage_kind == DistillationStageKind.REDUCE:
        if target.chunk_id is not None or target.level <= 0:
            raise ValueError('reduction stage target shape is invalid')
    else:
        raise ValueError('distillation stage target kind is invalid')
    if target.ordinal < 0:
        raise ValueError('distillation stage target ordinal is invalid')
    if not isinstance(target.input_manifest, dict):
        raise ValueError('distillation stage target manifest is invalid')
    if len(target.input_hash) != 64:
        raise ValueError('distillation stage target input hash is invalid')
    if not target.prompt_contract:
        raise ValueError('distillation stage prompt contract is required')


def extraction_stage_target(chunk: DistillationChunk) -> ProviderStageTarget:
    return ProviderStageTarget(
        window_id=chunk.window_id,
        chunk_id=chunk.id,
        stage_kind=DistillationStageKind.EXTRACT,
        level=0,
        ordinal=chunk.ordinal,
        input_manifest=chunk.input_manifest,
        input_hash=chunk.input_hash,
        prompt_contract=EXTRACT_PROMPT_CONTRACT,
    )


def _target_from_stage(stage: DistillationStage) -> ProviderStageTarget:
    return ProviderStageTarget(
        window_id=stage.window_id,
        chunk_id=stage.chunk_id,
        stage_kind=stage.stage_kind,
        level=stage.level,
        ordinal=stage.ordinal,
        input_manifest=stage.input_manifest,
        input_hash=stage.input_hash,
        prompt_contract=stage.prompt_contract,
    )


def _target_chunk(target: ProviderStageTarget, window: DistillationWindow) -> DistillationChunk | None:
    if target.chunk_id is None:
        return None
    try:
        chunk = DistillationChunk.objects.get(
            id=target.chunk_id,
            window_id=window.id,
            organization_id=window.organization_id,
            project_id=window.project_id,
            team_id=window.team_id,
        )
    except DistillationChunk.DoesNotExist as error:
        raise ValueError('distillation stage chunk is outside the target scope') from error
    if chunk.ordinal != target.ordinal:
        raise ValueError('distillation stage chunk ordinal does not match the target')

    return chunk


def _stage_matches_target(
    stage: DistillationStage,
    *,
    target: ProviderStageTarget,
    window: DistillationWindow,
    chunk: DistillationChunk | None,
    target_key: str,
    policy: ModelPolicy,
    policy_role: str,
) -> bool:
    return (
        stage.organization_id == window.organization_id
        and stage.project_id == window.project_id
        and stage.team_id == window.team_id
        and stage.window_id == window.id
        and stage.chunk_id == (chunk.id if chunk is not None else None)
        and stage.stage_kind == target.stage_kind
        and stage.level == target.level
        and stage.ordinal == target.ordinal
        and stage.target_key == target_key
        and stage.input_hash == target.input_hash
        and stage.input_manifest == target.input_manifest
        and stage.prompt_contract == target.prompt_contract
        and stage.policy_id == policy.id
        and stage.policy_version == policy.version
        and stage.policy_role == policy_role
    )


def _create_or_reuse_stage(
    *,
    target: ProviderStageTarget,
    window: DistillationWindow,
    work: WorkflowWork,
    policy: ModelPolicy,
    policy_role: str,
) -> DistillationStage:
    _validate_target_shape(target)
    chunk = _target_chunk(target, window)
    role_value = _role_value(policy_role)
    target_key = stage_target_key(
        work_id=str(work.id),
        work_input_fingerprint=work.input_fingerprint,
        window_input_hash=window.input_hash,
        stage_kind=target.stage_kind,
        level=target.level,
        ordinal=target.ordinal,
        chunk_ordinal=chunk.ordinal if chunk is not None else None,
        input_hash=target.input_hash,
        prompt_contract=target.prompt_contract,
    )
    scoped_key = stage_key(
        target_key=target_key,
        policy_id=str(policy.id),
        policy_version=policy.version,
        policy_role=role_value,
    )
    defaults: dict[str, object] = {
        'team_id': window.team_id,
        'window': window,
        'chunk': chunk,
        'stage_kind': target.stage_kind,
        'level': target.level,
        'ordinal': target.ordinal,
        'target_key': target_key,
        'input_hash': target.input_hash,
        'input_manifest': target.input_manifest,
        'prompt_contract': target.prompt_contract,
        'policy': policy,
        'policy_version': policy.version,
        'policy_role': role_value,
        'status': DistillationStageStatus.REQUIRED,
        'attempt_count': 0,
    }
    if target.stage_kind == DistillationStageKind.EXTRACT:
        defaults['reuse_key'] = extract_reuse_key(chunk)
    stage, _created = DistillationStage.objects.get_or_create(
        organization_id=window.organization_id,
        project_id=window.project_id,
        stage_key=scoped_key,
        defaults=defaults,
    )
    if not _stage_matches_target(
        stage,
        target=target,
        window=window,
        chunk=chunk,
        target_key=target_key,
        policy=policy,
        policy_role=role_value,
    ):
        raise ValueError('existing distillation stage does not match the requested target')

    return stage


def _failure_code_allows_fallback(code: str) -> bool:
    return code == PROVIDER_OUTPUT_MALFORMED or code in _FALLBACK_FAILURE_CODES


def _preferred_pending_stage(
    primary_stage: DistillationStage,
    *,
    target: ProviderStageTarget,
    window: DistillationWindow,
    work: WorkflowWork,
) -> DistillationStage:
    if not _fallback_permitted(primary_stage) or not _failure_code_allows_fallback(primary_stage.last_failure_class):
        return primary_stage
    fallback_policy = _resolve_fallback_policy(primary_stage)
    if fallback_policy is None or fallback_policy.id == primary_stage.policy_id:
        return primary_stage
    fallback_stage = _create_or_reuse_stage(
        target=target,
        window=window,
        work=work,
        policy=fallback_policy,
        policy_role=DistillationStagePolicyRole.FALLBACK,
    )
    if fallback_stage.status == DistillationStageStatus.COMPLETE or fallback_stage.attempt_count == 0:
        return fallback_stage
    if fallback_stage.last_failure_at is None:
        return fallback_stage
    if primary_stage.last_failure_at is None:
        return primary_stage
    if fallback_stage.last_failure_at < primary_stage.last_failure_at:
        return fallback_stage
    if (
        fallback_stage.last_failure_at == primary_stage.last_failure_at
        and fallback_stage.attempt_count < primary_stage.attempt_count
    ):
        return fallback_stage

    return primary_stage


def resolve_provider_stage(
    target: ProviderStageTarget,
    claim: WorkClaim,
    now: datetime,
    policy_role: str = DistillationStagePolicyRole.PRIMARY,
) -> DistillationStage:
    with transaction.atomic():
        work_execution.lock_work_fence(claim=claim, now=now)
        try:
            window = DistillationWindow.objects.select_related('work').get(id=target.window_id)
        except DistillationWindow.DoesNotExist as error:
            raise ValueError('distillation stage window does not exist') from error
        work = window.work
        if work.id != claim.work_id:
            raise ValueError('distillation stage target is outside the claimed work scope')

        policy = _resolve_primary_policy(window)
        stage = _create_or_reuse_stage(
            target=target,
            window=window,
            work=work,
            policy=policy,
            policy_role=policy_role,
        )
        if _role_value(policy_role) != DistillationStagePolicyRole.PRIMARY:
            return stage

        return _preferred_pending_stage(stage, target=target, window=window, work=work)


def resolve_extraction_stage(
    *,
    chunk: DistillationChunk,
    claim: WorkClaim,
    now: datetime,
    policy_role: str = DistillationStagePolicyRole.PRIMARY,
) -> DistillationStage:
    target = extraction_stage_target(chunk)

    return resolve_provider_stage(target, claim, now=now, policy_role=policy_role)


def _stage_manifest_ids(chunk: DistillationChunk) -> tuple[list[str], dict[str, str]]:
    entries = chunk.input_manifest.get('observations')
    if not isinstance(entries, list) or not entries:
        raise ExtractionContractError('chunk manifest observations are invalid')

    observation_ids: list[str] = []
    expected_digests: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ExtractionContractError('chunk manifest observation entry is invalid')
        observation_id = entry.get('observation_id')
        content_digest = entry.get('content_digest')
        if not isinstance(observation_id, str) or not isinstance(content_digest, str):
            raise ExtractionContractError('chunk manifest observation identity is invalid')
        if observation_id in expected_digests:
            raise ExtractionContractError('chunk manifest observations are duplicate-free')
        observation_ids.append(observation_id)
        expected_digests[observation_id] = content_digest

    return observation_ids, expected_digests


def _verify_stage_manifest_live(
    chunk: DistillationChunk, *, stage: DistillationStage | None = None
) -> tuple[dict[str, Observation], list[str]]:
    observation_ids, expected_digests = _stage_manifest_ids(chunk)
    scope = stage or chunk

    observations = {
        str(item.id): item
        for item in Observation.objects.filter(
            id__in=observation_ids,
            organization_id=scope.organization_id,
            project_id=scope.project_id,
            team_id=scope.team_id,
            session_id=chunk.window.session_id,
        )
    }
    if set(observations) != set(observation_ids):
        raise MemoryWorkerError('chunk observation is outside the stage scope', code='work_scope_invalid')

    for observation_id in observation_ids:
        try:
            digest = observation_content_digest(observations[observation_id])
        except ValueError as error:
            raise ExtractionContractError('observation content cannot be digested') from error
        if digest != expected_digests[observation_id]:
            raise MemoryWorkerError(
                'observation content digest does not match the frozen manifest',
                code='work_fingerprint_mismatch',
            )

    return observations, observation_ids


def _render_stage_prompt(chunk: DistillationChunk, *, stage: DistillationStage | None = None) -> str:
    observations, observation_ids = _verify_stage_manifest_live(chunk, stage=stage)

    cap = chunk.window.chunk_char_budget
    blocks = [render_observation_block(observations[observation_id], cap) for observation_id in observation_ids]
    prompt = '\n\n'.join(blocks)
    if len(prompt) > cap:
        raise ExtractionContractError('stage prompt exceeds the frozen chunk budget')

    return prompt


def _normalize_output(output: ExtractionOutput) -> dict[str, object]:
    memories: list[dict[str, object]] = []
    for memory in output.memories:
        entry: dict[str, object] = {
            'title': memory.title,
            'body': memory.body,
            'confidence': str(memory.confidence),
            'supporting_observation_ids': list(memory.supporting_observation_ids),
        }
        if memory.kind:
            entry['kind'] = memory.kind
        memories.append(entry)

    return {'memories': memories, 'no_signal_observation_ids': list(output.no_signal_observation_ids)}


class _ExtractionStageContract:
    stage_kind = DistillationStageKind.EXTRACT
    prompt_contract = EXTRACT_PROMPT_CONTRACT

    def prepare_call(self, stage: DistillationStage) -> PreparedProviderStageCall:
        chunk = stage.chunk
        if chunk is None:
            raise ExtractionContractError('extraction stage has no chunk')

        return PreparedProviderStageCall(
            prompt=_render_stage_prompt(chunk, stage=stage),
            system_prompt=_EXTRACT_SYSTEM_PROMPT,
            response_kind=EXTRACT_PROMPT_CONTRACT,
        )

    def normalize_output(self, raw_body: str, *, stage: DistillationStage) -> dict[str, object]:
        chunk = stage.chunk
        if chunk is None:
            raise ExtractionContractError('extraction stage has no chunk')
        chunk_observation_ids = frozenset(entry['observation_id'] for entry in chunk.input_manifest['observations'])
        try:
            output = parse_extraction_output(raw_body, chunk_observation_ids=chunk_observation_ids)
        except ExtractionContractError as error:
            raise ProviderStageOutputError('extraction provider output is malformed') from error

        return _normalize_output(output)


_EXTRACTION_STAGE_CONTRACT = _ExtractionStageContract()


def _provider_call_ids(*, stage: DistillationStage, request_id: str) -> tuple[str, ...]:
    return tuple(
        str(call_id)
        for call_id in ProviderCallRecord.objects.filter(
            organization_id=stage.organization_id,
            project_id=stage.project_id,
            policy_id=stage.policy_id,
            request_id=request_id,
        )
        .order_by('created_at', 'id')
        .values_list('id', flat=True)
    )


def _new_provider_call_ids(
    *,
    stage: DistillationStage,
    request_id: str,
    before: frozenset[str],
) -> tuple[str, ...]:
    return tuple(call_id for call_id in _provider_call_ids(stage=stage, request_id=request_id) if call_id not in before)


def _provider_call_record_matches(
    *,
    stage: DistillationStage,
    result: ProviderCallResult,
    request_id: str,
) -> bool:
    record = ProviderCallRecord.objects.filter(id=result.call_record_id).first()
    if record is None:
        return False

    return (
        record.organization_id == stage.organization_id
        and record.project_id == stage.project_id
        and record.team_id == stage.team_id
        and record.policy_id == stage.policy_id
        and record.policy_version == stage.policy_version
        and record.secret_id == stage.policy.secret_id
        and record.provider == stage.policy.provider == result.provider
        and record.model == stage.policy.model == result.model
        and record.task_type == stage.policy.task_type
        and record.request_id == request_id
        and record.redaction_state == result.redaction_state
        and record.result == AuditResult.RECORDED
    )


def _call_provider(
    *,
    stage: DistillationStage,
    data: ProviderCallInput,
    request_id: str,
    prior_call_ids: frozenset[str],
) -> ProviderCallResult | _ProviderErrorOutcome:
    try:
        gateway: ProviderGateway = get_provider_gateway(stage.policy)
        result = gateway.call(data)
    except Exception as error:
        if isinstance(error, StaleWorkFenceError):
            raise
        return _ProviderErrorOutcome(
            error,
            _new_provider_call_ids(stage=stage, request_id=request_id, before=prior_call_ids),
        )

    if not result.call_record_id or not _provider_call_record_matches(
        stage=stage,
        result=result,
        request_id=request_id,
    ):
        return _ProviderErrorOutcome(
            ModelPolicyError('provider_call_record_missing', 'provider call provenance is unavailable'),
            _new_provider_call_ids(stage=stage, request_id=request_id, before=prior_call_ids),
        )

    return result


def _validate_contract(stage: DistillationStage, contract: ProviderStageContract) -> None:
    if stage.stage_kind != contract.stage_kind or stage.prompt_contract != contract.prompt_contract:
        raise ValueError('provider stage contract does not match the persisted stage identity')


def _reuse_completed_source(
    stage: DistillationStage,
    window: DistillationWindow,
    *,
    now: datetime,
) -> _CompletedOutcome | None:
    if stage.stage_kind != DistillationStageKind.EXTRACT or not stage.reuse_key:
        return None

    source = (
        DistillationStage.objects.filter(
            window__session_id=window.session_id,
            organization_id=stage.organization_id,
            project_id=stage.project_id,
            team_id=stage.team_id,
            stage_kind=DistillationStageKind.EXTRACT,
            status=DistillationStageStatus.COMPLETE,
            reuse_key=stage.reuse_key,
            policy_id=stage.policy_id,
            policy_version=stage.policy_version,
        )
        .exclude(id=stage.id)
        .order_by('completed_at', 'id')
        .first()
    )
    if source is None:
        return None

    locked = DistillationStage.objects.select_for_update().get(id=stage.id)
    if locked.status == DistillationStageStatus.COMPLETE:
        return _CompletedOutcome(locked)

    chunk = stage.chunk
    _verify_stage_manifest_live(chunk, stage=locked)
    locked.status = DistillationStageStatus.COMPLETE
    locked.reused_from = source
    locked.accepted_provider_call_id = source.accepted_provider_call_id
    locked.response_hash = source.response_hash
    locked.response_size = source.response_size
    locked.output_snapshot = source.output_snapshot
    locked.output_hash = source.output_hash
    locked.completed_at = now
    locked.save()
    logger.info(
        'distill_extract_reused',
        stage_id=str(locked.id),
        reused_from_stage_id=str(source.id),
        session_id=str(window.session_id),
        window_id=str(window.id),
        chunk_ordinal=locked.ordinal,
    )

    return _CompletedOutcome(locked, provider_call_ids=(), started_calls=0)


def _attempt_stage(
    stage: DistillationStage,
    claim: WorkClaim,
    contract: ProviderStageContract,
    *,
    now: datetime,
) -> _CompletedOutcome | _MalformedOutcome | _TruncatedOutcome | _ProviderErrorOutcome:
    _validate_contract(stage, contract)
    request_id = f'distill-stage:{stage.stage_key}'
    prior_call_ids = frozenset(_provider_call_ids(stage=stage, request_id=request_id))
    with transaction.atomic():
        work_execution.lock_work_fence(claim=claim, now=now)
        window = stage.window
        if window.work_id != claim.work_id:
            raise ValueError('stage is outside the claimed work scope')

        existing = (
            DistillationStage.objects.select_for_update()
            .filter(
                organization_id=stage.organization_id,
                project_id=stage.project_id,
                window_id=stage.window_id,
                target_key=stage.target_key,
                status=DistillationStageStatus.COMPLETE,
            )
            .first()
        )
        if existing is not None:
            return _CompletedOutcome(existing)

        reused = _reuse_completed_source(stage, window, now=now)
        if reused is not None:
            return reused

        prepared = contract.prepare_call(stage)
        try:
            policy = _refresh_live_policy(stage)
        except (ModelPolicyError, ProviderSecretError) as error:
            return _ProviderErrorOutcome(error, started_calls=0)
        stage.policy = policy

        DistillationStage.objects.filter(id=stage.id).update(attempt_count=F('attempt_count') + 1)

    data = ProviderCallInput(
        organization_id=stage.organization_id,
        project_id=stage.project_id,
        team_id=stage.team_id,
        policy=stage.policy,
        request_id=request_id,
        trace_id='',
        prompt=prepared.prompt,
        system_prompt=prepared.system_prompt,
        response_kind=prepared.response_kind,
    )
    provider_result = _call_provider(
        stage=stage,
        data=data,
        request_id=request_id,
        prior_call_ids=prior_call_ids,
    )
    if isinstance(provider_result, _ProviderErrorOutcome):
        return provider_result
    result = provider_result

    response_bytes = result.generated_body.encode('utf-8')
    response_hash = hashlib.sha256(response_bytes).hexdigest()
    response_size = len(response_bytes)
    response_prefix = str(redact_value(result.generated_body).value)[:_RESPONSE_PREFIX_LIMIT]
    if stage.stage_kind == DistillationStageKind.REDUCE and is_truncated_finish_reason(result.finish_reason):
        return _TruncatedOutcome(
            response_hash,
            response_size,
            response_prefix,
            (str(result.call_record_id),),
        )
    try:
        snapshot = contract.normalize_output(result.generated_body, stage=stage)
    except ProviderStageOutputError as error:
        return _MalformedOutcome(
            response_hash,
            response_size,
            response_prefix,
            str(error),
            (str(result.call_record_id),),
        )

    output_hash = hashlib.sha256(canonical_json_bytes(snapshot)).hexdigest()
    commit_now = _fresh_now(now)
    with transaction.atomic():
        lock_work_fence(claim=claim, now=commit_now)
        locked = DistillationStage.objects.select_for_update().get(id=stage.id)
        existing = (
            DistillationStage.objects.select_for_update()
            .filter(
                organization_id=stage.organization_id,
                project_id=stage.project_id,
                window_id=stage.window_id,
                target_key=stage.target_key,
                status=DistillationStageStatus.COMPLETE,
            )
            .first()
        )
        if existing is not None:
            return _CompletedOutcome(existing, (str(result.call_record_id),), 1)

        if locked.status == DistillationStageStatus.COMPLETE:
            return _CompletedOutcome(locked, (str(result.call_record_id),), 1)

        locked.status = DistillationStageStatus.COMPLETE
        locked.accepted_provider_call_id = result.call_record_id
        locked.response_hash = response_hash
        locked.response_size = response_size
        locked.output_snapshot = snapshot
        locked.output_hash = output_hash
        locked.completed_at = commit_now
        locked.save()

        return _CompletedOutcome(locked, (str(result.call_record_id),), 1)


def _record_stage_failure_diagnostics(
    stage: DistillationStage,
    claim: WorkClaim,
    *,
    now: datetime,
    failure_class: str,
    response_hash: str,
    response_size: int,
    response_prefix: str,
    provider_call_ids: tuple[str, ...],
) -> None:
    with transaction.atomic():
        lock_work_fence(claim=claim, now=now)
        locked = DistillationStage.objects.select_for_update().get(id=stage.id)
        if locked.window.work_id != claim.work_id:
            raise ValueError('stage is outside the claimed work scope')
        locked.last_failure_class = failure_class
        locked.last_failure_at = now
        locked.save(update_fields=['last_failure_class', 'last_failure_at', 'updated_at'])
        for call_id in provider_call_ids:
            record = (
                ProviderCallRecord.objects.select_for_update()
                .filter(
                    id=call_id,
                    organization_id=stage.organization_id,
                    project_id=stage.project_id,
                    policy_id=stage.policy_id,
                )
                .first()
            )
            if record is None:
                continue
            metadata = dict(record.metadata or {})
            metadata.update(
                {
                    'response_hash': response_hash,
                    'response_size': response_size,
                    'response_prefix': response_prefix,
                }
            )
            record.metadata = metadata
            record.save(update_fields=['metadata', 'updated_at'])

    return


def _record_malformed(
    stage: DistillationStage,
    claim: WorkClaim,
    *,
    now: datetime,
    response_hash: str,
    response_size: int,
    response_prefix: str,
    provider_call_ids: tuple[str, ...],
) -> None:
    _record_stage_failure_diagnostics(
        stage,
        claim,
        now=now,
        failure_class=PROVIDER_OUTPUT_MALFORMED,
        response_hash=response_hash,
        response_size=response_size,
        response_prefix=response_prefix,
        provider_call_ids=provider_call_ids,
    )

    return


def _record_truncated(
    stage: DistillationStage,
    claim: WorkClaim,
    *,
    now: datetime,
    response_hash: str,
    response_size: int,
    response_prefix: str,
    provider_call_ids: tuple[str, ...],
) -> None:
    _record_stage_failure_diagnostics(
        stage,
        claim,
        now=now,
        failure_class=PROVIDER_OUTPUT_TRUNCATED,
        response_hash=response_hash,
        response_size=response_size,
        response_prefix=response_prefix,
        provider_call_ids=provider_call_ids,
    )

    return


def _record_provider_failure(
    stage: DistillationStage,
    claim: WorkClaim,
    *,
    now: datetime,
    failure: ClassifiedWorkFailure,
) -> None:
    with transaction.atomic():
        lock_work_fence(claim=claim, now=now)
        locked = DistillationStage.objects.select_for_update().get(id=stage.id)
        if locked.window.work_id != claim.work_id:
            raise ValueError('stage is outside the claimed work scope')
        locked.last_failure_class = failure.code
        locked.last_failure_at = now
        locked.save(update_fields=['last_failure_class', 'last_failure_at', 'updated_at'])

    return


def _malformed_failure(detail: str = '') -> ClassifiedWorkFailure:
    return ClassifiedWorkFailure(
        failure_class=PROVIDER_TRANSIENT,
        code=PROVIDER_OUTPUT_MALFORMED,
        redacted_detail=str(redact_value(detail).value)[:1024],
    )


def _truncated_failure() -> ClassifiedWorkFailure:
    return ClassifiedWorkFailure(
        failure_class=PROVIDER_TRANSIENT,
        code=PROVIDER_OUTPUT_TRUNCATED,
        redacted_detail=_TRUNCATION_FAILURE_DETAIL,
    )


def _fallback_permitted(stage: DistillationStage) -> bool:
    return stage.policy_role == DistillationStagePolicyRole.PRIMARY and stage.policy.fallback_enabled


def _fallback_eligible(failure: ClassifiedWorkFailure, error: BaseException | None = None) -> bool:
    if isinstance(error, ModelPolicyError) and error.http_status == 425:
        return False

    return failure.code in _FALLBACK_FAILURE_CODES


def _status_for_failure(failure: ClassifiedWorkFailure) -> str:
    if failure.failure_class in (CONFIGURATION, INVALID_INPUT):
        return STAGE_BLOCKED

    return STAGE_RETRY


def _classify_provider_error(error: BaseException, stage: DistillationStage) -> ClassifiedWorkFailure:
    fingerprint = execution_configuration_fingerprint(stage.window.work)
    translated = translate_failure(error, configuration_fingerprint=fingerprint)

    return ClassifiedWorkFailure(
        failure_class=translated.failure_class,
        code=translated.code,
        configuration_fingerprint=translated.configuration_fingerprint,
    )


def _try_fallback(
    primary_stage: DistillationStage,
    claim: WorkClaim,
    contract: ProviderStageContract,
    *,
    now: datetime,
    max_provider_calls: int,
    prior_provider_call_ids: tuple[str, ...],
    prior_started_calls: int,
    prior_failure: ClassifiedWorkFailure,
) -> StageExecutionResult | None:
    fallback_now = _fresh_now(now)
    with transaction.atomic():
        lock_work_fence(claim=claim, now=fallback_now)
        current = DistillationStage.objects.select_related('window', 'window__work', 'chunk').get(id=primary_stage.id)
        policy = _resolve_fallback_policy(current)
        if policy is None or policy.id == current.policy_id:
            return None
        fallback_stage = _create_or_reuse_stage(
            target=_target_from_stage(current),
            window=current.window,
            work=current.window.work,
            policy=policy,
            policy_role=DistillationStagePolicyRole.FALLBACK,
        )
    if prior_started_calls >= max_provider_calls:
        return StageExecutionResult(
            STAGE_CONTINUATION,
            fallback_stage,
            failure=prior_failure,
            provider_call_ids=prior_provider_call_ids,
            started_provider_calls=prior_started_calls,
        )

    fallback_now = _fresh_now(fallback_now)
    if not _lease_allows_provider_call(claim, now=fallback_now):
        return StageExecutionResult(
            STAGE_CONTINUATION,
            fallback_stage,
            failure=prior_failure,
            provider_call_ids=prior_provider_call_ids,
            started_provider_calls=prior_started_calls,
        )

    result = _run_stage(
        fallback_stage,
        claim,
        contract,
        now=fallback_now,
        allow_fallback=False,
        max_provider_calls=max_provider_calls,
        prior_provider_call_ids=prior_provider_call_ids,
        prior_started_calls=prior_started_calls,
    )
    return StageExecutionResult(
        result.status,
        result.stage,
        failure=result.failure,
        fallback_used=True,
        provider_call_ids=result.provider_call_ids,
        started_provider_calls=result.started_provider_calls,
    )


def _run_stage(
    stage: DistillationStage,
    claim: WorkClaim,
    contract: ProviderStageContract,
    *,
    now: datetime,
    allow_fallback: bool,
    max_provider_calls: int,
    prior_provider_call_ids: tuple[str, ...] = (),
    prior_started_calls: int = 0,
) -> StageExecutionResult:
    outcome = _attempt_stage(stage, claim, contract, now=now)

    if isinstance(outcome, _CompletedOutcome):
        return StageExecutionResult(
            STAGE_COMPLETED,
            outcome.stage,
            provider_call_ids=prior_provider_call_ids + outcome.provider_call_ids,
            started_provider_calls=prior_started_calls + outcome.started_calls,
        )

    if isinstance(outcome, _TruncatedOutcome):
        truncated_now = _fresh_now(now)
        _record_truncated(
            stage,
            claim,
            now=truncated_now,
            response_hash=outcome.response_hash,
            response_size=outcome.response_size,
            response_prefix=outcome.response_prefix,
            provider_call_ids=outcome.provider_call_ids,
        )

        return StageExecutionResult(
            STAGE_RETRY,
            stage,
            failure=_truncated_failure(),
            provider_call_ids=prior_provider_call_ids + outcome.provider_call_ids,
            started_provider_calls=prior_started_calls + outcome.started_calls,
        )

    if isinstance(outcome, _MalformedOutcome):
        malformed_now = _fresh_now(now)
        _record_malformed(
            stage,
            claim,
            now=malformed_now,
            response_hash=outcome.response_hash,
            response_size=outcome.response_size,
            response_prefix=outcome.response_prefix,
            provider_call_ids=outcome.provider_call_ids,
        )
        provider_call_ids = prior_provider_call_ids + outcome.provider_call_ids
        started_calls = prior_started_calls + outcome.started_calls
        failure = _malformed_failure(outcome.error_detail)
        if allow_fallback and _fallback_permitted(stage):
            fallback = _try_fallback(
                stage,
                claim,
                contract,
                now=malformed_now,
                max_provider_calls=max_provider_calls,
                prior_provider_call_ids=provider_call_ids,
                prior_started_calls=started_calls,
                prior_failure=failure,
            )
            if fallback is not None:
                return fallback

        return StageExecutionResult(
            STAGE_RETRY,
            stage,
            failure=failure,
            provider_call_ids=provider_call_ids,
            started_provider_calls=started_calls,
        )

    provider_call_ids = prior_provider_call_ids + outcome.provider_call_ids
    started_calls = prior_started_calls + outcome.started_calls
    failure_fence_now = _fresh_now(now)
    with transaction.atomic():
        lock_work_fence(claim=claim, now=failure_fence_now)
    failure = _classify_provider_error(outcome.error, stage)
    failure_now = _fresh_now(failure_fence_now)
    _record_provider_failure(stage, claim, now=failure_now, failure=failure)
    if _fallback_eligible(failure, outcome.error) and allow_fallback and _fallback_permitted(stage):
        fallback = _try_fallback(
            stage,
            claim,
            contract,
            now=failure_now,
            max_provider_calls=max_provider_calls,
            prior_provider_call_ids=provider_call_ids,
            prior_started_calls=started_calls,
            prior_failure=failure,
        )
        if fallback is not None:
            return fallback

    return StageExecutionResult(
        _status_for_failure(failure),
        stage,
        failure=failure,
        provider_call_ids=provider_call_ids,
        started_provider_calls=started_calls,
    )


def execute_provider_stage(
    stage: DistillationStage,
    claim: WorkClaim,
    contract: ProviderStageContract,
    *,
    now: datetime,
    max_provider_calls: int,
) -> StageExecutionResult:
    if type(max_provider_calls) is not int or max_provider_calls < 1:
        raise ValueError('max_provider_calls must be a positive integer')
    with transaction.atomic():
        work_execution.lock_work_fence(claim=claim, now=now)
        current = DistillationStage.objects.select_related('window', 'window__work', 'chunk').get(id=stage.id)
        if current.window.work_id != claim.work_id:
            raise ValueError('stage is outside the claimed work scope')
        _validate_contract(current, contract)
        if current.status == DistillationStageStatus.COMPLETE:
            return StageExecutionResult(STAGE_COMPLETED, current)

    return _run_stage(
        current,
        claim,
        contract,
        now=now,
        allow_fallback=current.policy_role == DistillationStagePolicyRole.PRIMARY,
        max_provider_calls=max_provider_calls,
    )


def execute_distillation_stage(
    stage: DistillationStage,
    claim: WorkClaim,
    *,
    now: datetime,
    max_provider_calls: int | None = None,
) -> StageExecutionResult:
    budget = max_provider_calls if max_provider_calls is not None else max_provider_calls_per_attempt()

    return execute_provider_stage(
        stage,
        claim,
        _EXTRACTION_STAGE_CONTRACT,
        now=now,
        max_provider_calls=budget,
    )
