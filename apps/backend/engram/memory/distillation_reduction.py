from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from engram.memory.workflow_work import canonical_json_bytes

if TYPE_CHECKING:
    from engram.core.models import DistillationStage, DistillationWindow


class ReductionContractError(ValueError):
    pass


MAX_TITLE = 255
MAX_BODY = 3000
REDUCTION_MANIFEST_SCHEMA = 'distillation_reduce_manifest.v1'
REDUCE_PROMPT_CONTRACT = 'distill_reduce.v2'

_REDUCE_SYSTEM_PROMPT = (
    'You consolidate engineering-memory drafts under the distill_reduce.v2 contract. Return '
    'exactly one JSON object and nothing else: no prose, no markdown code fences. The object '
    'must contain exactly the key memories (array of objects) and no additional properties. '
    'Each memories entry must contain exactly these keys and no additional properties: title '
    f'(non-blank string, at most {MAX_TITLE} characters); body (non-blank string, at most {MAX_BODY} '
    'characters); confidence (a JSON number between 0 and 1, never a string); source_refs '
    '(non-empty array of unique positive integers); kind (optional, one of: decision, '
    'convention, gotcha, architecture, incident; omit it when none applies). The user message is '
    'one JSON object with the single key drafts: an array of {index, title, body, confidence, '
    'kind?} where index is a positive integer. Every source_refs value must be a draft index '
    'copied verbatim from the input. Partition the drafts: assign every input index to exactly '
    'one memory, never repeat an index across memories or within one memory, and never omit an '
    'index. Task: merge only drafts that record the same or a near-duplicate durable fact, '
    'decision, or behavior into one memory whose title and body preserve the concrete details '
    '(identifiers, paths, versions, numbers) of every merged draft; never invent facts absent '
    'from the drafts. A draft that is distinct from every other draft must pass through as its '
    'own memory referencing that single index; do not force unrelated drafts together and do not '
    'drop any draft. The number of memories may therefore equal the number of drafts. Give each '
    'memory a confidence no higher than the highest confidence among its source drafts.'
)


_OUTPUT_TOKENS_PER_CHAR = 0.4
_OUTPUT_ENVELOPE_CHARS = 32
_PER_MEMORY_JSON_OVERHEAD = 128
_PER_MEMORY_INDEX_CHARS = 8
_TRUNCATION_MARGIN = 0.30
_PER_MEMORY_CHARS = MAX_TITLE + MAX_BODY + _PER_MEMORY_JSON_OVERHEAD + _PER_MEMORY_INDEX_CHARS


def worst_case_output_tokens(n: int) -> int:
    return math.ceil(_OUTPUT_TOKENS_PER_CHAR * (_OUTPUT_ENVELOPE_CHARS + n * _PER_MEMORY_CHARS))


def output_budget_tokens(cap: int) -> int:
    return math.floor(cap * (1 - _TRUNCATION_MARGIN))


def max_reduction_fanin(budget: int) -> int:
    n = 1
    while worst_case_output_tokens(n + 1) <= budget:
        n += 1

    return n


_MAX_TREE_LEVELS = 4
_GENERATION_LEVEL_STRIDE = 16
_MAX_GENERATION = 3


class ReductionTruncationExhausted(ReductionContractError):  # noqa: N818
    pass


def effective_reduction_target(total_drafts: int, floor: int) -> int:
    return min(48, max(floor, math.ceil(total_drafts / 4)))


def compute_reduction_generation(truncated_levels: Sequence[int]) -> int:
    if not truncated_levels:
        return 0

    generation = max(level // _GENERATION_LEVEL_STRIDE for level in truncated_levels) + 1
    if generation > _MAX_GENERATION:
        raise ReductionTruncationExhausted('reduction truncation generations exhausted')

    return generation


def _json(value: object) -> bytes:
    return canonical_json_bytes(value)


def _hash(value: object) -> str:
    return hashlib.sha256(_json(value)).hexdigest()


def stable_draft_id(target_key: str, output_hash: str, output_index: int) -> str:
    if not isinstance(target_key, str) or not target_key or not isinstance(output_hash, str) or not output_hash:
        raise ReductionContractError('draft identity fields are invalid')
    if not isinstance(output_index, int) or isinstance(output_index, bool) or output_index < 0:
        raise ReductionContractError('output index is invalid')
    return _hash({'target_key': target_key, 'output_hash': output_hash, 'output_index': output_index})


stable_leaf_id = stable_draft_id
stable_reduction_id = stable_draft_id


@dataclass(frozen=True, slots=True)
class DraftRef:
    draft_id: str
    source_stage_key: str
    source_output_hash: str
    output_index: int

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value
            for value in (self.draft_id, self.source_stage_key, self.source_output_hash)
        ):
            raise ReductionContractError('draft reference fields are invalid')
        if not isinstance(self.output_index, int) or isinstance(self.output_index, bool) or self.output_index < 0:
            raise ReductionContractError('draft reference index is invalid')

    def as_manifest(self) -> dict[str, object]:
        return {
            'draft_id': self.draft_id,
            'source_stage_key': self.source_stage_key,
            'source_output_hash': self.source_output_hash,
            'output_index': self.output_index,
        }


