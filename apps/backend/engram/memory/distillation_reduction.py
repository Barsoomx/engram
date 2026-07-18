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
REDUCE_PROMPT_CONTRACT = 'distill_reduce.v1'


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
    payload: object, inputs: Sequence[ReductionDraft], *, reduction_target: int | None = None
) -> ReductionOutput:
    if not isinstance(payload, dict) or set(payload) != {'memories'} or not isinstance(payload['memories'], list):
        raise ReductionContractError('reduction output must be exactly {memories: [...]}')
    allowed = {'title', 'body', 'confidence', 'source_ids', 'kind'}
    known = {draft.draft_id for draft in inputs}
    memories: list[ReducedMemory] = []
    for item in payload['memories']:
        if (
            not isinstance(item, dict)
            or not set(item).issubset(allowed)
            or not {'title', 'body', 'confidence', 'source_ids'} <= set(item)
        ):
            raise ReductionContractError('memory has malformed keys')
        title, body = item['title'], item['body']
        if not isinstance(title, str) or not title.strip() or len(title) > MAX_TITLE:
            raise ReductionContractError('title is invalid')
        if not isinstance(body, str) or not body.strip() or len(body) > MAX_BODY:
            raise ReductionContractError('body is invalid')
        source_ids = item['source_ids']
        if not isinstance(source_ids, list) or not source_ids:
            raise ReductionContractError('source_ids must be non-empty')
        if any(not isinstance(source_id, str) or not source_id for source_id in source_ids):
            raise ReductionContractError('source_ids must contain strings')
        if len(set(source_ids)) != len(source_ids):
            raise ReductionContractError('source_ids must be duplicate-free')
        if not set(source_ids) <= known:
            raise ReductionContractError('source_ids references an unknown draft')
        kind = item.get('kind', '')
        if not isinstance(kind, str) or (
            kind and kind not in {'decision', 'convention', 'gotcha', 'architecture', 'incident'}
        ):
            raise ReductionContractError('kind is unknown or not permitted')
        confidence_value = item['confidence']
        if isinstance(confidence_value, bool) or not isinstance(confidence_value, (int, float)):
            raise ReductionContractError('confidence must be numeric')

        memories.append(ReducedMemory(title, body, _confidence(confidence_value), tuple(source_ids), kind))
    if inputs and not memories:
        raise ReductionContractError('reduction output is empty')
    if inputs:
        covered = {source_id for memory in memories for source_id in memory.source_ids}
        if covered != known:
            raise ReductionContractError('reduction output does not cover every input')
        target = reduction_target if reduction_target is not None else len(inputs) - 1
        if len(inputs) > 1 and len(memories) >= len(inputs):
            raise ReductionContractError('reduction output must shrink')
        cap = max(target, math.ceil(len(inputs) / 2)) if len(inputs) > target else target
        if len(memories) > cap:
            raise ReductionContractError('reduction output exceeds cap')
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
    drafts: Sequence[ReductionDraft], *, reduction_target: int, prompt_budget: int, level: int = 1
) -> tuple[ReductionBatch, ...]:
    if reduction_target <= 0 or prompt_budget <= 0:
        raise ReductionContractError('reduction target and prompt budget must be positive')
    ordered = tuple(drafts)
    batches: list[ReductionBatch] = []
    index = 0
    while index < len(ordered):
        remaining = len(ordered) - index
        if remaining == 1:
            group = (ordered[index],)
            index += 1
            provider_required = False
        else:
            group_list = [ordered[index]]
            index += 1
            while index < len(ordered):
                candidate = tuple(group_list + [ordered[index]])
                size = len(_json([_draft_payload(draft) for draft in candidate]))
                if len(candidate) >= 2 and size > prompt_budget:
                    break
                group_list.append(ordered[index])
                index += 1
            if len(group_list) < 2:
                raise ReductionContractError('two normalized drafts do not fit prompt budget')
            group = tuple(group_list)
            provider_required = True
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
    drafts: Sequence[ReductionDraft], *, reduction_target: int, prompt_budget: int, provider: Provider
) -> tuple[ReductionDraft, ...]:
    if any(not isinstance(draft, ReductionDraft) for draft in drafts):
        raise ReductionContractError('reduction drafts must be typed ReductionDraft instances')
    accepted: list[_AcceptedReduction] = []
    while True:
        state = _evaluate_draft_reduction_state(
            tuple(drafts),
            accepted,
            reduction_target=reduction_target,
            prompt_budget=prompt_budget,
        )
        if state.final is not None:
            return state.final
        if state.pending is None:
            raise ReductionContractError('reduction state is incomplete')
        parsed = parse_reduction_output(
            provider(state.pending), state.pending.input_drafts, reduction_target=reduction_target
        )
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
    reduction_target: int,
    prompt_budget: int,
) -> _ReductionState:
    if reduction_target <= 0 or prompt_budget <= 0:
        return _ReductionState((), None, ())
    if len({row.batch_key for row in accepted_rows}) != len(accepted_rows):
        raise ReductionContractError('accepted reduction identities are duplicate')
    accepted_by_key = {row.batch_key: row for row in accepted_rows}
    current = initial_drafts
    if len(current) <= reduction_target:
        return _ReductionState(current, None, current)
    level = 1
    while len(current) > reduction_target:
        batches = build_reduction_batches(
            current,
            reduction_target=reduction_target,
            prompt_budget=prompt_budget,
            level=level,
        )
        next_level: list[ReductionDraft] = []
        for batch in batches:
            if not batch.provider_required:
                next_level.extend(batch.input_drafts)
                continue
            accepted = accepted_by_key.get(batch.batch_key)
            if accepted is None:
                return _ReductionState(current, batch, None)
            next_level.extend(_expand_reduced_drafts(accepted.drafts, batch.input_drafts))
        if len(next_level) >= len(current):
            raise ReductionContractError('reduction level did not shrink')
        current = tuple(next_level)
        level += 1
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
    reduction_target: int,
    prompt_budget: int,
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
        reduction_target=reduction_target,
        prompt_budget=prompt_budget,
    )


