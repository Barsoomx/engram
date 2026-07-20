from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable

from engram.core.models import MemoryCandidate, MemoryCandidateSource, Observation, ObservationSource
from engram.memory.workflow_work import canonical_json_bytes, observation_content_digest


class ImportProvenanceError(ValueError):
    pass


def import_candidate_content_hash(source_id: str, observation_content_hash: str) -> str:
    serialized = json.dumps(
        ('memory-candidate', source_id, observation_content_hash),
        sort_keys=True,
        default=str,
        separators=(',', ':'),
    )
    return hashlib.sha256(serialized.encode()).hexdigest()


def agent_proposal_candidate_content_hash(title: str, body: str, kind: str, team_id: object) -> str:
    serialized = json.dumps(
        (
            'agent_proposal_candidate',
            title,
            body,
            kind,
            str(team_id) if team_id is not None else None,
        ),
        sort_keys=True,
        separators=(',', ':'),
    )

    return hashlib.sha256(serialized.encode()).hexdigest()


_AGENT_ANCHOR_REQUIRED_KEYS = ('actor_type', 'actor_id', 'api_key_id', 'request_id', 'correlation_id')


def _validated_agent_anchors(source: MemoryCandidateSource) -> dict[str, object]:
    if (
        source.source_kind != 'agent_proposal'
        or source.window_id is not None
        or source.stage_id is not None
        or source.import_source_id is not None
        or source.observation_id is not None
    ):
        raise ImportProvenanceError('candidate source is not agent-only')

    anchors = source.anchors
    if not isinstance(anchors, dict) or anchors.get('schema') != 'agent_proposal_source.v1':
        raise ImportProvenanceError('agent anchors schema is invalid')

    for key in _AGENT_ANCHOR_REQUIRED_KEYS:
        if key not in anchors:
            raise ImportProvenanceError('agent anchors missing required key')

    if source.anchors_hash != hashlib.sha256(canonical_json_bytes(anchors)).hexdigest():
        raise ImportProvenanceError('agent anchors are not immutable')

    return anchors


def _source_metadata(import_source: ObservationSource, event_type: str, source_store_id: str) -> None:
    if import_source.source_type != 'claude_mem' or not import_source.source_id:
        raise ImportProvenanceError('invalid import source identity')
    if not isinstance(source_store_id, str) or not isinstance(event_type, str) or not event_type:
        raise ImportProvenanceError('invalid import source metadata')


def import_candidate_source_anchors(
    *,
    observation: Observation,
    import_source: ObservationSource,
    source_store_id: str,
    event_type: str,
) -> dict[str, object]:
    if import_source.observation_id != observation.id:
        raise ImportProvenanceError('import source observation mismatch')
    if (
        import_source.organization_id != observation.organization_id
        or import_source.project_id != observation.project_id
    ):
        raise ImportProvenanceError('import source scope mismatch')
    _source_metadata(import_source, event_type, source_store_id)
    raw_event_id = str(import_source.raw_event_id) if import_source.raw_event_id else None
    return {
        'schema': 'import_candidate_source.v1',
        'observation_id': str(observation.id),
        'session_sequence': observation.session_sequence,
        'observation_digest': observation_content_digest(observation),
        'source_type': import_source.source_type,
        'source_id': import_source.source_id,
        'source_store_id': source_store_id,
        'event_type': event_type,
        'raw_event_id': raw_event_id,
    }


def _validated_import_relations(
    candidate: MemoryCandidate,
    source: MemoryCandidateSource,
) -> tuple[ObservationSource, Observation]:
    if source.source_kind != 'import' or source.window_id is not None or source.stage_id is not None:
        raise ImportProvenanceError('candidate source is not import-only')
    if source.candidate_id != candidate.id:
        raise ImportProvenanceError('candidate source belongs to another candidate')
    if (
        source.organization_id,
        source.project_id,
        source.team_id,
    ) != (candidate.organization_id, candidate.project_id, candidate.team_id):
        raise ImportProvenanceError('candidate source scope mismatch')
    if source.import_source_id is None:
        raise ImportProvenanceError('import source is required')
    import_source = source.import_source
    observation = source.observation
    if import_source is None or observation is None:
        raise ImportProvenanceError('import source relations are required')
    if (
        observation.organization_id,
        observation.project_id,
        observation.team_id,
    ) != (candidate.organization_id, candidate.project_id, candidate.team_id):
        raise ImportProvenanceError('observation scope mismatch')
    if candidate.source_observation_id != observation.id or import_source.observation_id != observation.id:
        raise ImportProvenanceError('import source observation mismatch')
    return import_source, observation


