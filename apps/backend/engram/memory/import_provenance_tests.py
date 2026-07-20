import hashlib
import json
from types import SimpleNamespace

import pytest

from engram.memory.import_provenance import (
    ImportProvenanceError,
    _validated_agent_anchors,
    agent_proposal_candidate_content_hash,
)
from engram.memory.workflow_work import canonical_json_bytes

VALID_ANCHORS = {
    'schema': 'agent_proposal_source.v1',
    'actor_type': 'api_key',
    'actor_id': 'actor-1',
    'api_key_id': 'key-1',
    'request_id': 'req-1',
    'correlation_id': '',
}


def _expected_hash(title: str, body: str, kind: str, team: str | None) -> str:
    serialized = json.dumps(
        ('agent_proposal_candidate', title, body, kind, team),
        sort_keys=True,
        separators=(',', ':'),
    )

    return hashlib.sha256(serialized.encode()).hexdigest()


def _agent_source(anchors: dict[str, object] | object, anchors_hash: str | None = None, **overrides: object) -> SimpleNamespace:
    if anchors_hash is None and isinstance(anchors, dict):
        anchors_hash = hashlib.sha256(canonical_json_bytes(anchors)).hexdigest()
    base: dict[str, object] = {
        'source_kind': 'agent_proposal',
        'window_id': None,
        'stage_id': None,
        'import_source_id': None,
        'observation_id': None,
        'anchors': anchors,
        'anchors_hash': anchors_hash or 'a' * 64,
    }
    base.update(overrides)

    return SimpleNamespace(**base)


def test_agent_proposal_content_hash_matches_spec_digest() -> None:
    assert agent_proposal_candidate_content_hash('Title', 'Body', '', None) == _expected_hash('Title', 'Body', '', None)
    assert agent_proposal_candidate_content_hash('Title', 'Body', 'decision', 't1') == _expected_hash(
        'Title', 'Body', 'decision', 't1'
    )


def test_agent_proposal_content_hash_differs_on_kind_or_team() -> None:
    base = agent_proposal_candidate_content_hash('Title', 'Body', 'decision', None)
    assert agent_proposal_candidate_content_hash('Title', 'Body', 'gotcha', None) != base
    assert agent_proposal_candidate_content_hash('Title', 'Body', 'decision', 'team-1') != base


def test_agent_proposal_content_hash_uses_kind_verbatim() -> None:
    assert agent_proposal_candidate_content_hash('Title', 'Body', '', None) != agent_proposal_candidate_content_hash(
        'Title', 'Body', 'digest', None
    )


def test_agent_proposal_content_hash_team_none_is_json_null() -> None:
    assert agent_proposal_candidate_content_hash('Title', 'Body', '', None) == _expected_hash('Title', 'Body', '', None)
    assert agent_proposal_candidate_content_hash('Title', 'Body', '', None) != agent_proposal_candidate_content_hash(
        'Title', 'Body', '', 'None'
    )


def test_validated_agent_anchors_returns_anchors_on_success() -> None:
    source = _agent_source(VALID_ANCHORS)
    assert _validated_agent_anchors(source) == VALID_ANCHORS


def test_validated_agent_anchors_rejects_lineage_shape() -> None:
    for field in ('window_id', 'stage_id', 'import_source_id', 'observation_id'):
        source = _agent_source(VALID_ANCHORS, **{field: 'x'})
        with pytest.raises(ImportProvenanceError):
            _validated_agent_anchors(source)


def test_validated_agent_anchors_rejects_non_dict_and_bad_schema() -> None:
    with pytest.raises(ImportProvenanceError):
        _validated_agent_anchors(_agent_source('not-a-dict', anchors_hash='a' * 64))
    bad_schema = dict(VALID_ANCHORS, schema='other.v1')
    with pytest.raises(ImportProvenanceError):
        _validated_agent_anchors(_agent_source(bad_schema))


def test_validated_agent_anchors_rejects_missing_required_key() -> None:
    for key in ('actor_type', 'actor_id', 'api_key_id', 'request_id', 'correlation_id'):
        anchors = dict(VALID_ANCHORS)
        anchors.pop(key)
        with pytest.raises(ImportProvenanceError):
            _validated_agent_anchors(_agent_source(anchors))


def test_validated_agent_anchors_rejects_hash_mismatch() -> None:
    source = _agent_source(VALID_ANCHORS, anchors_hash='b' * 64)
    with pytest.raises(ImportProvenanceError):
        _validated_agent_anchors(source)