@dataclass(frozen=True, slots=True)
class ReductionDraft:
    draft_id: str
    title: str
    body: str
    confidence: Decimal
    source_ids: tuple[str, ...]
    source_stage_ids: tuple[str, ...] = ()
    anchor_ids: tuple[str, ...] = ()
    kind: str = ''
    source_stage_key: str = ''
    source_output_hash: str = ''
    output_index: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.draft_id, str) or not self.draft_id:
            raise ReductionContractError('draft id is invalid')
        if not isinstance(self.title, str) or not self.title or len(self.title) > MAX_TITLE:
            raise ReductionContractError('title is invalid')
        if not isinstance(self.body, str) or not self.body or len(self.body) > MAX_BODY:
            raise ReductionContractError('body is invalid')
        if not isinstance(self.confidence, Decimal):
            raise ReductionContractError('confidence must be Decimal')
        if not self.source_ids or len(set(self.source_ids)) != len(self.source_ids):
            raise ReductionContractError('source ids must be non-empty and duplicate-free')
        if any(not isinstance(item, str) or not item for item in self.source_ids):
            raise ReductionContractError('source ids must be strings')
        for field_name, values in (
            ('source_stage_ids', self.source_stage_ids),
            ('anchor_ids', self.anchor_ids),
        ):
            if any(not isinstance(item, str) or not item for item in values):
                raise ReductionContractError(f'{field_name} must contain strings')
        if not self.confidence.is_finite() or not Decimal('0') <= self.confidence <= Decimal('1'):
            raise ReductionContractError('confidence is outside [0, 1]')

    @property
    def ref(self) -> DraftRef:
        return DraftRef(
            self.draft_id,
            self.source_stage_key or self.draft_id,
            self.source_output_hash or self.draft_id,
            self.output_index,
        )

    @property
    def supporting_observation_ids(self) -> tuple[str, ...]:
        return self.source_ids

    @property
    def lineage_stage_ids(self) -> tuple[str, ...]:
        return self.source_stage_ids


@dataclass(frozen=True, slots=True)
class ReducedMemory:
    title: str
    body: str
    confidence: Decimal
    source_ids: tuple[str, ...]
    kind: str = ''


@dataclass(frozen=True, slots=True)
class ReductionOutput:
    memories: tuple[ReducedMemory, ...]


@dataclass(frozen=True, slots=True)
class ReductionBatch:
    level: int
    ordinal: int
    input_refs: tuple[DraftRef, ...]
    input_drafts: tuple[ReductionDraft, ...]
    input_hash: str
    target_key: str
    provider_required: bool = True

    @property
    def manifest(self) -> dict[str, object]:
        return {
            'schema': REDUCTION_MANIFEST_SCHEMA,
            'level': self.level,
            'ordinal': self.ordinal,
            'refs': [ref.as_manifest() for ref in self.input_refs],
        }

    @property
    def refs(self) -> tuple[DraftRef, ...]:
        return self.input_refs

    @property
    def batch_key(self) -> str:
        return self.target_key

    @property
    def input_manifest(self) -> dict[str, object]:
        return self.manifest


def _confidence(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal, str)):
        raise ReductionContractError('confidence must be numeric')
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ReductionContractError('confidence must be numeric') from error
    if not result.is_finite() or result < 0 or result > 1:
        raise ReductionContractError('confidence must be finite and within [0, 1]')
    return result