def derive_first_pending_reduction_target(
    extraction_targets: Sequence[DistillationStage],
    accepted_targets: Sequence[DistillationStage],
    *,
    reduction_target: int,
    prompt_budget: int,
) -> ReductionBatch | None:
    return _evaluate_reduction_state(
        extraction_targets,
        accepted_targets,
        reduction_target=reduction_target,
        prompt_budget=prompt_budget,
    ).pending


def _accepted_batch_key(target: DistillationStage) -> str:
    return _accepted_reduction(target).batch_key


def derive_final_reduction_drafts(
    extraction_targets: Sequence[DistillationStage],
    accepted_targets: Sequence[DistillationStage],
    *,
    reduction_target: int,
    prompt_budget: int = 120_000,
) -> tuple[ReductionDraft, ...]:
    result = _evaluate_reduction_state(
        extraction_targets,
        accepted_targets,
        reduction_target=reduction_target,
        prompt_budget=prompt_budget,
    )
    return result.final or ()


@dataclass(frozen=True, slots=True)
class ReductionStageContract:
    stage_kind: str = 'reduce'
    prompt_contract: str = 'distill_reduce.v1'
    response_kind: str = 'distill_reduce.v1'

    def prepare_call(self, stage: DistillationStage) -> object:
        from engram.memory.distillation_provider_stage import PreparedProviderStageCall

        inputs = _hydrate_input_drafts(stage)
        drafts = []
        for draft in inputs:
            entry = {
                'id': draft.draft_id,
                'title': draft.title,
                'body': draft.body,
                'confidence': str(draft.confidence),
            }
            if draft.kind:
                entry['kind'] = draft.kind
            drafts.append(entry)
        prompt = json.dumps(
            {'drafts': drafts, 'reduction_target': stage.window.reduction_target},
            ensure_ascii=False,
            separators=(',', ':'),
        )
        return PreparedProviderStageCall(
            prompt=prompt,
            system_prompt='Return exactly a JSON object with the memories key following distill_reduce.v1.',
            response_kind=self.response_kind,
        )

    def normalize_output(self, raw_body: str, *, stage: DistillationStage) -> dict[str, object]:
        from engram.memory.distillation_provider_stage import ProviderStageOutputError

        try:
            payload = json.loads(raw_body)
            inputs = _hydrate_input_drafts(stage)
            stage = _require_stage(stage)
            reduction_target = stage.window.reduction_target
            parsed = parse_reduction_output(payload, inputs, reduction_target=reduction_target)
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
