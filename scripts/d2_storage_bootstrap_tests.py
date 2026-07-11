from __future__ import annotations

import hashlib
import io
import json
import math
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

from scripts.d2_storage_bootstrap import (
    Action,
    AuthorityState,
    BackendSubject,
    BeatSource,
    BindDirectory,
    D2Error,
    FileLock,
    Journal,
    JournalIdentity,
    NodeSnapshot,
    Phase,
    QueueSnapshot,
    QuiescenceSample,
    RuntimeIdentity,
    TargetIdentity,
    atomic_write_json,
    capture_quiescence_sample,
    copy_beat_source,
    decide_authority_recovery,
    parse_beat_source,
    parse_rabbit_source,
    parse_runtime_identity,
    reconcile_quiescence,
    redact_and_bound,
    validate_effective_config,
    validate_runtime_agreement,
    validate_target_labels,
    volume_names,
)


def _identity() -> JournalIdentity:
    subjects = {
        name: BackendSubject(name, 'sha256:' + 'e' * 64, 'engram@sha256:' + 'a' * 64, 'b' * 40)
        for name in (
            'api',
            'worker-realtime',
            'worker-near-realtime',
            'worker-batch',
            'worker-highmemory',
            'worker-domain-events',
            'relay',
            'beat',
        )
    }
    return JournalIdentity(
        attempt_id='a1b2c3d4',
        rabbit_volume='engram_d2_rabbitmq_a1b2c3d4',
        beat_volume='engram_d2_beat_a1b2c3d4',
        backend_subjects=subjects,
        rabbit_source_id='rabbit-source',
        rabbit_source_path='/srv/rabbit',
        beat_source_id='beat-source',
        beat_source_path='/srv/app/celerybeat-schedule',
        compose_hash='sha256:' + 'c' * 64,
        rabbit_config_hash='sha256:' + 'd' * 64,
        rabbit_nodename='rabbit@engram-rabbitmq',
        rabbit_hostname='engram-rabbitmq',
        rabbit_mnesia_dir='/var/lib/rabbitmq/mnesia/rabbit@engram-rabbitmq',
    )


def _state(tmp_path: Path) -> Path:
    state = tmp_path / 'state'
    state.mkdir(mode=0o700)
    state.chmod(0o700)
    return state