def parse_reduction_output(  # noqa: C901
    payload: object, inputs: Sequence[ReductionDraft]
) -> ReductionOutput:
    if not isinstance(payload, dict) or set(payload) != {'memories'} or not isinstance(payload['memories'], list):
        raise ReductionContractError('reduction output must be exactly {memories: [...]}')
    allowed = {'title', 'body', 'confidence', 'source_refs', 'kind'}
    n = len(inputs)
    memories: list[ReducedMemory] = []
    covered: list[int] = []
    for item in payload['memories']:
        if (
            not isinstance(item, dict)
            or not set(item).issubset(allowed)
            or not {'title', 'body', 'confidence', 'source_refs'} <= set(item)
        ):
            raise ReductionContractError('memory has malformed keys')
        title, body = item['title'], item['body']
        if not isinstance(title, str) or not title.strip() or len(title) > MAX_TITLE:
            raise ReductionContractError('title is invalid')
        if not isinstance(body, str) or not body.strip() or len(body) > MAX_BODY:
            raise ReductionContractError('body is invalid')
        source_refs = item['source_refs']
        if not isinstance(source_refs, list) or not source_refs:
            raise ReductionContractError('source_refs must be non-empty')
        indices: list[int] = []
        seen: set[int] = set()
        for ref in source_refs:
            if isinstance(ref, bool) or not isinstance(ref, int):
                raise ReductionContractError('source_refs must be integers')
            if ref < 1 or ref > n:
                raise ReductionContractError('source_refs index is out of range')
            if ref in seen:
                raise ReductionContractError('source_refs must be duplicate-free')
            seen.add(ref)
            indices.append(ref)
        kind = item.get('kind', '')
        if not isinstance(kind, str) or (
            kind and kind not in {'decision', 'convention', 'gotcha', 'architecture', 'incident'}
        ):
            raise ReductionContractError('kind is unknown or not permitted')
        confidence_value = item['confidence']
        if isinstance(confidence_value, bool) or not isinstance(confidence_value, (int, float)):
            raise ReductionContractError('confidence must be numeric')
        source_ids = tuple(inputs[index - 1].draft_id for index in indices)
        source_ceiling = max(inputs[index - 1].confidence for index in indices)
        confidence = min(_confidence(confidence_value), source_ceiling)
        covered.extend(indices)
        memories.append(ReducedMemory(title, body, confidence, source_ids, kind))
    if n:
        if len(covered) != len(set(covered)):
            raise ReductionContractError('source_refs index repeats across memories')
        if set(covered) != set(range(1, n + 1)):
            raise ReductionContractError('reduction output must partition every draft')

    return ReductionOutput(tuple(memories))


parse_reduction_json = parse_reduction_output


def _draft_payload(draft: ReductionDraft) -> dict[str, object]:
    payload: dict[str, object] = {
        'id': draft.draft_id,
        'title': draft.title,
        'body': draft.body,
        'confidence': str(draft.confidence),
    }
    if draft.kind:
        payload['kind'] = draft.kind

    return payload


def reduction_input_hash(refs: Sequence[DraftRef]) -> str:
    return _hash({'schema': REDUCTION_MANIFEST_SCHEMA, 'refs': [ref.as_manifest() for ref in refs]})


def reduction_batch_key(*, level: int, ordinal: int, input_hash: str) -> str:
    return _hash(
        {
            'schema': 'distillation_reduce_batch.v1',
            'level': level,
            'ordinal': ordinal,
            'input_hash': input_hash,
        }
    )


reduction_target_key = reduction_batch_key


def build_reduction_batches(
    drafts: Sequence[ReductionDraft], *, max_fanin: int, level: int
) -> tuple[ReductionBatch, ...]:
    if max_fanin < 1:
        raise ReductionContractError('reduction fan-in must be positive')
    ordered = tuple(drafts)
    batches: list[ReductionBatch] = []
    index = 0
    while index < len(ordered):
        size = min(max_fanin, len(ordered) - index)
        group = ordered[index : index + size]
        index += size
        provider_required = len(group) >= 2
        refs = tuple(draft.ref for draft in group)
        input_hash = reduction_input_hash(refs)
        batches.append(
            ReductionBatch(
                level,
                len(batches),
                refs,
                group,
                input_hash,
                reduction_target_key(level=level, ordinal=len(batches), input_hash=input_hash),
                provider_required,
            )
        )

    return tuple(batches)


Provider = Callable[[ReductionBatch], object]


