from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from uuid import UUID

from engram.memory.distillation_reduction import stable_draft_id
from engram.memory.workflow_work import canonical_json_bytes


class ProvenanceContractError(ValueError):
    pass


_DIGEST_FIELDS = ('observation_digest', 'content_digest', 'digest')
_ANCHOR_FIELDS = (
    'files',
    'file_paths',
    'symbols',
    'commands',
    'errors',
    'error_identifiers',
    'commits',
    'commit_identifiers',
)


def _value(row: object, name: str, default: object = None) -> object:
    if isinstance(row, Mapping):
        return row.get(name, default)
    return getattr(row, name, default)


def _required(row: object, *names: str) -> object:
    for name in names:
        value = _value(row, name)
        if value is not None:
            return value
    raise ProvenanceContractError(f'missing {names[0]}')


def _sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _check_digest(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in '0123456789abcdef' for character in value)
    ):
        raise ProvenanceContractError(f'{label} must be lowercase SHA-256')
    return value


def _canonical_id(value: object, label: str) -> str:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, str) and value.strip():
        return value
    raise ProvenanceContractError(f'{label} must be a non-empty string or UUID')


def _strings(value: object, *, path_mode: bool = False) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, (list, tuple, set, frozenset)):
        result: set[str] = set()
        for item in value:
            result.update(_strings(item, path_mode=path_mode))
        return result
    if isinstance(value, Mapping):
        keys = (
            ('path', 'file_path', 'filename', 'name')
            if path_mode
            else (
                'value',
                'id',
                'identifier',
                'name',
                'symbol',
                'command',
                'error',
                'commit',
            )
        )
        result: set[str] = set()
        for key in keys:
            if key in value:
                result.update(_strings(value[key], path_mode=path_mode))
        return result
    return set()


def _observation_anchor_parts(observation: object) -> dict[str, set[str]]:
    metadata = _value(observation, 'source_metadata', {})
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, Mapping):
        raise ProvenanceContractError('source_metadata must be an object')

    def metadata_values(*names: str) -> set[str]:
        values: set[str] = set()
        for name in names:
            values.update(_strings(metadata.get(name, [])))
        return values

    return {
        'file_paths': _strings(_value(observation, 'files_read', []), path_mode=True)
        | _strings(_value(observation, 'files_modified', []), path_mode=True),
        'symbols': metadata_values('symbols'),
        'commands': metadata_values('commands'),
        'errors': metadata_values('errors', 'error_identifiers'),
        'commits': metadata_values('commits', 'commit_identifiers'),
    }


def candidate_source_anchors(
    observation: object,
    *,
    observation_id: str | None = None,
    session_sequence: int | None = None,
    observation_digest: str | None = None,
) -> dict[str, object]:
    oid = observation_id if observation_id is not None else _required(observation, 'observation_id', 'id')
    sequence = session_sequence if session_sequence is not None else _required(observation, 'session_sequence')
    digest = observation_digest
    if digest is None:
        digest = _required(observation, *_DIGEST_FIELDS)
    oid = _canonical_id(oid, 'observation_id')
    if type(sequence) is not int or sequence <= 0:
        raise ProvenanceContractError('session_sequence must be positive')
    digest = _check_digest(digest, 'observation_digest')
    parts = _observation_anchor_parts(observation)
    return {
        'schema': 'candidate_source_anchors.v1',
        'observation_id': oid,
        'session_sequence': sequence,
        'observation_digest': digest,
        **{key: sorted(values) for key, values in parts.items()},
    }


def canonical_source_manifest(anchors: Mapping[str, object]) -> str:
    if not isinstance(anchors, Mapping) or anchors.get('schema') != 'candidate_source_anchors.v1':
        raise ProvenanceContractError('invalid candidate source anchor schema')
    return _sha256(dict(anchors))


anchors_hash = canonical_source_manifest