def _tar_archive(entries: list[tuple[tarfile.TarInfo, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w') as archive:
        for member, payload in entries:
            archive.addfile(member, io.BytesIO(payload) if member.isreg() else None)
    return buffer.getvalue()


def _regular_member(name: str, payload: bytes) -> tarfile.TarInfo:
    member = tarfile.TarInfo(name)
    member.size = len(payload)
    member.mode = 0o600
    return member


class _Process:
    def __init__(
        self,
        stdout: io.BytesIO,
        *,
        returncode: int = 0,
        stderr: bytes = b'',
        timeout_once: bool = False,
    ) -> None:
        self.stdout = stdout
        self.stderr = io.BytesIO(stderr)
        self.returncode = returncode
        self.timeout_once = timeout_once
        self.killed = False
        self.wait_calls = 0

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls += 1
        if self.timeout_once and self.wait_calls == 1:
            raise subprocess.TimeoutExpired('docker cp', timeout)
        return self.returncode

    def kill(self) -> None:
        self.killed = True


def _evidence(action: str) -> dict[str, object]:
    values: dict[str, dict[str, object]] = {
        Action.OLD_ASYNC_STOP.value: {
            'container_id': 'old',
            'child_exit': True,
            'restart_policy': 'no',
        },
        Action.RABBIT_STOP.value: {
            'container_id': 'rabbit',
            'manifest_sha256': 'a' * 64,
            'nodename': 'rabbit@host',
        },
        Action.RABBIT_COPY.value: {
            'volume': 'rabbit-volume',
            'sha256': 'b' * 64,
            'size': 10,
        },
        Action.BEAT_COPY.value: {'path': '/state/beat', 'sha256': 'c' * 64, 'size': 10},
        Action.NAMED_RABBIT_START.value: {
            'container_id': 'target-rabbit',
            'image_id': 'sha256:' + 'e' * 64,
            'nodename': 'rabbit@host',
        },
        Action.RABBIT_ALIAS_CUTOVER.value: {
            'old_container_id': 'rabbit',
            'target_container_id': 'target-rabbit',
            'alias_owner': 'target-rabbit',
        },
        Action.TARGET_AUTHORITY_COMMIT.value: {
            'authority_state': 'committed',
            'recovery_decision': 'target-only',
        },
        Action.OLD_ASYNC_RESTORE.value: {'container_ids': ['target'], 'restored': True},
    }
    return {'returncode': 0, 'selected': values[action]}


def _complete_through(journal: Journal, target: str) -> None:
    for action in Action:
        journal.record_intent(action.value)
        journal.record_completion(action.value, _evidence(action.value))
        if action.value == target:
            return
    raise AssertionError(target)


def test_source_descriptors_require_exact_verified_shapes(tmp_path: Path) -> None:
    rabbit = parse_rabbit_source(
        {
            'Mounts': [
                {
                    'Type': 'bind',
                    'Source': str(tmp_path),
                    'Destination': '/var/lib/rabbitmq',
                }
            ]
        },
        expected_host_path=tmp_path,
    )
    assert rabbit == BindDirectory(tmp_path.resolve())
    beat = parse_beat_source(
        {'Id': 'beat-1', 'Files': {'/srv/app/celerybeat-schedule': 'regular'}},
        '/srv/app/celerybeat-schedule',
        siblings=[],
    )
    assert beat == BeatSource('beat-1', '/srv/app/celerybeat-schedule')
    for mounts in (
        [],
        [{'Type': 'volume', 'Name': 'rabbit', 'Destination': '/var/lib/rabbitmq'}],
        [
            {
                'Type': 'bind',
                'Source': str(tmp_path),
                'Destination': '/var/lib/rabbitmq',
            },
            {
                'Type': 'bind',
                'Source': str(tmp_path),
                'Destination': '/var/lib/rabbitmq',
            },
        ],
        [{'Type': 'bind', 'Source': 'relative', 'Destination': '/var/lib/rabbitmq'}],
    ):
        with pytest.raises(D2Error):
            parse_rabbit_source({'Mounts': mounts}, expected_host_path=tmp_path)


def test_source_symlink_and_sidecar_changes_are_rejected(tmp_path: Path) -> None:
    source = tmp_path / 'source'
    source.mkdir()
    link = tmp_path / 'link'
    link.symlink_to(source, target_is_directory=True)
    with pytest.raises(D2Error):
        parse_rabbit_source(
            {
                'Mounts': [
                    {
                        'Type': 'bind',
                        'Source': str(link),
                        'Destination': '/var/lib/rabbitmq',
                    }
                ]
            },
            expected_host_path=link,
        )
    inspect = {
        'Id': 'beat-1',
        'Files': {
            '/srv/app/celerybeat-schedule': 'regular',
            '/srv/app/celerybeat-schedule.db': 'regular',
        },
    }
    with pytest.raises(D2Error):
        parse_beat_source(inspect, '/srv/app/celerybeat-schedule', siblings=['/srv/app/other.db'])


def test_runtime_identity_requires_exact_agreement() -> None:
    inspect = {
        'Image': 'sha256:image',
        'RepoDigests': ['engram@sha256:' + 'a' * 64],
        'Config': {'Labels': {'org.opencontainers.image.revision': 'b' * 40}},
    }
    runtime = parse_runtime_identity(inspect)
    assert validate_runtime_agreement([runtime, runtime]) == runtime
    with pytest.raises(D2Error):
        validate_runtime_agreement(
            [
                runtime,
                RuntimeIdentity('other', runtime.repository_digest, runtime.revision),
            ]
        )


def test_journal_identity_is_exact_typed_and_non_secret() -> None:
    identity = _identity()
    payload = identity.to_mapping()
    assert set(payload['backend_subjects']) == {
        'api',
        'worker-realtime',
        'worker-near-realtime',
        'worker-batch',
        'worker-highmemory',
        'worker-domain-events',
        'relay',
        'beat',
    }
    assert JournalIdentity.from_mapping(payload) == identity
    with pytest.raises(D2Error):
        JournalIdentity.from_mapping({**payload, 'PASSWORD': 'secret'})
    subjects = dict(payload['backend_subjects'])
    subjects['worker-batch'] = {
        **subjects['worker-batch'],
        'repository_digest': 'engram@sha256:' + 'e' * 64,
    }
    with pytest.raises(D2Error):
        JournalIdentity.from_mapping({**payload, 'backend_subjects': subjects})


def test_journal_phase_action_and_authority_are_consistent(tmp_path: Path) -> None:
    journal = Journal(_state(tmp_path) / 'journal.json', _identity())
    journal.record_intent(Action.OLD_ASYNC_STOP.value)
    with pytest.raises(D2Error):
        journal.record_intent(Action.RABBIT_STOP.value)
    journal.record_completion(Action.OLD_ASYNC_STOP.value, _evidence(Action.OLD_ASYNC_STOP.value))
    journal.record_intent(Action.RABBIT_STOP.value)
    journal.record_completion(Action.RABBIT_STOP.value, _evidence(Action.RABBIT_STOP.value))
    payload = json.loads(journal.path.read_text())
    payload['phase'] = Phase.RABBIT_COPY_COMPLETE.value
    journal.path.write_text(json.dumps(payload))
    with pytest.raises(D2Error):
        Journal.load(journal.path)


def test_complete_phase_requires_all_eight_completed_actions(tmp_path: Path) -> None:
    journal = Journal(_state(tmp_path) / 'journal.json', _identity())
    with pytest.raises(D2Error):
        journal.set_phase(Phase.COMPLETE)
    _complete_through(journal, Action.TARGET_AUTHORITY_COMMIT.value)
    with pytest.raises(D2Error):
        journal.set_phase(Phase.COMPLETE)
    journal.record_intent(Action.OLD_ASYNC_RESTORE.value)
    with pytest.raises(D2Error):
        journal.set_phase(Phase.COMPLETE)
    journal.record_completion(Action.OLD_ASYNC_RESTORE.value, _evidence(Action.OLD_ASYNC_RESTORE.value))
    journal.set_phase(Phase.COMPLETE)
    assert journal.data['phase'] == Phase.COMPLETE.value
    assert Journal.load(journal.path).data['phase'] == Phase.COMPLETE.value


def test_authority_is_derived_from_journal_not_caller_enum(tmp_path: Path) -> None:
    journal = Journal(_state(tmp_path) / 'journal.json', _identity())
    _complete_through(journal, Action.TARGET_AUTHORITY_COMMIT.value)
    assert journal.authority_state is AuthorityState.COMMITTED
    assert journal.recovery_decision == 'target-only'
    assert decide_authority_recovery(journal, target_processing=True) == 'target-only'
    with pytest.raises(D2Error):
        decide_authority_recovery(AuthorityState.PRE_COMMIT, target_processing=True)


def test_state_directory_is_preexisting_canonical_root_owned_mode_0700(
    tmp_path: Path,
) -> None:
    with pytest.raises(D2Error):
        FileLock(tmp_path / 'missing' / 'controller.lock').acquire()
    state = _state(tmp_path)
    state.chmod(0o755)  # noqa: S103
    with pytest.raises(D2Error):
        FileLock(state / 'controller.lock').acquire()
    state.chmod(0o700)
    link = tmp_path / 'state-link'
    link.symlink_to(state, target_is_directory=True)
    with pytest.raises(D2Error):
        FileLock(link / 'controller.lock').acquire()


def test_lock_is_exclusive_and_rejects_leaf_symlink(tmp_path: Path) -> None:
    state = _state(tmp_path)
    first = FileLock(state / 'controller.lock')
    first.acquire()
    try:
        with pytest.raises(D2Error):
            FileLock(state / 'controller.lock').acquire()
    finally:
        first.release()
    target = state / 'target'
    target.write_text('target')
    (state / 'lock-link').symlink_to(target)
    with pytest.raises(D2Error):
        FileLock(state / 'lock-link').acquire()
    assert target.read_text() == 'target'


def test_atomic_write_uses_temp_file_replace_and_directory_fsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(tmp_path)
    calls: list[str] = []
    original_fsync = os.fsync
    original_replace = os.replace

    def fsync(fd: int) -> None:
        calls.append('fsync')
        original_fsync(fd)

    def replace(*args: object, **kwargs: object) -> None:
        calls.append('replace')
        original_replace(*args, **kwargs)

    monkeypatch.setattr(os, 'fsync', fsync)
    monkeypatch.setattr(os, 'replace', replace)
    atomic_write_json(state / 'journal.json', {'schema': 2})
    assert calls.index('replace') > 0
    assert calls[-1] == 'fsync'
    assert (state / 'journal.json').stat().st_mode & 0o777 == 0o600
    link = state / 'journal-link'
    link.symlink_to(state / 'journal.json')
    with pytest.raises(D2Error):
        atomic_write_json(link, {'schema': 2})


@pytest.mark.parametrize(
    'payload',
    [
        '{"Authorization": "Bearer top-secret-token"}',
        "{'Authorization': 'Bearer top-secret-token'}",
    ],
)
def test_bearer_redaction_is_independent_of_authorization_punctuation(payload: str) -> None:
    output = redact_and_bound(payload)
    assert 'top-secret-token' not in output
    assert 'Bearer [REDACTED]' in output


def test_copy_beat_source_extracts_only_schedule_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(tmp_path)
    source = BeatSource('container-1', '/srv/app/celerybeat-schedule')
    payload = b'schedule payload only'
    archive = _tar_archive([(_regular_member('celerybeat-schedule', payload), payload)])
    process = _Process(io.BytesIO(archive), stderr=b'{"Authorization":"Bearer top-secret"}')
    calls: list[tuple[list[str], dict[str, object]]] = []

    def popen(argv: list[str], **kwargs: object) -> _Process:
        calls.append((argv, kwargs))
        return process

    monkeypatch.setattr('scripts.d2_storage_bootstrap.subprocess.Popen', popen)
    destination = state / 'schedule'
    metadata = copy_beat_source(source, destination)
    assert destination.read_bytes() == payload
    assert metadata.path == destination
    assert metadata.size == len(payload)
    assert metadata.sha256 == hashlib.sha256(payload).hexdigest()
    assert metadata.returncode == 0
    assert 'top-secret' not in metadata.stderr
    assert calls == [
        (
            ['/usr/bin/docker', 'cp', 'container-1:/srv/app/celerybeat-schedule', '-'],
            {
                'stdout': subprocess.PIPE,
                'stderr': subprocess.PIPE,
                'cwd': '/',
                'env': {
                    'DOCKER_HOST': 'unix:///var/run/docker.sock',
                    'DOCKER_CONTEXT': 'default',
                },
            },
        )
    ]
    assert process.wait_calls == 1
    assert process.stdout.closed and process.stderr.closed


@pytest.mark.parametrize('case', ['symlink', 'extra', 'escape', 'unexpected', 'oversize'])
def test_copy_beat_source_rejects_unsafe_tar_members(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    payload = b'schedule'
    expected = _regular_member('celerybeat-schedule', payload)
    entries = [(expected, payload)]
    max_bytes = 16 * 1024 * 1024
    if case == 'symlink':
        member = tarfile.TarInfo('celerybeat-schedule')
        member.type = tarfile.SYMTYPE
        member.linkname = '/etc/passwd'
        entries = [(member, b'')]
    elif case == 'extra':
        entries.append((_regular_member('extra', b'extra'), b'extra'))
    elif case == 'escape':
        entries = [(_regular_member('../celerybeat-schedule', payload), payload)]
    elif case == 'unexpected':
        entries = [(_regular_member('other-schedule', payload), payload)]
    else:
        max_bytes = len(payload) - 1
    process = _Process(io.BytesIO(_tar_archive(entries)))
    monkeypatch.setattr('scripts.d2_storage_bootstrap.subprocess.Popen', lambda *_args, **_kwargs: process)
    destination = _state(tmp_path) / 'schedule'
    with pytest.raises(D2Error):
        copy_beat_source(
            BeatSource('container-1', '/srv/app/celerybeat-schedule'),
            destination,
            max_bytes=max_bytes,
        )
    assert not destination.exists()
    assert list(destination.parent.glob('schedule.quarantined.*'))
    assert process.killed and process.wait_calls >= 2
    assert process.stdout.closed and process.stderr.closed


@pytest.mark.parametrize('case', ['nonzero', 'timeout', 'stream'])
def test_copy_beat_source_reaps_child_and_quarantines_every_failed_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    payload = b'schedule'
    archive = _tar_archive([(_regular_member('celerybeat-schedule', payload), payload)])

    class FailingStream(io.BytesIO):
        def read(self, _size: int = -1) -> bytes:
            raise OSError('stream failed')

    stdout = FailingStream(archive) if case == 'stream' else io.BytesIO(archive)
    process = _Process(
        stdout,
        returncode=1 if case == 'nonzero' else 0,
        timeout_once=case == 'timeout',
    )
    monkeypatch.setattr('scripts.d2_storage_bootstrap.subprocess.Popen', lambda *_args, **_kwargs: process)
    destination = _state(tmp_path) / 'schedule'
    with pytest.raises(D2Error):
        copy_beat_source(BeatSource('container-1', '/srv/app/celerybeat-schedule'), destination)
    assert not destination.exists()
    assert list(destination.parent.glob('schedule.quarantined.*'))
    assert process.killed and process.wait_calls >= 1
    assert process.stdout.closed and process.stderr.closed


def test_quiescence_capture_uses_module_monotonic_and_requires_five_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nodes = tuple(NodeSnapshot(f'node-{index}', f'queue-{index}', True, (), 0, 0, 0) for index in range(5))
    queues = tuple(QueueSnapshot(f'queue-{index}', 2, 0, 0) for index in range(5))
    clock = iter((0.0, 4.0, 5.0))
    monkeypatch.setattr('scripts.d2_storage_bootstrap.time.monotonic', lambda: next(clock))
    first = capture_quiescence_sample(nodes, queues)
    assert not reconcile_quiescence(first, capture_quiescence_sample(nodes, queues))
    assert reconcile_quiescence(first, capture_quiescence_sample(nodes, queues))
    bad = tuple(QueueSnapshot(f'queue-{index}', 2, 1 if index == 0 else 0, 0) for index in range(5))
    monkeypatch.setattr('scripts.d2_storage_bootstrap.time.monotonic', lambda: 10.0)
    assert not reconcile_quiescence(first, capture_quiescence_sample(nodes, bad))
    with pytest.raises(TypeError):
        QuiescenceSample(10.0, nodes, queues)


@pytest.mark.parametrize('timestamp', [math.nan, math.inf, -math.inf])
def test_quiescence_rejects_nonfinite_and_nonincreasing_clock(
    timestamp: float,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nodes = tuple(NodeSnapshot(f'node-{index}', f'queue-{index}', True, (), 0, 0, 0) for index in range(5))
    queues = tuple(QueueSnapshot(f'queue-{index}', 2, 0, 0) for index in range(5))
    monkeypatch.setattr('scripts.d2_storage_bootstrap.time.monotonic', lambda: 10.0)
    first = capture_quiescence_sample(nodes, queues)
    monkeypatch.setattr('scripts.d2_storage_bootstrap.time.monotonic', lambda: timestamp)
    assert not reconcile_quiescence(first, capture_quiescence_sample(nodes, queues))
    monkeypatch.setattr('scripts.d2_storage_bootstrap.time.monotonic', lambda: 10.0)
    assert not reconcile_quiescence(first, capture_quiescence_sample(nodes, queues))


def test_action_evidence_requires_returncode_and_exact_nested_selected_keys(
    tmp_path: Path,
) -> None:
    journal = Journal(_state(tmp_path) / 'journal.json', _identity())
    journal.record_intent(Action.OLD_ASYNC_STOP.value)
    with pytest.raises(D2Error):
        journal.record_completion(
            Action.OLD_ASYNC_STOP.value,
            {'selected': _evidence(Action.OLD_ASYNC_STOP.value)['selected']},
        )
    with pytest.raises(D2Error):
        journal.record_completion(
            Action.OLD_ASYNC_STOP.value,
            {
                'returncode': 0,
                'selected': {
                    **_evidence(Action.OLD_ASYNC_STOP.value)['selected'],
                    'stdout': 'secret',
                },
            },
        )
    journal.record_completion(Action.OLD_ASYNC_STOP.value, _evidence(Action.OLD_ASYNC_STOP.value))
    payload = json.loads(journal.path.read_text())
    payload['actions'][-1]['result']['selected']['container_id'] = {'unexpected': 'extra'}
    journal.path.write_text(json.dumps(payload))
    with pytest.raises(D2Error):
        Journal.load(journal.path)


@pytest.mark.parametrize('returncode', [99, True, '0'])
def test_action_evidence_requires_exact_integer_zero(tmp_path: Path, returncode: object) -> None:
    journal = Journal(_state(tmp_path) / 'journal.json', _identity())
    journal.record_intent(Action.OLD_ASYNC_STOP.value)
    evidence = _evidence(Action.OLD_ASYNC_STOP.value)
    evidence['returncode'] = returncode
    with pytest.raises(D2Error):
        journal.record_completion(Action.OLD_ASYNC_STOP.value, evidence)


@pytest.mark.parametrize(
    ('action', 'field', 'invalid'),
    [
        (Action.OLD_ASYNC_STOP, 'container_id', ''),
        (Action.OLD_ASYNC_STOP, 'child_exit', 1),
        (Action.OLD_ASYNC_STOP, 'child_exit', False),
        (Action.OLD_ASYNC_STOP, 'restart_policy', 'always'),
        (Action.RABBIT_STOP, 'manifest_sha256', 'not-a-sha256'),
        (Action.RABBIT_COPY, 'size', True),
        (Action.BEAT_COPY, 'path', 'relative'),
        (Action.NAMED_RABBIT_START, 'image_id', 'image'),
        (Action.RABBIT_ALIAS_CUTOVER, 'alias_owner', ''),
        (Action.OLD_ASYNC_RESTORE, 'container_ids', ['old', 1]),
        (Action.OLD_ASYNC_RESTORE, 'restored', 0),
        (Action.OLD_ASYNC_RESTORE, 'restored', False),
    ],
)
def test_action_evidence_requires_action_specific_types_and_semantics(
    tmp_path: Path,
    action: Action,
    field: str,
    invalid: object,
) -> None:
    journal = Journal(_state(tmp_path) / 'journal.json', _identity())
    for preceding in Action:
        journal.record_intent(preceding.value)
        evidence = _evidence(preceding.value)
        if preceding is action:
            selected = dict(evidence['selected'])
            selected[field] = invalid
            evidence['selected'] = selected
            with pytest.raises(D2Error):
                journal.record_completion(preceding.value, evidence)
            return
        journal.record_completion(preceding.value, evidence)
    raise AssertionError(action)


@pytest.mark.parametrize(
    'selected',
    [
        {'authority_state': 'pre_commit', 'recovery_decision': 'target-only'},
        {'authority_state': 'committed', 'recovery_decision': 'abort-old-source'},
    ],
)
def test_commit_evidence_requires_exact_selected_values(tmp_path: Path, selected: dict[str, str]) -> None:
    journal = Journal(_state(tmp_path) / 'journal.json', _identity())
    _complete_through(journal, Action.RABBIT_ALIAS_CUTOVER.value)
    journal.record_intent(Action.TARGET_AUTHORITY_COMMIT.value)
    with pytest.raises(D2Error):
        journal.record_completion(
            Action.TARGET_AUTHORITY_COMMIT.value,
            {'returncode': 0, 'selected': selected},
        )


def test_journal_snapshot_mutation_cannot_change_persisted_authority(tmp_path: Path) -> None:
    journal = Journal(_state(tmp_path) / 'journal.json', _identity())
    _complete_through(journal, Action.TARGET_AUTHORITY_COMMIT.value)
    snapshot = journal.data
    snapshot['authority_state'] = AuthorityState.PRE_COMMIT.value
    snapshot['recovery_decision'] = 'abort-old-source'
    snapshot['actions'].clear()
    assert decide_authority_recovery(journal, target_processing=True) == 'target-only'


def test_journal_load_requires_exact_top_level_and_action_keys(tmp_path: Path) -> None:
    journal = Journal(_state(tmp_path) / 'journal.json', _identity())
    journal.record_intent(Action.OLD_ASYNC_STOP.value)
    incomplete = json.loads(journal.path.read_text())
    for payload in (
        {**incomplete, 'extra': None},
        {key: value for key, value in incomplete.items() if key != 'schema'},
    ):
        journal.path.write_text(json.dumps(payload))
        with pytest.raises(D2Error):
            Journal.load(journal.path)
    journal.path.write_text(json.dumps(incomplete))
    for action in (
        {**incomplete['actions'][0], 'extra': None},
        {key: value for key, value in incomplete['actions'][0].items() if key != 'timestamp'},
    ):
        payload = {**incomplete, 'actions': [action]}
        journal.path.write_text(json.dumps(payload))
        with pytest.raises(D2Error):
            Journal.load(journal.path)

    journal.path.write_text(json.dumps(incomplete))
    loaded = Journal.load(journal.path)
    loaded.record_completion(Action.OLD_ASYNC_STOP.value, _evidence(Action.OLD_ASYNC_STOP.value))
    complete = json.loads(journal.path.read_text())
    for action in (
        {**complete['actions'][0], 'extra': None},
        {key: value for key, value in complete['actions'][0].items() if key != 'completed_at'},
    ):
        payload = {**complete, 'actions': [action]}
        journal.path.write_text(json.dumps(payload))
        with pytest.raises(D2Error):
            Journal.load(journal.path)


@pytest.mark.parametrize('field', ['timestamp', 'completed_at'])
@pytest.mark.parametrize('value', [True, math.nan, math.inf, -math.inf])
def test_journal_load_requires_finite_numeric_action_times(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    journal = Journal(_state(tmp_path) / 'journal.json', _identity())
    journal.record_intent(Action.OLD_ASYNC_STOP.value)
    journal.record_completion(Action.OLD_ASYNC_STOP.value, _evidence(Action.OLD_ASYNC_STOP.value))
    payload = json.loads(journal.path.read_text())
    payload['actions'][0][field] = value
    journal.path.write_text(json.dumps(payload))
    with pytest.raises(D2Error):
        Journal.load(journal.path)


def test_journal_load_rejects_completion_before_intent_and_completed_action_in_intent_phase(tmp_path: Path) -> None:
    journal = Journal(_state(tmp_path) / 'journal.json', _identity())
    journal.record_intent(Action.OLD_ASYNC_STOP.value)
    journal.record_completion(Action.OLD_ASYNC_STOP.value, _evidence(Action.OLD_ASYNC_STOP.value))
    payload = json.loads(journal.path.read_text())
    payload['actions'][0]['completed_at'] = payload['actions'][0]['timestamp'] - 1
    journal.path.write_text(json.dumps(payload))
    with pytest.raises(D2Error):
        Journal.load(journal.path)
    payload['actions'][0]['completed_at'] = payload['actions'][0]['timestamp']
    payload['phase'] = Phase.OLD_ASYNC_STOP_INTENT.value
    journal.path.write_text(json.dumps(payload))
    with pytest.raises(D2Error):
        Journal.load(journal.path)


def test_journal_load_requires_globally_ordered_action_timestamps(tmp_path: Path) -> None:
    journal = Journal(_state(tmp_path) / 'journal.json', _identity())
    _complete_through(journal, Action.RABBIT_STOP.value)
    payload = json.loads(journal.path.read_text())
    previous_completed_at = payload['actions'][0]['completed_at']
    payload['actions'][1]['timestamp'] = previous_completed_at
    payload['actions'][1]['completed_at'] = previous_completed_at
    journal.path.write_text(json.dumps(payload))
    Journal.load(journal.path)
    payload['actions'][1]['timestamp'] = previous_completed_at - 1
    journal.path.write_text(json.dumps(payload))
    with pytest.raises(D2Error):
        Journal.load(journal.path)


def test_target_labels_and_effective_config_are_strict() -> None:
    names = volume_names('a1b2c3d4')
    identity = TargetIdentity('a1b2c3d4', names[0], names[1])
    validate_target_labels(identity.labels(), identity)
    with pytest.raises(D2Error):
        validate_target_labels({**identity.labels(), 'd2.lifecycle': 'active'}, identity)
    assert (
        validate_effective_config(
            {
                'broker_connection_retry_on_startup': True,
                'broker_connection_retry': False,
                'broker_connection_max_retries': None,
            }
        )['broker_connection_max_retries']
        is None
    )
    with pytest.raises(D2Error):
        validate_effective_config(
            {
                'broker_connection_retry_on_startup': True,
                'broker_connection_retry': False,
                'broker_connection_max_retries': True,
            }
        )