def _materialize_reduced(batch: ReductionBatch, parsed: ReductionOutput) -> tuple[ReductionDraft, ...]:
    by_id = {draft.draft_id: draft for draft in batch.input_drafts}
    result: list[ReductionDraft] = []
    for index, memory in enumerate(parsed.memories):
        source_drafts = [by_id[source_id] for source_id in memory.source_ids]
        observation_ids = tuple(dict.fromkeys(item for draft in source_drafts for item in draft.source_ids))
        stage_ids = tuple(dict.fromkeys(item for draft in source_drafts for item in draft.source_stage_ids))
        anchor_ids = tuple(dict.fromkeys(item for draft in source_drafts for item in draft.anchor_ids))
        output_hash = _hash(
            {
                'title': memory.title,
                'body': memory.body,
                'confidence': str(memory.confidence),
                'source_ids': list(memory.source_ids),
                'kind': memory.kind,
            }
        )
        result.append(
            ReductionDraft(
                stable_draft_id(batch.target_key, output_hash, index),
                memory.title,
                memory.body,
                memory.confidence,
                observation_ids,
                stage_ids,
                anchor_ids,
                memory.kind,
                batch.target_key,
                output_hash,
                index,
            )
        )
    return tuple(result)


def reduce_multilevel(
    drafts: Sequence[ReductionDraft],
    *,
    reduction_target_floor: int,
    output_budget_tokens: int,
    generation: int,
    provider: Provider,
) -> tuple[ReductionDraft, ...]:
    if any(not isinstance(draft, ReductionDraft) for draft in drafts):
        raise ReductionContractError('reduction drafts must be typed ReductionDraft instances')
    accepted: list[_AcceptedReduction] = []
    while True:
        state = _evaluate_draft_reduction_state(
            tuple(drafts),
            accepted,
            reduction_target_floor=reduction_target_floor,
            output_budget_tokens=output_budget_tokens,
            generation=generation,
        )
        if state.final is not None:
            return state.final
        if state.pending is None:
            raise ReductionContractError('reduction state is incomplete')
        parsed = parse_reduction_output(provider(state.pending), state.pending.input_drafts)
        accepted.append(
            _AcceptedReduction(
                state.pending.level,
                state.pending.ordinal,
                state.pending.input_hash,
                _materialize_reduced(state.pending, parsed),
            )
        )


def _require_stage(stage: DistillationStage) -> DistillationStage:
    from engram.core.models import DistillationStage

    if not isinstance(stage, DistillationStage):
        raise ReductionContractError('reduction stages must be DistillationStage instances')
    return stage


def _snapshot_drafts(stage: DistillationStage) -> tuple[ReductionDraft, ...]:  # noqa: C901
    from engram.core.models import DistillationStageKind, DistillationStageStatus

    stage = _require_stage(stage)
    if stage.status != DistillationStageStatus.COMPLETE or stage.output_snapshot is None:
        raise ReductionContractError('reduction input stage must be complete with an output snapshot')
    if any(not isinstance(value, str) or not value for value in (stage.target_key, stage.stage_key, stage.output_hash)):
        raise ReductionContractError('distillation stage identity is invalid')
    snapshot = stage.output_snapshot
    if not isinstance(snapshot, dict) or set(snapshot) != (
        {'memories', 'no_signal_observation_ids'} if stage.stage_kind == DistillationStageKind.EXTRACT else {'memories'}
    ):
        raise ReductionContractError('distillation stage snapshot does not match its v1 contract')
    outputs = snapshot['memories']
    if not isinstance(outputs, list):
        raise ReductionContractError('distillation stage memories must be a list')
    if stage.stage_kind == DistillationStageKind.EXTRACT:
        no_signal = snapshot['no_signal_observation_ids']
        if (
            not isinstance(no_signal, list)
            or any(not isinstance(item, str) or not item for item in no_signal)
            or len(set(no_signal)) != len(no_signal)
        ):
            raise ReductionContractError('distillation no-signal ids are invalid')
    if stage.stage_kind not in (DistillationStageKind.EXTRACT, DistillationStageKind.REDUCE):
        raise ReductionContractError('unsupported distillation stage kind')

    result: list[ReductionDraft] = []
    for index, output in enumerate(outputs):
        if not isinstance(output, dict):
            raise ReductionContractError('distillation memory must be an object')
        if stage.stage_kind == DistillationStageKind.EXTRACT:
            allowed = {'title', 'body', 'confidence', 'supporting_observation_ids', 'kind'}
            source_key = 'supporting_observation_ids'
        else:
            allowed = {'title', 'body', 'confidence', 'source_ids', 'kind'}
            source_key = 'source_ids'
        if not set(output).issubset(allowed) or not {'title', 'body', 'confidence', source_key} <= set(output):
            raise ReductionContractError('distillation memory has malformed keys')
        source_ids = output[source_key]
        if not isinstance(source_ids, list) or any(not isinstance(item, str) or not item for item in source_ids):
            raise ReductionContractError('distillation memory source ids are invalid')
        if len(set(source_ids)) != len(source_ids):
            raise ReductionContractError('distillation memory source ids are duplicate-free')
        kind = output.get('kind', '')
        if not isinstance(kind, str) or (
            kind and kind not in {'decision', 'convention', 'gotcha', 'architecture', 'incident'}
        ):
            raise ReductionContractError('distillation memory kind is unknown')
        output_hash = _hash(
            {
                'title': output['title'],
                'body': output['body'],
                'confidence': str(output['confidence']),
                'source_ids': source_ids,
                'kind': kind,
            }
        )
        draft_id = stable_draft_id(stage.target_key, output_hash, index)
        result.append(
            ReductionDraft(
                draft_id=draft_id,
                title=output['title'],
                body=output['body'],
                confidence=_confidence(output['confidence']),
                source_ids=tuple(source_ids),
                source_stage_ids=(stage.stage_key,),
                kind=kind,
                source_stage_key=stage.stage_key,
                source_output_hash=stage.output_hash,
                output_index=index,
            )
        )
    return tuple(result)


