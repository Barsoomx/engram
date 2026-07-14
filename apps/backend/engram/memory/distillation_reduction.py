from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from engram.memory.workflow_work import canonical_json_bytes


class ReductionContractError(ValueError):
    pass


MAX_MEMORIES = 12
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
        if not self.source_ids or len(set(self.source_ids)) != len(self.source_ids):
            raise ReductionContractError('source ids must be non-empty and duplicate-free')
        if any(not isinstance(item, str) or not item for item in self.source_ids):
            raise ReductionContractError('source ids must be strings')
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
        if not isinstance(title, str) or not title or len(title) > MAX_TITLE:
            raise ReductionContractError('title is invalid')
        if not isinstance(body, str) or not body or len(body) > MAX_BODY:
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
        memories.append(ReducedMemory(title, body, _confidence(item['confidence']), tuple(source_ids), kind))
    if len(memories) > MAX_MEMORIES:
        raise ReductionContractError('too many memories')
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
    return {
        'id': draft.draft_id,
        'title': draft.title,
        'body': draft.body,
        'confidence': str(draft.confidence),
        'source_ids': list(draft.source_ids),
        'kind': draft.kind,
    }


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
    current = tuple(drafts)
    level = 1
    while len(current) > reduction_target:
        next_level: list[ReductionDraft] = []
        for batch in build_reduction_batches(
            current, reduction_target=reduction_target, prompt_budget=prompt_budget, level=level
        ):
            if not batch.provider_required:
                next_level.extend(batch.input_drafts)
                continue
            parsed = parse_reduction_output(provider(batch), batch.input_drafts, reduction_target=reduction_target)
            next_level.extend(_materialize_reduced(batch, parsed))
        if len(next_level) >= len(current):
            raise ReductionContractError('reduction level did not shrink')
        current = tuple(next_level)
        level += 1
    return current


def _value(item: object, key: str, default: object = None) -> object:
    if isinstance(item, Mapping):
        return item.get(key, default)
    return getattr(item, key, default)


def _snapshot_drafts(snapshot: object) -> tuple[ReductionDraft, ...]:
    outputs = _value(snapshot, 'outputs', _value(snapshot, 'drafts', None))
    if outputs is None:
        output_snapshot = _value(snapshot, 'output_snapshot', {})
        outputs = _value(output_snapshot, 'memories', ())
    if outputs is None:
        return ()
    target_key = str(_value(snapshot, 'target_key', _value(snapshot, 'stage_key', 'extraction')))
    source_stage_key = str(_value(snapshot, 'stage_key', target_key))
    output_hash = str(_value(snapshot, 'output_hash', ''))

    def draft_hash(output: object) -> str:
        value = {
            'title': str(_value(output, 'title')),
            'body': str(_value(output, 'body')),
            'confidence': str(_value(output, 'confidence')),
            'source_ids': list(_value(output, 'source_ids', _value(output, 'supporting_observation_ids', ()))),
            'kind': str(_value(output, 'kind', '')),
        }
        return _hash(value)

    return tuple(
        output
        if isinstance(output, ReductionDraft)
        else ReductionDraft(
            draft_id=str(
                _value(output, 'draft_id')
                or stable_draft_id(
                    target_key,
                    output_hash or draft_hash(output),
                    int(_value(output, 'output_index', index)),
                )
            ),
            title=str(_value(output, 'title')),
            body=str(_value(output, 'body')),
            confidence=_confidence(_value(output, 'confidence')),
            source_ids=tuple(_value(output, 'source_ids', _value(output, 'supporting_observation_ids', ()))),
            source_stage_ids=tuple(_value(output, 'source_stage_ids', ())),
            anchor_ids=tuple(_value(output, 'anchor_ids', ())),
            kind=str(_value(output, 'kind', '')),
            source_stage_key=str(_value(output, 'source_stage_key', source_stage_key)),
            source_output_hash=str(_value(output, 'source_output_hash', output_hash or draft_hash(output))),
            output_index=int(_value(output, 'output_index', index)),
        )
        for index, output in enumerate(outputs)
    )


