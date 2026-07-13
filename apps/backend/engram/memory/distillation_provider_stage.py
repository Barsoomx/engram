from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from django.db import transaction
from django.db.models import F

from engram.core.models import (
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
from engram.memory import work_execution
from engram.memory.candidate_parsing import strip_json_fence
from engram.memory.distillation_window import render_observation_block
from engram.memory.work_execution import WorkClaim, execution_configuration_fingerprint, lock_work_fence
from engram.memory.work_failures import (
    CONFIGURATION,
    INFRASTRUCTURE_TRANSIENT,
    INVALID_INPUT,
    PROVIDER_TRANSIENT,
    ClassifiedWorkFailure,
    translate_failure,
)
from engram.memory.workflow_work import canonical_json_bytes
from engram.model_policy.errors import ModelPolicyError, ProviderSecretError
from engram.model_policy.models import ModelPolicy
from engram.model_policy.services import (
    ProviderCallInput,
    ProviderCallResult,
    ResolveModelPolicy,
    ResolveModelPolicyInput,
    get_provider_gateway,
)

EXTRACT_PROMPT_CONTRACT = 'distill_extract.v1'
PROVIDER_OUTPUT_MALFORMED = 'provider_output_malformed'

STAGE_COMPLETED = 'completed'
STAGE_RETRY = 'retry'
STAGE_BLOCKED = 'blocked'
STAGE_CONTINUATION = 'continuation'

_MAX_MEMORIES = 12
_MAX_TITLE = 255
_MAX_BODY = 3000

_TARGET_SCHEMA = 'distillation_stage_target/v1'
_IDENTITY_SCHEMA = 'distillation_stage_identity/v1'

_EXTRACT_SYSTEM_PROMPT = (
    'Return a JSON object with exactly the keys memories and no_signal_observation_ids '
    'following the distill_extract.v1 contract. Every chunk observation must appear in a '
    'memory supporting set or in no_signal_observation_ids.'
)


class ExtractionContractError(Exception):
    pass


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


@dataclass(frozen=True, slots=True)
class _CompletedOutcome:
    stage: DistillationStage


@dataclass(frozen=True, slots=True)
class _MalformedOutcome:
    call_result: ProviderCallResult


@dataclass(frozen=True, slots=True)
class _ProviderErrorOutcome:
    error: BaseException


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

    if value < 0 or value > 1:
        raise ExtractionContractError('confidence must be within [0, 1]')

    return Decimal(str(value))


def _parse_kind(value: object) -> str:
    if not isinstance(value, str):
        raise ExtractionContractError('kind must be a string')

    clamped = clamp_memory_kind(value)
    if clamped == '':
        raise ExtractionContractError('kind is unknown or not permitted')

    return clamped


def _parse_id_list(
    value: object,
    chunk_observation_ids: frozenset[str],
    label: str,
    *,
    require_non_empty: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ExtractionContractError(f'{label} must be a list')

    if require_non_empty and not value:
        raise ExtractionContractError(f'{label} must be non-empty')

    ids: list[str] = []
    seen: set[str] = set()
    for entry in value:
        if not isinstance(entry, str):
            raise ExtractionContractError(f'{label} must contain strings')

        if entry in seen:
            raise ExtractionContractError(f'{label} must be duplicate-free')

        if entry not in chunk_observation_ids:
            raise ExtractionContractError(f'{label} references an unknown observation')

        seen.add(entry)
        ids.append(entry)

    return tuple(ids)


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
    if not isinstance(body, str) or len(body) > _MAX_BODY:
        raise ExtractionContractError('memory body is invalid')

    supporting = _parse_id_list(
        item['supporting_observation_ids'],
        chunk_observation_ids,
        'supporting_observation_ids',
        require_non_empty=True,
    )
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
        parsed = json.loads(strip_json_fence(raw_body))
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

    memories = tuple(_parse_memory(item, chunk_observation_ids) for item in memories_raw)
    no_signal = _parse_id_list(parsed['no_signal_observation_ids'], chunk_observation_ids, 'no_signal_observation_ids')

    supporting_union: set[str] = set()
    for memory in memories:
        supporting_union.update(memory.supporting_observation_ids)

    no_signal_set = set(no_signal)
    if supporting_union & no_signal_set:
        raise ExtractionContractError('an observation cannot be both supporting and no-signal')

    if supporting_union | no_signal_set != set(chunk_observation_ids):
        raise ExtractionContractError('coverage does not equal the chunk manifest')

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


def _resolve_fallback_policy(stage: DistillationStage) -> ModelPolicy | None:
    try:
        return _resolve_policy(stage, 'generation')
    except ModelPolicyError:
        return None


def _create_or_reuse_stage(
    *,
    chunk: DistillationChunk,
    window: DistillationWindow,
    work: WorkflowWork,
    policy: ModelPolicy,
    policy_role: str,
) -> DistillationStage:
    role_value = _role_value(policy_role)
    target_key = stage_target_key(
        work_id=str(work.id),
        work_input_fingerprint=work.input_fingerprint,
        window_input_hash=window.input_hash,
        stage_kind=DistillationStageKind.EXTRACT.value,
        level=0,
        ordinal=chunk.ordinal,
        chunk_ordinal=chunk.ordinal,
        input_hash=chunk.input_hash,
        prompt_contract=EXTRACT_PROMPT_CONTRACT,
    )
    scoped_key = stage_key(
        target_key=target_key,
        policy_id=str(policy.id),
        policy_version=policy.version,
        policy_role=role_value,
    )
    stage, _created = DistillationStage.objects.get_or_create(
        organization_id=window.organization_id,
        project_id=window.project_id,
        stage_key=scoped_key,
        defaults={
            'team_id': window.team_id,
            'window': window,
            'chunk': chunk,
            'stage_kind': DistillationStageKind.EXTRACT,
            'level': 0,
            'ordinal': chunk.ordinal,
            'target_key': target_key,
            'input_hash': chunk.input_hash,
            'input_manifest': chunk.input_manifest,
            'prompt_contract': EXTRACT_PROMPT_CONTRACT,
            'policy': policy,
            'policy_version': policy.version,
            'policy_role': role_value,
            'status': DistillationStageStatus.REQUIRED,
            'attempt_count': 0,
        },
    )

    return stage


def resolve_extraction_stage(
    *,
    chunk: DistillationChunk,
    claim: WorkClaim,
    now: datetime,
    policy_role: str = DistillationStagePolicyRole.PRIMARY,
) -> DistillationStage:
    with transaction.atomic():
        work_execution.lock_work_fence(claim=claim, now=now)
        window = chunk.window
        work = window.work
        if work.id != claim.work_id:
            raise ValueError('chunk is outside the claimed work scope')

        policy = _resolve_primary_policy(window)

        return _create_or_reuse_stage(chunk=chunk, window=window, work=work, policy=policy, policy_role=policy_role)


def _render_stage_prompt(chunk: DistillationChunk) -> str:
    entries = chunk.input_manifest['observations']
    observation_ids = [entry['observation_id'] for entry in entries]
    observations = {str(item.id): item for item in Observation.objects.filter(id__in=observation_ids)}
    cap = chunk.window.chunk_char_budget
    blocks = [render_observation_block(observations[key], cap) for key in observation_ids if key in observations]

    return '\n\n'.join(blocks)


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


def _attempt_stage(
    stage: DistillationStage,
    claim: WorkClaim,
    *,
    now: datetime,
) -> _CompletedOutcome | _MalformedOutcome | _ProviderErrorOutcome:
    with transaction.atomic():
        work_execution.lock_work_fence(claim=claim, now=now)
        window = stage.window
        if window.work_id != claim.work_id:
            raise ValueError('stage is outside the claimed work scope')

        DistillationStage.objects.filter(id=stage.id).update(attempt_count=F('attempt_count') + 1)

    chunk = stage.chunk
    chunk_observation_ids = frozenset(entry['observation_id'] for entry in chunk.input_manifest['observations'])
    gateway = get_provider_gateway(stage.policy)
    data = ProviderCallInput(
        organization_id=stage.organization_id,
        project_id=stage.project_id,
        team_id=stage.team_id,
        policy=stage.policy,
        request_id=f'distill-stage:{stage.stage_key}',
        trace_id='',
        prompt=_render_stage_prompt(chunk),
        system_prompt=_EXTRACT_SYSTEM_PROMPT,
        response_kind='candidates',
    )
    try:
        result = gateway.call(data)
    except (ModelPolicyError, ProviderSecretError) as error:
        return _ProviderErrorOutcome(error)

    response_bytes = result.generated_body.encode('utf-8')
    response_hash = hashlib.sha256(response_bytes).hexdigest()
    response_size = len(response_bytes)
    try:
        output = parse_extraction_output(result.generated_body, chunk_observation_ids=chunk_observation_ids)
    except ExtractionContractError:
        return _MalformedOutcome(result)

    snapshot = _normalize_output(output)
    output_hash = hashlib.sha256(canonical_json_bytes(snapshot)).hexdigest()
    with transaction.atomic():
        lock_work_fence(claim=claim, now=now)
        locked = DistillationStage.objects.select_for_update().get(id=stage.id)
        existing = (
            DistillationStage.objects.select_for_update()
            .filter(window_id=stage.window_id, target_key=stage.target_key, status=DistillationStageStatus.COMPLETE)
            .first()
        )
        if existing is not None:
            return _CompletedOutcome(existing)

        if locked.status == DistillationStageStatus.COMPLETE:
            return _CompletedOutcome(locked)

        locked.status = DistillationStageStatus.COMPLETE
        locked.accepted_provider_call_id = result.call_record_id
        locked.response_hash = response_hash
        locked.response_size = response_size
        locked.output_snapshot = snapshot
        locked.output_hash = output_hash
        locked.completed_at = now
        locked.save()

        return _CompletedOutcome(locked)


def _record_malformed(stage: DistillationStage, *, now: datetime) -> None:
    DistillationStage.objects.filter(id=stage.id).update(
        last_failure_class=PROVIDER_OUTPUT_MALFORMED,
        last_failure_at=now,
    )

    return


def _malformed_failure() -> ClassifiedWorkFailure:
    return ClassifiedWorkFailure(failure_class=PROVIDER_TRANSIENT, code=PROVIDER_OUTPUT_MALFORMED)


def _fallback_permitted(stage: DistillationStage) -> bool:
    return stage.policy_role == DistillationStagePolicyRole.PRIMARY and stage.policy.fallback_enabled


def _fallback_eligible(failure: ClassifiedWorkFailure) -> bool:
    return failure.failure_class in (PROVIDER_TRANSIENT, INFRASTRUCTURE_TRANSIENT)


def _status_for_failure(failure: ClassifiedWorkFailure) -> str:
    if failure.failure_class in (CONFIGURATION, INVALID_INPUT):
        return STAGE_BLOCKED

    return STAGE_RETRY


def _classify_provider_error(error: BaseException, stage: DistillationStage) -> ClassifiedWorkFailure:
    fingerprint = execution_configuration_fingerprint(stage.window.work)

    return translate_failure(error, configuration_fingerprint=fingerprint)


def _try_fallback(primary_stage: DistillationStage, claim: WorkClaim, *, now: datetime) -> StageExecutionResult | None:
    policy = _resolve_fallback_policy(primary_stage)
    if policy is None or policy.id == primary_stage.policy_id:
        return None

    window = primary_stage.window
    fallback_stage = _create_or_reuse_stage(
        chunk=primary_stage.chunk,
        window=window,
        work=window.work,
        policy=policy,
        policy_role=DistillationStagePolicyRole.FALLBACK,
    )
    result = _run_stage(fallback_stage, claim, now=now, allow_fallback=False)
    if result.status != STAGE_COMPLETED:
        return None

    return StageExecutionResult(STAGE_COMPLETED, result.stage, fallback_used=True)


def _run_stage(
    stage: DistillationStage,
    claim: WorkClaim,
    *,
    now: datetime,
    allow_fallback: bool,
) -> StageExecutionResult:
    outcome = _attempt_stage(stage, claim, now=now)

    if isinstance(outcome, _CompletedOutcome):
        return StageExecutionResult(STAGE_COMPLETED, outcome.stage)

    if isinstance(outcome, _MalformedOutcome):
        _record_malformed(stage, now=now)
        if allow_fallback and _fallback_permitted(stage):
            fallback = _try_fallback(stage, claim, now=now)
            if fallback is not None:
                return fallback

        return StageExecutionResult(STAGE_RETRY, stage, failure=_malformed_failure())

    failure = _classify_provider_error(outcome.error, stage)
    if _fallback_eligible(failure) and allow_fallback and _fallback_permitted(stage):
        fallback = _try_fallback(stage, claim, now=now)
        if fallback is not None:
            return fallback

    return StageExecutionResult(_status_for_failure(failure), stage, failure=failure)


def execute_distillation_stage(stage: DistillationStage, claim: WorkClaim, *, now: datetime) -> StageExecutionResult:
    current = DistillationStage.objects.get(id=stage.id)
    if current.status == DistillationStageStatus.COMPLETE:
        return StageExecutionResult(STAGE_COMPLETED, current)

    return _run_stage(current, claim, now=now, allow_fallback=True)