def _hydrate_input_drafts(stage: DistillationStage) -> tuple[ReductionDraft, ...]:
    from engram.core.models import DistillationStageKind, DistillationStageStatus

    stage = _require_stage(stage)
    if stage.stage_kind != DistillationStageKind.REDUCE:
        raise ReductionContractError('reduction input hydration requires a reduce stage')
    if stage.status not in (DistillationStageStatus.REQUIRED, DistillationStageStatus.COMPLETE):
        raise ReductionContractError('reduction stage status is invalid')
    manifest = stage.input_manifest
    if not isinstance(manifest, dict) or set(manifest) != {'schema', 'level', 'ordinal', 'refs'}:
        raise ReductionContractError('reduction input manifest is invalid')
    refs = manifest['refs']
    if not isinstance(refs, list) or not refs:
        raise ReductionContractError('reduction input manifest refs are invalid')
    result: list[ReductionDraft] = []
    candidates = tuple(stage.window.stages.filter(status=DistillationStageStatus.COMPLETE))
    for ref in refs:
        if not isinstance(ref, dict) or set(ref) != {
            'draft_id',
            'source_stage_key',
            'source_output_hash',
            'output_index',
        }:
            raise ReductionContractError('reduction input ref is invalid')
        matches = [
            draft
            for source_stage in candidates
            for draft in _snapshot_drafts(source_stage)
            if draft.ref.as_manifest() == ref
        ]
        if len(matches) != 1:
            raise ReductionContractError('reduction input ref is missing or ambiguous')
        result.append(matches[0])
    return tuple(result)


def _expand_reduced_drafts(
    drafts: Sequence[ReductionDraft],
    inputs: Sequence[ReductionDraft],
) -> tuple[ReductionDraft, ...]:
    by_id = {draft.draft_id: draft for draft in inputs}
    expanded: list[ReductionDraft] = []
    for draft in drafts:
        source_drafts = [by_id[source_id] for source_id in draft.source_ids if source_id in by_id]
        if not source_drafts:
            expanded.append(draft)
            continue
        expanded.append(
            ReductionDraft(
                draft.draft_id,
                draft.title,
                draft.body,
                draft.confidence,
                tuple(dict.fromkeys(item for source in source_drafts for item in source.source_ids)),
                tuple(dict.fromkeys(item for source in source_drafts for item in source.source_stage_ids)),
                tuple(dict.fromkeys(item for source in source_drafts for item in source.anchor_ids)),
                draft.kind,
                draft.source_stage_key,
                draft.source_output_hash,
                draft.output_index,
            )
        )
    return tuple(expanded)


@dataclass(frozen=True, slots=True)
class _AcceptedReduction:
    level: int
    ordinal: int
    input_hash: str
    drafts: tuple[ReductionDraft, ...]

    @property
    def batch_key(self) -> str:
        return reduction_batch_key(level=self.level, ordinal=self.ordinal, input_hash=self.input_hash)