def _stage_rows(stage: object) -> tuple[object, ...]:
    direct = _value(stage, 'source_stages', None)
    if direct is not None:
        return tuple(direct)
    window = _value(stage, 'window', None)
    if window is not None:
        direct = _value(window, 'stages', None)
        if direct is not None:
            if hasattr(direct, 'all'):
                return tuple(direct.all())
            return tuple(direct)
        direct = _value(window, 'accepted_stages', None)
        if direct is not None:
            return tuple(direct)
    try:
        from engram.core.models import DistillationStage

        window_id = _value(stage, 'window_id') or _value(window, 'id')
        if window_id is None:
            return ()
        return tuple(DistillationStage.objects.filter(window_id=window_id, status='complete'))
    except (ImportError, AttributeError):
        return ()


def _hydrate_input_drafts(stage: object) -> tuple[ReductionDraft, ...]:  # noqa: C901
    manifest = _value(stage, 'input_manifest', {})
    if not isinstance(manifest, Mapping):
        raise ReductionContractError('reduction input manifest is invalid')
    refs = manifest.get('refs', ())
    if not isinstance(refs, (list, tuple)) or not refs:
        raise ReductionContractError('reduction input manifest refs are invalid')
    candidates = _stage_rows(stage)
    window_id = _value(stage, 'window_id') or _value(_value(stage, 'window', None), 'id')
    scope_keys = ('organization_id', 'project_id', 'team_id')
    result: list[ReductionDraft] = []
    for ref in refs:
        if not isinstance(ref, Mapping):
            raise ReductionContractError('reduction input ref is invalid')
        required = ('draft_id', 'source_stage_key', 'source_output_hash', 'output_index')
        if any(key not in ref for key in required):
            raise ReductionContractError('reduction input ref is incomplete')
        matches: list[ReductionDraft] = []
        for source_stage in candidates:
            if _value(source_stage, 'status', 'complete') not in ('complete', 'completed', 'accepted'):
                continue
            source_window_id = _value(source_stage, 'window_id') or _value(_value(source_stage, 'window', None), 'id')
            if window_id is not None and source_window_id is not None and source_window_id != window_id:
                continue
            if any(
                _value(source_stage, key) is not None
                and _value(stage, key) is not None
                and _value(source_stage, key) != _value(stage, key)
                for key in scope_keys
            ):
                continue
            for draft in _snapshot_drafts(source_stage):
                if (
                    draft.draft_id == ref['draft_id']
                    and draft.source_stage_key == ref['source_stage_key']
                    and draft.source_output_hash == ref['source_output_hash']
                    and draft.output_index == ref['output_index']
                ):
                    matches.append(draft)
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


def derive_first_pending_reduction_target(
    extraction_targets: Sequence[object],
    accepted_targets: Sequence[object],
    *,
    reduction_target: int,
    prompt_budget: int,
) -> ReductionBatch | None:
    if not extraction_targets or any(
        _value(target, 'status') not in ('completed', 'accepted', 'complete') for target in extraction_targets
    ):
        return None
    drafts = tuple(draft for target in extraction_targets for draft in _snapshot_drafts(target))
    if len(drafts) <= reduction_target:
        return None
    accepted_keys = {
        _accepted_batch_key(target)
        for target in accepted_targets
        if _value(target, 'status') in ('completed', 'accepted', 'complete')
    }
    current = drafts
    level = 1
    while len(current) > reduction_target:
        batches = build_reduction_batches(
            current, reduction_target=reduction_target, prompt_budget=prompt_budget, level=level
        )
        pending = next(
            (batch for batch in batches if batch.provider_required and batch.target_key not in accepted_keys), None
        )
        if pending is not None:
            return pending
        completed = {
            _accepted_batch_key(target): target
            for target in accepted_targets
            if int(_value(target, 'level', 0)) == level and _accepted_batch_key(target) in accepted_keys
        }
        if not completed and any(batch.provider_required for batch in batches):
            return None
        current = tuple(
            draft
            for batch in batches
            for draft in (
                _expand_reduced_drafts(_snapshot_drafts(completed[batch.target_key]), batch.input_drafts)
                if batch.provider_required and batch.target_key in completed
                else batch.input_drafts
            )
        )
        level += 1
    return None