def _validated_import_source(
    candidate: MemoryCandidate,
    source: MemoryCandidateSource,
) -> tuple[MemoryCandidateSource, dict[str, object]]:
    import_source, observation = _validated_import_relations(candidate, source)
    anchors = source.anchors
    if not isinstance(anchors, dict):
        raise ImportProvenanceError('import anchors must be an object')
    source_metadata = import_source.metadata if isinstance(import_source.metadata, dict) else {}
    source_store_id = source_metadata.get('source_store_id', '')
    event_type = source_metadata.get('event_type', '')
    expected = import_candidate_source_anchors(
        observation=observation,
        import_source=import_source,
        source_store_id=source_store_id,
        event_type=event_type,
    )
    if anchors != expected or source.anchors_hash != hashlib.sha256(canonical_json_bytes(expected)).hexdigest():
        raise ImportProvenanceError('import anchors are not immutable')
    if candidate.title != observation.title or candidate.body != (observation.body or observation.title):
        raise ImportProvenanceError('import candidate content mismatch')
    expected_content_hash = import_candidate_content_hash(import_source.source_id, observation.content_hash)
    if candidate.content_hash != expected_content_hash:
        raise ImportProvenanceError('import candidate content hash mismatch')
    return source, expected


def validated_import_candidate_source(
    candidate: MemoryCandidate,
    *,
    sources: Iterable[MemoryCandidateSource] | None = None,
) -> tuple[MemoryCandidateSource, dict[str, object]]:
    selected = (
        list(sources)
        if sources is not None
        else list(
            MemoryCandidateSource.objects.select_related('observation', 'import_source').filter(
                candidate_id=candidate.id
            )
        )
    )
    if len(selected) != 1:
        raise ImportProvenanceError('import candidate must have exactly one source')
    return _validated_import_source(candidate, selected[0])


def is_validated_import_candidate(
    candidate: MemoryCandidate,
    *,
    sources: Iterable[MemoryCandidateSource] | None = None,
) -> bool:
    try:
        validated_import_candidate_source(candidate, sources=sources)
    except (ImportProvenanceError, AttributeError, TypeError, ValueError):
        return False
    return True


def validated_agent_candidate_source(
    candidate: MemoryCandidate,
    *,
    sources: Iterable[MemoryCandidateSource] | None = None,
) -> tuple[MemoryCandidateSource, dict[str, object]]:
    selected = (
        list(sources)
        if sources is not None
        else list(MemoryCandidateSource.objects.filter(candidate_id=candidate.id))
    )
    if len(selected) != 1:
        raise ImportProvenanceError('agent candidate must have exactly one source')

    source = selected[0]
    anchors = _validated_agent_anchors(source)
    if source.candidate_id != candidate.id:
        raise ImportProvenanceError('candidate source belongs to another candidate')

    if (
        source.organization_id,
        source.project_id,
        source.team_id,
    ) != (candidate.organization_id, candidate.project_id, candidate.team_id):
        raise ImportProvenanceError('agent candidate source scope mismatch')

    if candidate.source_observation_id is not None:
        raise ImportProvenanceError('agent candidate must not have a source observation')

    return source, anchors


def agent_proposal_evidence_manifest(
    candidate: MemoryCandidate,
    *,
    sources: Iterable[MemoryCandidateSource] | None = None,
) -> tuple[list[dict[str, object]], str]:
    source, _anchors = validated_agent_candidate_source(candidate, sources=sources)
    entry = {'anchors': source.anchors, 'anchors_hash': source.anchors_hash}
    entries = [entry]

    return entries, hashlib.sha256(canonical_json_bytes(entries)).hexdigest()


def import_memory_metadata(anchors: dict[str, object]) -> dict[str, object]:
    if not isinstance(anchors, dict) or anchors.get('schema') != 'import_candidate_source.v1':
        raise ImportProvenanceError('invalid import anchors')
    return {
        'source': 'claude_mem_import',
        'source_store_id': anchors['source_store_id'],
        'source_id': anchors['source_id'],
        'event_type': anchors['event_type'],
    }


def candidate_evidence_manifest(
    candidate: MemoryCandidate,
    *,
    sources: Iterable[MemoryCandidateSource] | None = None,
) -> tuple[list[dict[str, object]], str]:
    selected = (
        list(sources)
        if sources is not None
        else list(
            MemoryCandidateSource.objects.select_related('window', 'observation', 'stage', 'import_source').filter(
                candidate_id=candidate.id
            )
        )
    )
    if not selected:
        raise ImportProvenanceError('candidate provenance is empty')
    kinds = {source.source_kind for source in selected}
    if kinds == {'agent_proposal'}:
        return agent_proposal_evidence_manifest(candidate, sources=selected)
    if kinds == {'distillation'}:
        from engram.memory.candidate_decision_work import evidence_manifest

        return evidence_manifest(candidate, sources=selected)
    if kinds != {'import'} or len(selected) != 1:
        raise ImportProvenanceError('candidate provenance has mixed or multiple source kinds')
    _source, anchors = validated_import_candidate_source(candidate, sources=selected)
    entry = {'anchors': anchors, 'anchors_hash': selected[0].anchors_hash}
    entries = [entry]
    return entries, hashlib.sha256(canonical_json_bytes(entries)).hexdigest()


def import_source_metadata(
    candidate: MemoryCandidate,
    sources: Iterable[MemoryCandidateSource] | None = None,
) -> dict[str, object]:
    _source, anchors = validated_import_candidate_source(candidate, sources=sources)
    return import_memory_metadata(anchors)