@dataclass(frozen=True, slots=True)
class CandidateSourcePlan:
    observation_id: str
    session_sequence: int
    observation_digest: str
    lineage_stage_key: str
    anchors: Mapping[str, object]
    anchors_hash: str
    lineage_stage_id: str | None = None

    @property
    def sequence(self) -> int:
        return self.session_sequence

    @property
    def digest(self) -> str:
        return self.observation_digest

    @property
    def stage_key(self) -> str:
        return self.lineage_stage_key

    @property
    def deciding_stage_key(self) -> str:
        return self.lineage_stage_key


@dataclass(frozen=True, slots=True)
class CandidatePlan:
    final_draft_id: str
    title: str
    body: str
    confidence: Decimal
    kind: str
    deciding_stage_key: str
    sources: tuple[CandidateSourcePlan, ...]
    content_hash: str | None = None
    deciding_stage_id: str | None = None

    @property
    def source_plans(self) -> tuple[CandidateSourcePlan, ...]:
        return self.sources


@dataclass(frozen=True, slots=True)
class CoveragePlan:
    observation_id: str
    session_sequence: int
    observation_digest: str
    outcome: str
    deciding_stage_key: str
    deciding_stage_id: str | None = None

    @property
    def sequence(self) -> int:
        return self.session_sequence

    @property
    def digest(self) -> str:
        return self.observation_digest

    @property
    def stage_key(self) -> str:
        return self.deciding_stage_key


@dataclass(frozen=True, slots=True)
class FinalizationPlan:
    scope: Mapping[str, object]
    candidates: tuple[CandidatePlan, ...]
    coverage: tuple[CoveragePlan, ...]
    has_signal: bool
    intent: str
    window_input_hash: str

    @property
    def candidate_plans(self) -> tuple[CandidatePlan, ...]:
        return self.candidates

    @property
    def coverage_plans(self) -> tuple[CoveragePlan, ...]:
        return self.coverage

    @property
    def signal_intent(self) -> str:
        return self.intent


def _stage_snapshot(stage: object) -> Mapping[str, object]:
    snapshot = _value(stage, 'output_snapshot')
    if snapshot is None:
        snapshot = _value(stage, 'outputs')
    if isinstance(snapshot, Mapping):
        return snapshot
    if isinstance(snapshot, (list, tuple)):
        return {'memories': list(snapshot)}
    raise ProvenanceContractError('accepted stage output snapshot is invalid')


def _stage_key(stage: object) -> str:
    key = _required(stage, 'stage_key', 'id')
    if not isinstance(key, str) or not key:
        raise ProvenanceContractError('stage key must be non-empty')
    return key


def _stage_outputs(stage: object) -> tuple[Mapping[str, object], ...]:
    snapshot = _stage_snapshot(stage)
    values = snapshot.get('memories', snapshot.get('outputs', snapshot.get('drafts', [])))
    if not isinstance(values, (list, tuple)):
        raise ProvenanceContractError('stage memories must be a list')
    if any(not isinstance(value, Mapping) for value in values):
        raise ProvenanceContractError('stage memory must be an object')
    return tuple(values)


def _output_id(
    output: object,
    stage_key: str,
    index: int,
    *,
    target_key: str | None = None,
    output_hash: str | None = None,
) -> str:
    value = _value(output, 'draft_id') or _value(output, 'id')
    if value is None:
        if not target_key or not output_hash:
            raise ProvenanceContractError('stable draft identity requires stage target_key and output_hash')
        value = stable_draft_id(target_key, output_hash, index)
    if not isinstance(value, str) or not value:
        raise ProvenanceContractError('draft id must be non-empty')
    return value


def _source_ids(output: object) -> tuple[str, ...]:
    values = _value(output, 'source_ids')
    if values is None:
        values = _value(output, 'supporting_draft_ids')
    if values is None:
        return ()
    if not isinstance(values, (list, tuple)):
        raise ProvenanceContractError('source_ids must be strings')
    values = tuple(_canonical_id(item, 'source_id') for item in values)
    if len(set(values)) != len(values):
        raise ProvenanceContractError('source_ids must be duplicate-free')
    return tuple(values)


