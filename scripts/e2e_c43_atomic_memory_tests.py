from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from scripts import e2e_c43_atomic_memory as atomic

IDS = {
    'candidate_id': '10000000-0000-4000-8000-000000000001',
    'memory_id': '10000000-0000-4000-8000-000000000002',
    'version_id': '10000000-0000-4000-8000-000000000003',
    'transition_id': '10000000-0000-4000-8000-000000000004',
    'document_id': '10000000-0000-4000-8000-000000000005',
    'work_id': '10000000-0000-4000-8000-000000000006',
}
EXACT_HASH = 'a' * 64


def _payload() -> dict[str, object]:
    return {
        **IDS,
        'current_transition_id': IDS['transition_id'],
        'transition_exact_document_id': IDS['document_id'],
        'exact_projection_hash': EXACT_HASH,
        'embedding_projection_hash': '',
        'embedding_reference': '',
        'embedding_vector_count': 0,
        'embedding_pgvector_is_null': True,
        'transition_count': 1,
        'audit_count': 1,
        'document_count': 1,
        'work_count': 1,
        'work_execution_state': 'ready',
        'active_run_count': 0,
    }


def _snapshot(payload: dict[str, object] | None = None) -> atomic.MemorySnapshot:
    return atomic.parse_memory_snapshot(json.dumps(payload or _payload()))


def test_pre_kill_snapshot_proves_atomic_semantic_commit_with_blank_embedding() -> None:
    snapshot = _snapshot()

    assert atomic.validate_pre_kill(snapshot) is None
    assert snapshot.exact_projection_hash == EXACT_HASH
    assert snapshot.embedding_projection_hash == ''
    assert snapshot.embedding_reference == ''
    assert snapshot.embedding_vector_count == 0
    assert snapshot.embedding_pgvector_is_null is True
    assert snapshot.transition_count == snapshot.audit_count == 1
    assert snapshot.document_count == snapshot.work_count == 1


def test_active_claim_preserves_semantic_identity_and_has_one_running_lease() -> None:
    baseline = _snapshot()
    active_payload = _payload()
    active_payload.update(
        {
            'work_execution_state': 'leased',
            'active_run_count': 1,
        }
    )

    active = _snapshot(active_payload)

    assert atomic.validate_active_claim(active, baseline) is None
    assert active.work_id == baseline.work_id
    assert active.current_transition_id == baseline.current_transition_id
    assert active.transition_exact_document_id == baseline.document_id
    assert active.embedding_projection_hash == ''
    assert active.embedding_vector_count == 0
    assert active.embedding_pgvector_is_null is True

    cross_id_payload = copy.deepcopy(active_payload)
    cross_id_payload['memory_id'] = IDS['candidate_id']
    with pytest.raises(ValueError, match='memory'):
        atomic.validate_active_claim(_snapshot(cross_id_payload), baseline)


def test_recovered_snapshot_proves_same_chain_and_one_current_vector() -> None:
    baseline = _snapshot()
    recovered_payload = _payload()
    recovered_payload.update(
        {
            'embedding_projection_hash': EXACT_HASH,
            'embedding_reference': 'provider:embedding-0001',
            'embedding_vector_count': 1,
            'embedding_pgvector_is_null': False,
            'work_execution_state': 'settled',
        }
    )

    recovered = _snapshot(recovered_payload)

    assert atomic.validate_recovered(recovered, baseline) is None
    assert recovered.candidate_id == baseline.candidate_id
    assert recovered.memory_id == baseline.memory_id
    assert recovered.version_id == baseline.version_id
    assert recovered.transition_id == baseline.transition_id
    assert recovered.current_transition_id == baseline.current_transition_id
    assert recovered.document_id == baseline.document_id
    assert recovered.transition_exact_document_id == baseline.document_id
    assert recovered.work_id == baseline.work_id
    assert recovered.embedding_projection_hash == recovered.exact_projection_hash
    assert recovered.embedding_vector_count > 0
    assert recovered.embedding_pgvector_is_null is False
    assert recovered.active_run_count == 0
    assert recovered.transition_count == recovered.document_count == recovered.work_count == 1


@pytest.mark.parametrize(
    'raw',
    (
        'not-json',
        '[]',
        json.dumps({**_payload(), 'memory_id': True}),
        json.dumps({key: value for key, value in _payload().items() if key != 'work_id'}),
    ),
)
def test_memory_snapshot_parser_rejects_malformed_snapshots(raw: str) -> None:
    with pytest.raises(ValueError, match='snapshot'):
        atomic.parse_memory_snapshot(raw)


@pytest.mark.parametrize(
    ('field', 'value', 'message'),
    (
        ('transition_exact_document_id', IDS['memory_id'], 'document'),
        ('current_transition_id', IDS['document_id'], 'transition'),
        ('work_count', 2, 'work'),
        ('transition_count', 2, 'transition'),
    ),
)
def test_validators_reject_cross_id_or_duplicate_rows(field: str, value: object, message: str) -> None:
    baseline = _snapshot()
    mutated = copy.deepcopy(_payload())
    mutated[field] = value
    snapshot = _snapshot(mutated)

    with pytest.raises(ValueError, match=message):
        atomic.validate_pre_kill(snapshot)

    with pytest.raises(ValueError, match=message):
        atomic.validate_recovered(snapshot, baseline)


def test_deterministic_env_preserves_windows_docker_plugin_discovery() -> None:
    source = {
        'PATH': 'docker-bin',
        'APPDATA': 'app-data',
        'LOCALAPPDATA': 'local-app-data',
        'ProgramFiles': 'program-files',
        'XDG_RUNTIME_DIR': 'runtime-dir',
        'UNRELATED_SECRET': 'must-not-leak',
    }

    result = atomic.deterministic_env(Path.cwd() / 'c43.env', source=source)

    assert result['PATH'] == 'docker-bin'
    assert result['APPDATA'] == 'app-data'
    assert result['LOCALAPPDATA'] == 'local-app-data'
    assert result['ProgramFiles'] == 'program-files'
    assert result['XDG_RUNTIME_DIR'] == 'runtime-dir'
    assert 'UNRELATED_SECRET' not in result