@dataclass(frozen=True, slots=True)
class _ReductionState:
    current: tuple[ReductionDraft, ...]
    pending: ReductionBatch | None
    final: tuple[ReductionDraft, ...] | None


def _evaluate_draft_reduction_state(
    initial_drafts: tuple[ReductionDraft, ...],
    accepted_rows: Sequence[_AcceptedReduction],
    *,
    reduction_target_floor: int,
    output_budget_tokens: int,
    generation: int,
) -> _ReductionState:
    if reduction_target_floor <= 0 or output_budget_tokens <= 0:
        return _ReductionState((), None, ())
    if len({row.batch_key for row in accepted_rows}) != len(accepted_rows):
        raise ReductionContractError('accepted reduction identities are duplicate')
    accepted_by_key = {row.batch_key: row for row in accepted_rows}
    target = effective_reduction_target(len(initial_drafts), reduction_target_floor)
    current = initial_drafts
    if len(current) <= target:
        return _ReductionState(current, None, current)
    budget = output_budget_tokens >> generation
    max_fanin = max_reduction_fanin(budget)
    level_base = generation * _GENERATION_LEVEL_STRIDE
    for tree_level in range(1, _MAX_TREE_LEVELS + 1):
        if len(current) <= target:
            break
        level = level_base + tree_level
        batches = build_reduction_batches(current, max_fanin=max_fanin, level=level)
        next_level: list[ReductionDraft] = []
        for batch in batches:
            if not batch.provider_required:
                next_level.extend(batch.input_drafts)
                continue
            accepted = accepted_by_key.get(batch.batch_key)
            if accepted is None:
                return _ReductionState(current, batch, None)
            next_level.extend(_expand_reduced_drafts(accepted.drafts, batch.input_drafts))
        if len(next_level) == len(current):
            return _ReductionState(current, None, current)
        current = tuple(next_level)

    return _ReductionState(current, None, current)


def _accepted_reduction(stage: DistillationStage) -> _AcceptedReduction:
    from engram.core.models import DistillationStageKind, DistillationStageStatus

    stage = _require_stage(stage)
    if stage.stage_kind != DistillationStageKind.REDUCE or stage.status != DistillationStageStatus.COMPLETE:
        raise ReductionContractError('accepted reductions must be complete reduce stages')
    if (
        type(stage.level) is not int
        or type(stage.ordinal) is not int
        or not isinstance(stage.input_hash, str)
        or not stage.input_hash
    ):
        raise ReductionContractError('accepted reduction identity is invalid')
    return _AcceptedReduction(stage.level, stage.ordinal, stage.input_hash, _snapshot_drafts(stage))


def _evaluate_reduction_state(
    extraction_stages: Sequence[DistillationStage],
    accepted_stages: Sequence[DistillationStage],
    *,
    reduction_target_floor: int,
    output_budget_tokens: int,
    generation: int,
) -> _ReductionState:
    from engram.core.models import DistillationStageKind, DistillationStageStatus

    extraction = tuple(_require_stage(stage) for stage in extraction_stages)
    accepted_rows = tuple(_accepted_reduction(stage) for stage in accepted_stages)
    if any(stage.stage_kind != DistillationStageKind.EXTRACT for stage in extraction):
        raise ReductionContractError('reduction extraction inputs must be extract stages')
    if any(stage.status != DistillationStageStatus.COMPLETE for stage in extraction):
        return _ReductionState((), None, ())
    if extraction:
        window_id = extraction[0].window_id
        scope = (extraction[0].organization_id, extraction[0].project_id, extraction[0].team_id)
        if any(
            stage.window_id != window_id or (stage.organization_id, stage.project_id, stage.team_id) != scope
            for stage in extraction
        ):
            raise ReductionContractError('reduction extraction stages are out of scope')
        if any(
            stage.window_id != window_id or (stage.organization_id, stage.project_id, stage.team_id) != scope
            for stage in accepted_stages
        ):
            raise ReductionContractError('reduction accepted stages are out of scope')
    current = tuple(draft for stage in extraction for draft in _snapshot_drafts(stage))
    return _evaluate_draft_reduction_state(
        current,
        accepted_rows,
        reduction_target_floor=reduction_target_floor,
        output_budget_tokens=output_budget_tokens,
        generation=generation,
    )