def _support_ids(output: object) -> tuple[str, ...]:
    values = _value(output, 'supporting_observation_ids')
    if values is None:
        values = _value(output, 'source_observation_ids', [])
    if not isinstance(values, (list, tuple)):
        raise ProvenanceContractError('supporting observation ids must be strings')
    values = tuple(_canonical_id(item, 'observation_id') for item in values)
    if len(set(values)) != len(values):
        raise ProvenanceContractError('supporting observation ids must be duplicate-free')
    return tuple(values)


def _scope_match(row: object, scope: Mapping[str, object]) -> None:
    for key, expected in scope.items():
        if expected is not None and _value(row, key) not in (None, expected):
            raise ProvenanceContractError(f'scope mismatch for {key}')


def build_finalization_plan(  # noqa: C901
    *,
    window: object | None = None,
    final_drafts: Iterable[object] | None = None,
    observations: Iterable[object],
    accepted_stages: Iterable[object] | None = None,
    scope: Mapping[str, object] | None = None,
    window_input_hash: str | None = None,
    extraction_stages: Iterable[object] | None = None,
    reduction_stages: Iterable[object] | None = None,
    final_stage_key: str | None = None,
) -> FinalizationPlan:
    if window is not None:
        scope = scope or _value(window, 'scope')
        if scope is None:
            scope = {key: _value(window, key) for key in ('organization_id', 'project_id', 'team_id', 'session_id')}
        window_input_hash = window_input_hash or _value(window, 'input_hash')
    if not isinstance(scope, Mapping):
        raise ProvenanceContractError('scope is required')
    if window_input_hash is None:
        window_input_hash = _required(window or {}, 'input_hash')
    window_input_hash = _check_digest(window_input_hash, 'window_input_hash')
    scope_view = MappingProxyType(dict(scope))

    observation_rows = tuple(observations)
    obs_by_id: dict[str, object] = {}
    for observation in observation_rows:
        _scope_match(observation, scope)
        oid = _canonical_id(_required(observation, 'observation_id', 'id'), 'observation_id')
        if oid in obs_by_id:
            raise ProvenanceContractError('observations must have unique ids')
        candidate_source_anchors(observation)
        obs_by_id[oid] = observation
    if not obs_by_id:
        raise ProvenanceContractError('observations must be non-empty')

    stages = tuple(accepted_stages or ())
    if extraction_stages is not None:
        stages += tuple(extraction_stages)
    if reduction_stages is not None:
        stages += tuple(reduction_stages)
    stage_map: dict[str, object] = {}
    draft_map: dict[str, tuple[object, str, int]] = {}
    no_signal: set[str] = set()
    no_signal_stage: dict[str, str] = {}
    for stage in stages:
        _scope_match(stage, scope)
        status = _value(stage, 'status', 'complete')
        if status not in ('complete', 'completed'):
            raise ProvenanceContractError('all accepted stages must be complete')
        key = _stage_key(stage)
        if key in stage_map:
            raise ProvenanceContractError('duplicate stage key')
        output_hash = _value(stage, 'output_hash')
        if output_hash not in (None, ''):
            _check_digest(output_hash, 'stage output_hash')
        stage_map[key] = stage
        snapshot = _stage_snapshot(stage)
        values = snapshot.get('no_signal_observation_ids', ())
        if values is not None:
            if not isinstance(values, (list, tuple)):
                raise ProvenanceContractError('no-signal ids must be a list')
            if len(set(values)) != len(values):
                raise ProvenanceContractError('no-signal ids must be duplicate-free strings')
            stage_kind = _value(stage, 'stage_kind', 'extract')
            if stage_kind not in ('extract', 'extraction') and values:
                raise ProvenanceContractError('no-signal declarations must come from extraction stages')
            for value in values:
                observation_id = _canonical_id(value, 'observation_id')
                if observation_id in no_signal_stage:
                    raise ProvenanceContractError('observation has duplicate no-signal declarations')
                no_signal_stage[observation_id] = key
            no_signal.update(no_signal_stage)
        for index, output in enumerate(_stage_outputs(stage)):
            did = _output_id(
                output,
                key,
                index,
                target_key=_value(stage, 'target_key'),
                output_hash=_value(stage, 'output_hash'),
            )
            if did in draft_map:
                raise ProvenanceContractError('duplicate draft id')
            draft_map[did] = (output, key, index)

    supplied_final = tuple(final_drafts or ())
    if supplied_final:
        finals: list[tuple[object, str, int]] = []
        for index, output in enumerate(supplied_final):
            did = _value(output, 'draft_id') or _value(output, 'id') or f'final:{index}'
            source_stage = _value(output, 'source_stage_key') or _value(output, 'deciding_stage_key')
            if not isinstance(source_stage, str):
                source_stage = _value(output, 'stage_key')
            if not isinstance(source_stage, str):
                raise ProvenanceContractError('final draft lineage stage is required')
            finals.append((output, source_stage, index))
    else:
        finals = []
        if final_stage_key is not None and final_stage_key not in stage_map:
            raise ProvenanceContractError('final stage key is unknown')
        ordered_stage_keys = [key for key in stage_map if key == final_stage_key] or list(stage_map)
        if ordered_stage_keys:
            chosen = ordered_stage_keys[-1] if final_stage_key is None else ordered_stage_keys[0]
            finals = [(_output, chosen, index) for index, _output in enumerate(_stage_outputs(stage_map[chosen]))]

    memo: dict[str, dict[str, str]] = {}

    def resolve(output_id: str, stack: set[str]) -> dict[str, str]:
        if output_id in memo:
            return memo[output_id]
        if output_id in stack:
            raise ProvenanceContractError('draft lineage cycle')
        if output_id not in draft_map:
            raise ProvenanceContractError('unknown draft lineage')
        output, stage_key, _ = draft_map[output_id]
        lineage = dict.fromkeys(_support_ids(output), stage_key)
        source_ids = _source_ids(output)
        if source_ids:
            for source_id in source_ids:
                if source_id in obs_by_id:
                    lineage.setdefault(source_id, stage_key)
                    continue
                child_lineage = resolve(source_id, stack | {output_id})
                for observation_id, child_stage_key in child_lineage.items():
                    lineage.setdefault(observation_id, child_stage_key)
        memo[output_id] = lineage
        return lineage

    candidate_plans: list[CandidatePlan] = []
    signal_observation_ids: set[str] = set()
    candidate_ids: set[str] = set()
    for _index, (output, deciding_stage, output_index) in enumerate(finals):
        if deciding_stage not in stage_map:
            raise ProvenanceContractError('final draft deciding stage is unknown')
        deciding_stage_row = stage_map[deciding_stage]
        did = _output_id(
            output,
            deciding_stage,
            output_index,
            target_key=_value(deciding_stage_row, 'target_key'),
            output_hash=_value(deciding_stage_row, 'output_hash'),
        )
        if did in candidate_ids:
            raise ProvenanceContractError('duplicate final draft id')
        candidate_ids.add(did)
        if did in draft_map:
            lineage = resolve(did, set())
        else:
            direct_ids = _support_ids(output)
            if not direct_ids:
                direct_ids = tuple(source_id for source_id in _source_ids(output) if source_id in obs_by_id)
            lineage = dict.fromkeys(direct_ids, deciding_stage)
        support = set(lineage)
        if not support:
            raise ProvenanceContractError('candidate must support an observation')
        if not support <= set(obs_by_id):
            raise ProvenanceContractError('candidate references unknown observation')
        explicit_anchor_values = {
            value
            for field in _ANCHOR_FIELDS
            for value in _strings(_value(output, field, []), path_mode=field in ('files', 'file_paths'))
        }
        persisted_anchor_values = {
            value
            for oid in support
            for values in _observation_anchor_parts(obs_by_id[oid]).values()
            for value in values
        }
        if explicit_anchor_values - persisted_anchor_values:
            raise ProvenanceContractError('provider invented an anchor')
        title = _value(output, 'title')
        body = _value(output, 'body')
        if (
            not isinstance(title, str)
            or not title.strip()
            or len(title) > 255
            or not isinstance(body, str)
            or not body
            or len(body) > 3000
        ):
            raise ProvenanceContractError('candidate title/body are invalid')
        confidence_raw = _value(output, 'confidence', 0)
        try:
            confidence = Decimal(str(confidence_raw))
        except Exception as error:
            raise ProvenanceContractError('candidate confidence is invalid') from error
        if not confidence.is_finite() or confidence < 0 or confidence > 1:
            raise ProvenanceContractError('candidate confidence is invalid')
        kind = _value(output, 'kind', '')
        known_kinds = {'decision', 'convention', 'gotcha', 'architecture', 'incident'}
        if not isinstance(kind, str) or (kind and kind not in known_kinds):
            raise ProvenanceContractError('candidate kind is invalid')
        sources: list[CandidateSourcePlan] = []
        for oid in sorted(support, key=lambda value: (_value(obs_by_id[value], 'session_sequence'), value)):
            observation = obs_by_id[oid]
            anchors = candidate_source_anchors(observation)
            lineage_stage = deciding_stage
            source = CandidateSourcePlan(
                observation_id=oid,
                session_sequence=_required(observation, 'session_sequence'),
                observation_digest=_required(observation, *_DIGEST_FIELDS),
                lineage_stage_key=lineage_stage,
                anchors=MappingProxyType(anchors),
                anchors_hash=canonical_source_manifest(anchors),
            )
            sources.append(source)
            signal_observation_ids.add(oid)
        session_id = scope_view.get('session_id')
        content_hash = None
        if session_id is not None:
            content_hash = hashlib.sha256(f'{session_id}:{title}:{body}'.encode()).hexdigest()
        candidate_plans.append(
            CandidatePlan(
                final_draft_id=did,
                title=title,
                body=body,
                confidence=confidence,
                kind=kind,
                deciding_stage_key=deciding_stage,
                sources=tuple(sources),
                content_hash=content_hash,
            )
        )

    unknown_no_signal = no_signal - set(obs_by_id)
    if unknown_no_signal:
        raise ProvenanceContractError('no-signal references unknown observation')
    if signal_observation_ids & no_signal:
        raise ProvenanceContractError('observation is both signal and no-signal')
    if signal_observation_ids | no_signal != set(obs_by_id):
        raise ProvenanceContractError('coverage does not equal observation manifest')
    first_deciding_stage = candidate_plans[0].deciding_stage_key if candidate_plans else (next(iter(stage_map), ''))
    coverage = tuple(
        CoveragePlan(
            observation_id=oid,
            session_sequence=_required(obs_by_id[oid], 'session_sequence'),
            observation_digest=_required(obs_by_id[oid], *_DIGEST_FIELDS),
            outcome='signal' if oid in signal_observation_ids else 'no_signal',
            deciding_stage_key=(first_deciding_stage if oid in signal_observation_ids else no_signal_stage[oid]),
        )
        for oid in sorted(obs_by_id, key=lambda value: (_value(obs_by_id[value], 'session_sequence'), value))
    )
    return FinalizationPlan(
        scope=scope_view,
        candidates=tuple(candidate_plans),
        coverage=coverage,
        has_signal=bool(signal_observation_ids),
        intent='signal' if signal_observation_ids else 'no_signal',
        window_input_hash=window_input_hash,
    )


derive_candidate_source_anchors = candidate_source_anchors
plan_finalization = build_finalization_plan