def _accepted_batch_key(target: object) -> str:
    level = _value(target, 'level')
    ordinal = _value(target, 'ordinal')
    input_hash = _value(target, 'input_hash')
    if type(level) is not int or type(ordinal) is not int or not isinstance(input_hash, str) or not input_hash:
        raise ReductionContractError('accepted reduction target identity is invalid')
    return reduction_batch_key(level=level, ordinal=ordinal, input_hash=input_hash)


def derive_final_reduction_drafts(  # noqa: C901
    extraction_targets: Sequence[object],
    accepted_targets: Sequence[object] | None = None,
    *,
    reduction_target: int,
    prompt_budget: int = 120_000,
) -> tuple[ReductionDraft, ...]:
    if accepted_targets is None:
        accepted_targets = extraction_targets
        extraction_targets = ()
    if reduction_target <= 0 or prompt_budget <= 0:
        return ()
    if not accepted_targets and not extraction_targets:
        return ()
    accepted = tuple(
        target for target in accepted_targets if _value(target, 'status') in ('completed', 'accepted', 'complete')
    )
    if len(accepted) != len(accepted_targets):
        return ()
    if not extraction_targets:
        latest_level = max(int(_value(target, 'level', 0)) for target in accepted)
        latest = [target for target in accepted if int(_value(target, 'level', 0)) == latest_level]
        drafts = tuple(
            draft
            for target in sorted(latest, key=lambda item: int(_value(item, 'ordinal', 0)))
            for draft in _snapshot_drafts(target)
        )
        return drafts if len(drafts) <= reduction_target else ()
    if any(_value(target, 'status') not in ('completed', 'accepted', 'complete') for target in extraction_targets):
        return ()
    current = tuple(draft for target in extraction_targets for draft in _snapshot_drafts(target))
    level = 1
    by_batch = {_accepted_batch_key(target): target for target in accepted}
    while len(current) > reduction_target:
        next_level: list[ReductionDraft] = []
        for batch in build_reduction_batches(
            current, reduction_target=reduction_target, prompt_budget=prompt_budget, level=level
        ):
            if not batch.provider_required:
                next_level.extend(batch.input_drafts)
                continue
            target = by_batch.get(batch.batch_key)
            if target is None:
                return ()
            next_level.extend(_expand_reduced_drafts(_snapshot_drafts(target), batch.input_drafts))
        if len(next_level) >= len(current):
            return ()
        current = tuple(next_level)
        level += 1
    return current


@dataclass(frozen=True, slots=True)
class ReductionStageContract:
    stage_kind: str = 'reduce'
    prompt_contract: str = 'distill_reduce.v1'
    response_kind: str = 'distill_reduce.v1'

    def prepare_call(self, stage: object) -> object:
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
            {'drafts': drafts},
            ensure_ascii=False,
            separators=(',', ':'),
        )
        return PreparedProviderStageCall(
            prompt=prompt,
            system_prompt='Return exactly a JSON object with the memories key following distill_reduce.v1.',
            response_kind=self.response_kind,
        )

    def normalize_output(self, raw_body: str, *, stage: object) -> dict[str, object]:
        from engram.memory.distillation_provider_stage import ProviderStageOutputError

        try:
            payload = json.loads(raw_body)
            inputs = _hydrate_input_drafts(stage)
            window = _value(stage, 'window', None)
            reduction_target = _value(window, 'reduction_target', _value(stage, 'reduction_target', None))
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


def provider_stage_target(window: object, batch: ReductionBatch) -> object:
    from engram.memory.distillation_provider_stage import ProviderStageTarget

    if not isinstance(batch, ReductionBatch) or batch.level < 1:
        raise ReductionContractError('reduction batch must have level >= 1')
    window_id = _value(window, 'id', _value(window, 'window_id'))
    if window_id is None:
        raise ReductionContractError('reduction window id is required')
    return ProviderStageTarget(
        window_id=window_id,
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