def derive_first_pending_reduction_target(
    extraction_targets: Sequence[DistillationStage],
    accepted_targets: Sequence[DistillationStage],
    *,
    reduction_target_floor: int,
    output_budget_tokens: int,
    generation: int,
) -> ReductionBatch | None:
    return _evaluate_reduction_state(
        extraction_targets,
        accepted_targets,
        reduction_target_floor=reduction_target_floor,
        output_budget_tokens=output_budget_tokens,
        generation=generation,
    ).pending


def _accepted_batch_key(target: DistillationStage) -> str:
    return _accepted_reduction(target).batch_key


def derive_final_reduction_drafts(
    extraction_targets: Sequence[DistillationStage],
    accepted_targets: Sequence[DistillationStage],
    *,
    reduction_target_floor: int,
    output_budget_tokens: int,
    generation: int,
) -> tuple[ReductionDraft, ...]:
    result = _evaluate_reduction_state(
        extraction_targets,
        accepted_targets,
        reduction_target_floor=reduction_target_floor,
        output_budget_tokens=output_budget_tokens,
        generation=generation,
    )

    return result.final or ()


@dataclass(frozen=True, slots=True)
class ReductionStageContract:
    stage_kind: str = 'reduce'
    prompt_contract: str = 'distill_reduce.v2'
    response_kind: str = 'distill_reduce.v2'

    def prepare_call(self, stage: DistillationStage) -> object:
        from engram.memory.distillation_provider_stage import PreparedProviderStageCall

        inputs = _hydrate_input_drafts(stage)
        drafts = []
        for index, draft in enumerate(inputs, start=1):
            entry = {
                'index': index,
                'title': draft.title,
                'body': draft.body,
                'confidence': str(draft.confidence),
            }
            if draft.kind:
                entry['kind'] = draft.kind
            drafts.append(entry)
        prompt = json.dumps(
            {'drafts': drafts},
            ensure_ascii=False,
            separators=(',', ':'),
        )
        return PreparedProviderStageCall(
            prompt=prompt,
            system_prompt=_REDUCE_SYSTEM_PROMPT,
            response_kind=self.response_kind,
        )

    def normalize_output(self, raw_body: str, *, stage: DistillationStage) -> dict[str, object]:
        from engram.memory.distillation_provider_stage import ProviderStageOutputError

        try:
            payload = json.loads(raw_body)
            inputs = _hydrate_input_drafts(stage)
            parsed = parse_reduction_output(payload, inputs)
            memories: list[dict[str, object]] = []
            for memory in parsed.memories:
                entry: dict[str, object] = {
                    'title': memory.title,
                    'body': memory.body,
                    'confidence': str(memory.confidence),
                    'source_ids': list(memory.source_ids),
                }
                if memory.kind:
                    entry['kind'] = memory.kind
                memories.append(entry)
            return {'memories': memories}
        except (TypeError, ValueError, ReductionContractError) as error:
            raise ProviderStageOutputError('reduction provider output is malformed') from error


def provider_stage_target(window: DistillationWindow, batch: ReductionBatch) -> object:
    from engram.core.models import DistillationWindow
    from engram.memory.distillation_provider_stage import ProviderStageTarget

    if not isinstance(batch, ReductionBatch) or batch.level < 1:
        raise ReductionContractError('reduction batch must have level >= 1')
    if not isinstance(window, DistillationWindow):
        raise ReductionContractError('reduction window must be a DistillationWindow instance')
    return ProviderStageTarget(
        window_id=window.id,
        chunk_id=None,
        stage_kind='reduce',
        level=batch.level,
        ordinal=batch.ordinal,
        input_manifest=batch.manifest,
        input_hash=batch.input_hash,
        prompt_contract=REDUCE_PROMPT_CONTRACT,
    )


def resolve_reduction_stage(*args: Any, **kwargs: Any) -> Any:
    from engram.memory import distillation_provider_stage as engine

    resolver = getattr(engine, 'resolve_provider_stage', None)
    if resolver is None:
        raise RuntimeError('generic provider-stage resolver is unavailable')
    return resolver(*args, **kwargs)


def execute_reduction_stage(*args: Any, **kwargs: Any) -> Any:
    from engram.memory import distillation_provider_stage as engine

    executor = getattr(engine, 'execute_provider_stage', None)
    if executor is None:
        raise RuntimeError('generic provider-stage executor is unavailable')
    kwargs.setdefault('contract', ReductionStageContract())
    return executor(*args, **kwargs)
