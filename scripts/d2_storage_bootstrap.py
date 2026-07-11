from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import selectors
import stat as statmod
import subprocess
import tarfile
import time
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from types import MappingProxyType


class D2Error(RuntimeError):
    pass


_SECRET_KEY_RE = re.compile(
    r'(?i)(?:password|token|secret|cookie|api[_-]?key|broker[_-]?url|database[_-]?url|redis[_-]?url|authorization)'
)
_STATE_DIR_MODE = 0o700
_STATE_FILE_MODE = 0o600
_SHA256_RE = re.compile(r'^sha256:[0-9a-f]{64}$')
_REVISION_RE = re.compile(r'^[0-9a-f]{40}$')


def _canonical_directory(path: Path, message: str) -> Path:
    if not path.is_absolute() or path != path.resolve():
        raise D2Error(message)
    try:
        stat = path.stat()
        lstat = path.lstat()
    except OSError as error:
        raise D2Error(message) from error
    if not path.is_dir() or lstat.st_mode != stat.st_mode or stat.st_uid != 0:
        raise D2Error(message)
    if stat.st_mode & 0o777 != _STATE_DIR_MODE:
        raise D2Error(message)
    return path


def _open_state_directory(path: Path) -> int:
    canonical = _canonical_directory(path, 'D2 state directory must pre-exist as a root-owned mode 0700 directory')
    try:
        fd = os.open(
            canonical,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0),
        )
    except OSError as error:
        raise D2Error('D2 state directory cannot be opened safely') from error
    try:
        stat = os.fstat(fd)
        if stat.st_uid != 0 or stat.st_mode & 0o777 != _STATE_DIR_MODE or not canonical.is_dir():
            raise D2Error('D2 state directory identity changed')
        return fd
    except BaseException:
        os.close(fd)
        raise


def _secure_file_stat(fd: int) -> os.stat_result:
    stat = os.fstat(fd)
    if stat.st_uid != 0 or stat.st_mode & 0o777 != _STATE_FILE_MODE or not statmod.S_ISREG(stat.st_mode):
        raise D2Error('D2 state file must be a root-owned mode 0600 regular file')
    return stat


@dataclass(frozen=True)
class BindDirectory:
    host_path: Path

    def __post_init__(self) -> None:
        _canonical_directory(self.host_path, 'Rabbit bind path must be an existing canonical directory')


@dataclass(frozen=True)
class BeatSource:
    container_id: str
    absolute_path: str
    sidecar_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        path = PurePosixPath(self.absolute_path)
        if (
            not self.container_id
            or not path.is_absolute()
            or '..' in path.parts
            or str(path) != self.absolute_path
            or not path.name
        ):
            raise D2Error('Beat source must identify an absolute container path')
        if len(self.sidecar_paths) != len(set(self.sidecar_paths)):
            raise D2Error('Beat sidecar paths must be unique')


def parse_rabbit_source(inspect: Mapping[str, object], *, expected_host_path: Path) -> BindDirectory:
    mounts = inspect.get('Mounts')
    if not isinstance(mounts, list):
        raise D2Error('Rabbit inspect mounts are malformed')
    matches = [
        mount for mount in mounts if isinstance(mount, Mapping) and mount.get('Destination') == '/var/lib/rabbitmq'
    ]
    if len(matches) != 1:
        raise D2Error('Rabbit must have exactly one expected storage mount')
    mount = matches[0]
    source = mount.get('Source')
    if mount.get('Type') != 'bind' or not isinstance(source, str):
        raise D2Error('Rabbit storage must be a bind directory')
    source_path = Path(source)
    expected = expected_host_path
    if (
        not source_path.is_absolute()
        or source_path != source_path.resolve()
        or expected != expected.resolve()
        or source_path != expected
    ):
        raise D2Error('Rabbit bind path is not the inspected canonical path')
    return BindDirectory(source_path)


def parse_beat_source(
    inspect: Mapping[str, object],
    absolute_path: str,
    *,
    siblings: Sequence[str] | None = None,
) -> BeatSource:
    path = PurePosixPath(absolute_path)
    if not path.is_absolute() or '..' in path.parts or str(path) != absolute_path or not path.name:
        raise D2Error('Beat schedule path must be canonical and absolute')
    container_id = inspect.get('Id')
    if not isinstance(container_id, str) or not container_id:
        raise D2Error('Beat inspect container id is missing')
    file_types = inspect.get('Files')
    if not isinstance(file_types, Mapping) or file_types.get(absolute_path) != 'regular':
        raise D2Error('Beat schedule must be an explicitly enumerated regular file')
    if siblings is None:
        raise D2Error('Beat sidecar enumeration is required')
    sidecars = list(siblings)
    if len(sidecars) != len(set(sidecars)):
        raise D2Error('Beat sidecar paths must be unique')
    expected_paths = {absolute_path, *sidecars}
    if set(file_types) != expected_paths:
        raise D2Error('Beat file enumeration contains missing or unrelated paths')
    for sibling in sidecars:
        sibling_path = PurePosixPath(sibling)
        if (
            not sibling_path.is_absolute()
            or '..' in sibling_path.parts
            or str(sibling_path) != sibling
            or sibling_path.parent != path.parent
            or not (sibling_path.name.startswith(f'{path.name}.') or sibling_path.name.startswith(f'{path.name}-'))
            or file_types.get(sibling) != 'regular'
        ):
            raise D2Error('Beat sidecars must be canonical regular same-parent files')
    return BeatSource(container_id, absolute_path, tuple(sidecars))


@dataclass(frozen=True)
class RuntimeIdentity:
    image_id: str
    repository_digest: str
    revision: str


def parse_runtime_identity(inspect: Mapping[str, object]) -> RuntimeIdentity:
    image_id = inspect.get('Image') or inspect.get('ImageID')
    digests = inspect.get('RepoDigests')
    config = inspect.get('Config')
    labels = config.get('Labels') if isinstance(config, Mapping) else inspect.get('Labels')
    revision = labels.get('org.opencontainers.image.revision') if isinstance(labels, Mapping) else None
    if (
        not isinstance(image_id, str)
        or not isinstance(digests, list)
        or len(digests) != 1
        or not isinstance(digests[0], str)
        or not isinstance(revision, str)
    ):
        raise D2Error('Runtime inspect identity is malformed')
    return RuntimeIdentity(image_id, digests[0], revision)


def validate_runtime_agreement(runtimes: Sequence[RuntimeIdentity]) -> RuntimeIdentity:
    if not runtimes or any(not item.image_id or not item.repository_digest or not item.revision for item in runtimes):
        raise D2Error('Runtime identity is incomplete')
    first = runtimes[0]
    if any(item != first for item in runtimes[1:]):
        raise D2Error('Async runtime image, digest, and revision do not agree')
    return first


@dataclass(frozen=True)
class BackendSubject:
    name: str
    image_id: str
    repository_digest: str
    revision: str

    def __post_init__(self) -> None:
        if not self.name or not self.image_id or not self.repository_digest or not self.revision:
            raise D2Error('Backend subject identity is incomplete')
        if _SECRET_KEY_RE.search(self.name):
            raise D2Error('Backend subject name is secret-like')
        if (
            not _SHA256_RE.fullmatch(self.image_id)
            or not re.fullmatch(r'[^@\s]+@sha256:[0-9a-f]{64}', self.repository_digest)
            or not _REVISION_RE.fullmatch(self.revision)
        ):
            raise D2Error('Backend subject digest or revision is malformed')

    def to_mapping(self) -> dict[str, str]:
        return {
            'image_id': self.image_id,
            'repository_digest': self.repository_digest,
            'revision': self.revision,
        }


_BACKEND_SUBJECTS = frozenset(
    {
        'api',
        'worker-realtime',
        'worker-near-realtime',
        'worker-batch',
        'worker-highmemory',
        'worker-domain-events',
        'relay',
        'beat',
    }
)
_IDENTITY_FIELDS = frozenset(
    {
        'attempt_id',
        'rabbit_volume',
        'beat_volume',
        'backend_subjects',
        'rabbit_source_id',
        'rabbit_source_path',
        'beat_source_id',
        'beat_source_path',
        'compose_hash',
        'rabbit_config_hash',
        'rabbit_nodename',
        'rabbit_hostname',
        'rabbit_mnesia_dir',
    }
)


@dataclass(frozen=True)
class JournalIdentity:
    attempt_id: str
    rabbit_volume: str
    beat_volume: str
    backend_subjects: Mapping[str, BackendSubject]
    rabbit_source_id: str
    rabbit_source_path: str
    beat_source_id: str
    beat_source_path: str
    compose_hash: str
    rabbit_config_hash: str
    rabbit_nodename: str
    rabbit_hostname: str
    rabbit_mnesia_dir: str

    def __post_init__(self) -> None:
        volume_names(self.attempt_id)
        expected_rabbit, expected_beat = volume_names(self.attempt_id)
        if (self.rabbit_volume, self.beat_volume) != (expected_rabbit, expected_beat):
            raise D2Error('Journal target volumes do not match attempt identity')
        if set(self.backend_subjects) != _BACKEND_SUBJECTS:
            raise D2Error('Journal identity must contain exactly the eight backend subjects')
        subjects = {
            name: value if isinstance(value, BackendSubject) else BackendSubject(name, **value)
            for name, value in self.backend_subjects.items()
        }
        identities = {(item.image_id, item.repository_digest, item.revision) for item in subjects.values()}
        if len(identities) != 1:
            raise D2Error('Backend subjects must share image id, repository digest, and revision')
        for key, value in self.to_mapping(include_subjects=False).items():
            if _SECRET_KEY_RE.search(key) or not isinstance(value, str) or not value:
                raise D2Error('Journal identity contains an invalid or secret-like field')
        if not _SHA256_RE.fullmatch(self.compose_hash) or not _SHA256_RE.fullmatch(self.rabbit_config_hash):
            raise D2Error('Journal configuration hashes are malformed')
        if not self.rabbit_source_path.startswith('/') or not self.beat_source_path.startswith('/'):
            raise D2Error('Journal source paths must be absolute')
        object.__setattr__(self, 'backend_subjects', MappingProxyType(subjects))

    def to_mapping(self, *, include_subjects: bool = True) -> dict[str, object]:
        mapping: dict[str, object] = {
            'attempt_id': self.attempt_id,
            'rabbit_volume': self.rabbit_volume,
            'beat_volume': self.beat_volume,
            'rabbit_source_id': self.rabbit_source_id,
            'rabbit_source_path': self.rabbit_source_path,
            'beat_source_id': self.beat_source_id,
            'beat_source_path': self.beat_source_path,
            'compose_hash': self.compose_hash,
            'rabbit_config_hash': self.rabbit_config_hash,
            'rabbit_nodename': self.rabbit_nodename,
            'rabbit_hostname': self.rabbit_hostname,
            'rabbit_mnesia_dir': self.rabbit_mnesia_dir,
        }
        if include_subjects:
            mapping['backend_subjects'] = {
                name: subject.to_mapping() for name, subject in self.backend_subjects.items()
            }
        return mapping

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> JournalIdentity:
        if set(payload) != _IDENTITY_FIELDS or any(_SECRET_KEY_RE.search(str(key)) for key in payload):
            raise D2Error('Journal identity fields are not exact and non-secret')
        subjects_payload = payload.get('backend_subjects')
        if not isinstance(subjects_payload, Mapping) or set(subjects_payload) != _BACKEND_SUBJECTS:
            raise D2Error('Journal identity backend subjects are not exact')
        subjects: dict[str, BackendSubject] = {}
        for name, value in subjects_payload.items():
            if not isinstance(value, Mapping) or set(value) != {
                'image_id',
                'repository_digest',
                'revision',
            }:
                raise D2Error('Backend subject fields are not exact')
            if any(_SECRET_KEY_RE.search(str(key)) for key in value):
                raise D2Error('Backend subject contains a secret-like field')
            subjects[name] = BackendSubject(name, value['image_id'], value['repository_digest'], value['revision'])
        values = {key: payload[key] for key in _IDENTITY_FIELDS if key != 'backend_subjects'}
        if any(not isinstance(value, str) for value in values.values()):
            raise D2Error('Journal identity values must be strings')
        return cls(backend_subjects=subjects, **values)


def validate_effective_config(values: Mapping[str, object]) -> dict[str, object]:
    required = (
        'broker_connection_retry_on_startup',
        'broker_connection_retry',
        'broker_connection_max_retries',
    )
    if set(values) != set(required):
        raise D2Error('Effective Celery config must contain exactly selected keys')
    if not isinstance(values[required[0]], bool) or not isinstance(values[required[1]], bool):
        raise D2Error('Celery retry flags must be booleans')
    if values[required[2]] is not None and (
        not isinstance(values[required[2]], int) or isinstance(values[required[2]], bool)
    ):
        raise D2Error('Celery max retries must be an integer or null')
    return dict(values)


class Phase(StrEnum):
    PRECHECK = 'PRECHECK'
    OLD_ASYNC_STOP_INTENT = 'OLD_ASYNC_STOP_INTENT'
    OLD_ASYNC_STOP_COMPLETE = 'OLD_ASYNC_STOP_COMPLETE'
    RABBIT_STOP_INTENT = 'RABBIT_STOP_INTENT'
    RABBIT_STOP_COMPLETE = 'RABBIT_STOP_COMPLETE'
    RABBIT_COPY_INTENT = 'RABBIT_COPY_INTENT'
    RABBIT_COPY_COMPLETE = 'RABBIT_COPY_COMPLETE'
    BEAT_COPY_INTENT = 'BEAT_COPY_INTENT'
    BEAT_COPY_COMPLETE = 'BEAT_COPY_COMPLETE'
    NAMED_RABBIT_START_INTENT = 'NAMED_RABBIT_START_INTENT'
    NAMED_RABBIT_VERIFIED = 'NAMED_RABBIT_VERIFIED'
    RABBIT_ALIAS_CUTOVER_INTENT = 'RABBIT_ALIAS_CUTOVER_INTENT'
    RABBIT_ALIAS_CUTOVER_COMPLETE = 'RABBIT_ALIAS_CUTOVER_COMPLETE'
    TARGET_AUTHORITY_COMMIT_INTENT = 'TARGET_AUTHORITY_COMMIT_INTENT'
    TARGET_AUTHORITY_COMMITTED = 'TARGET_AUTHORITY_COMMITTED'
    OLD_ASYNC_RESTORE_INTENT = 'OLD_ASYNC_RESTORE_INTENT'
    OLD_ASYNC_RESTORED = 'OLD_ASYNC_RESTORED'
    COMPLETE = 'COMPLETE'


_LEGAL_NEXT_PHASE: dict[Phase, Phase] = {
    Phase.PRECHECK: Phase.OLD_ASYNC_STOP_INTENT,
    Phase.OLD_ASYNC_STOP_INTENT: Phase.OLD_ASYNC_STOP_COMPLETE,
    Phase.OLD_ASYNC_STOP_COMPLETE: Phase.RABBIT_STOP_INTENT,
    Phase.RABBIT_STOP_INTENT: Phase.RABBIT_STOP_COMPLETE,
    Phase.RABBIT_STOP_COMPLETE: Phase.RABBIT_COPY_INTENT,
    Phase.RABBIT_COPY_INTENT: Phase.RABBIT_COPY_COMPLETE,
    Phase.RABBIT_COPY_COMPLETE: Phase.BEAT_COPY_INTENT,
    Phase.BEAT_COPY_INTENT: Phase.BEAT_COPY_COMPLETE,
    Phase.BEAT_COPY_COMPLETE: Phase.NAMED_RABBIT_START_INTENT,
    Phase.NAMED_RABBIT_START_INTENT: Phase.NAMED_RABBIT_VERIFIED,
    Phase.NAMED_RABBIT_VERIFIED: Phase.RABBIT_ALIAS_CUTOVER_INTENT,
    Phase.RABBIT_ALIAS_CUTOVER_INTENT: Phase.RABBIT_ALIAS_CUTOVER_COMPLETE,
    Phase.RABBIT_ALIAS_CUTOVER_COMPLETE: Phase.TARGET_AUTHORITY_COMMIT_INTENT,
    Phase.TARGET_AUTHORITY_COMMIT_INTENT: Phase.TARGET_AUTHORITY_COMMITTED,
    Phase.TARGET_AUTHORITY_COMMITTED: Phase.OLD_ASYNC_RESTORE_INTENT,
    Phase.OLD_ASYNC_RESTORE_INTENT: Phase.OLD_ASYNC_RESTORED,
    Phase.OLD_ASYNC_RESTORED: Phase.COMPLETE,
}


class Action(StrEnum):
    OLD_ASYNC_STOP = 'old_async_stop'
    RABBIT_STOP = 'rabbit_stop'
    RABBIT_COPY = 'rabbit_copy'
    BEAT_COPY = 'beat_copy'
    NAMED_RABBIT_START = 'named_rabbit_start'
    RABBIT_ALIAS_CUTOVER = 'rabbit_alias_cutover'
    TARGET_AUTHORITY_COMMIT = 'target_authority_commit'
    OLD_ASYNC_RESTORE = 'old_async_restore'


class AuthorityState(StrEnum):
    PRE_COMMIT = 'pre_commit'
    COMMITTED = 'committed'


_ACTION_PHASES: dict[str, tuple[Phase, Phase]] = {
    Action.OLD_ASYNC_STOP.value: (
        Phase.OLD_ASYNC_STOP_INTENT,
        Phase.OLD_ASYNC_STOP_COMPLETE,
    ),
    Action.RABBIT_STOP.value: (Phase.RABBIT_STOP_INTENT, Phase.RABBIT_STOP_COMPLETE),
    Action.RABBIT_COPY.value: (Phase.RABBIT_COPY_INTENT, Phase.RABBIT_COPY_COMPLETE),
    Action.BEAT_COPY.value: (Phase.BEAT_COPY_INTENT, Phase.BEAT_COPY_COMPLETE),
    Action.NAMED_RABBIT_START.value: (
        Phase.NAMED_RABBIT_START_INTENT,
        Phase.NAMED_RABBIT_VERIFIED,
    ),
    Action.RABBIT_ALIAS_CUTOVER.value: (
        Phase.RABBIT_ALIAS_CUTOVER_INTENT,
        Phase.RABBIT_ALIAS_CUTOVER_COMPLETE,
    ),
    Action.TARGET_AUTHORITY_COMMIT.value: (
        Phase.TARGET_AUTHORITY_COMMIT_INTENT,
        Phase.TARGET_AUTHORITY_COMMITTED,
    ),
    Action.OLD_ASYNC_RESTORE.value: (
        Phase.OLD_ASYNC_RESTORE_INTENT,
        Phase.OLD_ASYNC_RESTORED,
    ),
}
_ACTION_ORDER = tuple(_ACTION_PHASES)
_ACTION_EVIDENCE_FIELDS: dict[str, frozenset[str]] = {
    Action.OLD_ASYNC_STOP.value: frozenset({'container_id', 'child_exit', 'restart_policy'}),
    Action.RABBIT_STOP.value: frozenset({'container_id', 'manifest_sha256', 'nodename'}),
    Action.RABBIT_COPY.value: frozenset({'volume', 'sha256', 'size'}),
    Action.BEAT_COPY.value: frozenset({'path', 'sha256', 'size'}),
    Action.NAMED_RABBIT_START.value: frozenset({'container_id', 'image_id', 'nodename'}),
    Action.RABBIT_ALIAS_CUTOVER.value: frozenset({'old_container_id', 'target_container_id', 'alias_owner'}),
    Action.TARGET_AUTHORITY_COMMIT.value: frozenset({'authority_state', 'recovery_decision'}),
    Action.OLD_ASYNC_RESTORE.value: frozenset({'container_ids', 'restored'}),
}


def _sanitize_tree(
    value: object,
    allowed_keys: frozenset[str],
    depth: int = 0,
    *,
    secrets: Sequence[str],
) -> object:
    if depth > 5:
        raise D2Error('Action evidence is too deeply nested')
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return redact_and_bound(value, secrets)
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str) or key not in allowed_keys or _SECRET_KEY_RE.search(key):
                raise D2Error('Action evidence contains an unapproved or secret-like key')
            result[key] = _sanitize_tree(item, allowed_keys, depth + 1, secrets=secrets)
        return result
    if isinstance(value, (list, tuple)):
        if len(value) > 32:
            raise D2Error('Action evidence has too many items')
        return [_sanitize_tree(item, allowed_keys, depth + 1, secrets=secrets) for item in value]
    raise D2Error('Action evidence contains an unsupported value')


def _sanitize_action_evidence(name: str, result: Mapping[str, object], *, secrets: Sequence[str]) -> dict[str, object]:
    required = _ACTION_EVIDENCE_FIELDS.get(name)
    if required is None or set(result) != {'returncode', 'selected'}:
        raise D2Error('Action evidence schema is not exact')
    returncode = result.get('returncode')
    selected = result.get('selected')
    if type(returncode) is not int or returncode != 0 or not isinstance(selected, Mapping):
        raise D2Error('Action evidence requires returncode and selected fields')
    if set(selected) != required:
        raise D2Error('Action evidence selected fields are incomplete or extra')
    sanitized = _sanitize_tree(selected, required, secrets=secrets)
    if not isinstance(sanitized, dict):
        raise D2Error('Action evidence selected fields are malformed')
    _validate_action_selected(name, sanitized)
    return {'returncode': returncode, 'selected': sanitized}


def _validate_action_selected(name: str, selected: Mapping[str, object]) -> None:  # noqa: C901
    string_fields = {
        Action.OLD_ASYNC_STOP.value: ('container_id', 'restart_policy'),
        Action.RABBIT_STOP.value: ('container_id', 'manifest_sha256', 'nodename'),
        Action.RABBIT_COPY.value: ('volume', 'sha256'),
        Action.BEAT_COPY.value: ('path', 'sha256'),
        Action.NAMED_RABBIT_START.value: ('container_id', 'image_id', 'nodename'),
        Action.RABBIT_ALIAS_CUTOVER.value: ('old_container_id', 'target_container_id', 'alias_owner'),
        Action.TARGET_AUTHORITY_COMMIT.value: ('authority_state', 'recovery_decision'),
    }.get(name, ())
    if any(not isinstance(selected[field], str) or not selected[field] for field in string_fields):
        raise D2Error('Action evidence selected values are malformed')
    if name == Action.OLD_ASYNC_STOP.value and (
        selected['child_exit'] is not True or selected['restart_policy'] != 'no'
    ):
        raise D2Error('Action evidence selected values are malformed')
    if name in {Action.RABBIT_COPY.value, Action.BEAT_COPY.value}:
        if type(selected['size']) is not int or selected['size'] < 0:
            raise D2Error('Action evidence selected values are malformed')
        if re.fullmatch(r'[0-9a-f]{64}', selected['sha256']) is None:
            raise D2Error('Action evidence selected values are malformed')
    if name == Action.RABBIT_STOP.value and re.fullmatch(r'[0-9a-f]{64}', selected['manifest_sha256']) is None:
        raise D2Error('Action evidence selected values are malformed')
    if name == Action.BEAT_COPY.value and not selected['path'].startswith('/'):
        raise D2Error('Action evidence selected values are malformed')
    if name == Action.NAMED_RABBIT_START.value and _SHA256_RE.fullmatch(selected['image_id']) is None:
        raise D2Error('Action evidence selected values are malformed')
    if name == Action.RABBIT_ALIAS_CUTOVER.value and selected['alias_owner'] != selected['target_container_id']:
        raise D2Error('Action evidence selected values are malformed')
    if name == Action.TARGET_AUTHORITY_COMMIT.value and dict(selected) != {
        'authority_state': 'committed',
        'recovery_decision': 'target-only',
    }:
        raise D2Error('Action evidence selected values are malformed')
    if name == Action.OLD_ASYNC_RESTORE.value:
        container_ids = selected['container_ids']
        if (
            not isinstance(container_ids, list)
            or any(not isinstance(item, str) or not item for item in container_ids)
            or len(container_ids) != len(set(container_ids))
            or selected['restored'] is not True
        ):
            raise D2Error('Action evidence selected values are malformed')


class FileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._directory_fd: int | None = None
        self._file_fd: int | None = None

    def acquire(self) -> None:
        self._directory_fd = _open_state_directory(self.path.parent)
        try:
            self._file_fd = os.open(
                self.path.name,
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0),
                _STATE_FILE_MODE,
                dir_fd=self._directory_fd,
            )
            _secure_file_stat(self._file_fd)
            try:
                fcntl.flock(self._file_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise D2Error('D2 controller lock is already held') from error
        except OSError as error:
            self.release()
            raise D2Error('D2 controller lock path is unsafe') from error
        except BaseException:
            self.release()
            raise

    def release(self) -> None:
        if self._file_fd is not None:
            try:
                fcntl.flock(self._file_fd, fcntl.LOCK_UN)
            finally:
                os.close(self._file_fd)
                self._file_fd = None
        if self._directory_fd is not None:
            os.close(self._directory_fd)
            self._directory_fd = None

    def __enter__(self) -> FileLock:
        self.acquire()
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.release()


def atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    directory_fd = _open_state_directory(path.parent)
    temporary_name = f'.{path.name}.{os.getpid()}.{time.time_ns()}.tmp'
    try:
        try:
            existing_fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
        except FileNotFoundError:
            existing_fd = None
        if existing_fd is not None:
            try:
                _secure_file_stat(existing_fd)
            finally:
                os.close(existing_fd)
        file_fd = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0),
            _STATE_FILE_MODE,
            dir_fd=directory_fd,
        )
        try:
            _secure_file_stat(file_fd)
            content = json.dumps(payload, sort_keys=True, separators=(',', ':')) + '\n'
            os.write(file_fd, content.encode('utf-8'))
            os.fsync(file_fd)
        finally:
            os.close(file_fd)
        os.replace(temporary_name, path.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    except OSError as error:
        raise D2Error('D2 journal atomic write failed') from error
    finally:
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.close(directory_fd)


def _read_json_secure(path: Path) -> Mapping[str, object]:
    directory_fd = _open_state_directory(path.parent)
    try:
        try:
            file_fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
        except OSError as error:
            raise D2Error('D2 journal is missing or malformed') from error
        try:
            _secure_file_stat(file_fd)
            content = b''
            while chunk := os.read(file_fd, 1024 * 1024):
                content += chunk
        finally:
            os.close(file_fd)
    finally:
        os.close(directory_fd)
    try:
        payload = json.loads(content.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise D2Error('D2 journal is missing or malformed') from error
    if not isinstance(payload, dict):
        raise D2Error('D2 journal is malformed')
    return payload


def redact_and_bound(value: str, secrets: Sequence[str] = (), *, limit: int = 4000) -> str:
    if limit <= 0:
        return ''
    redacted = value
    bearer_tokens = re.findall(r'(?i)(\bbearer\s+)([^\s,;\]\}\"\']+)', redacted)
    for secret in sorted({item for item in secrets if item}, key=len, reverse=True):
        redacted = redacted.replace(secret, '[REDACTED]')
    redacted = re.sub(
        r'(?i)(\bbearer\s+)[^\s,;\]\}\"\']+',
        r'\1[REDACTED]',
        redacted,
    )
    redacted = re.sub(
        r'(?i)(["\']?(?:ERLANG_COOKIE|[A-Z][A-Z0-9_]*(?:PASSWORD|TOKEN|SECRET|API_KEY)|BROKER_URL|DATABASE_URL|REDIS_URL|password|token|cookie)["\']?\s*[=:]\s*["\']?)([^"\'\s}},]+)',
        r'\1[REDACTED]',
        redacted,
    )
    redacted = re.sub(
        r'(?i)(--?(?:password|token|cookie|secret|api[-_]key|broker[-_]url|database[-_]url|redis[-_]url)\s+)[^\s]+',
        r'\1[REDACTED]',
        redacted,
    )
    for _prefix, token in bearer_tokens:
        if token and token in redacted:
            raise D2Error('Bearer token remained after redaction')
    return redacted[-limit:]


@dataclass(frozen=True)
class BeatCopyMetadata:
    path: Path
    size: int
    sha256: str
    returncode: int
    stderr: str


def copy_beat_source(  # noqa: C901
    beat_source: BeatSource,
    destination: Path,
    *,
    source_path: str | None = None,
    timeout: float = 180.0,
    max_bytes: int = 16 * 1024 * 1024,
    output_limit: int = 4000,
    secrets: Sequence[str] = (),
) -> BeatCopyMetadata:
    selected_path = source_path or beat_source.absolute_path
    if selected_path not in {beat_source.absolute_path, *beat_source.sidecar_paths}:
        raise D2Error('Beat copy source differs from journaled descriptor')
    if type(max_bytes) is not int or max_bytes <= 0 or not math.isfinite(timeout) or timeout <= 0:
        raise D2Error('Beat copy bounds must be positive')
    if type(output_limit) is not int or output_limit < 0:
        raise D2Error('Beat copy output limit must be nonnegative')

    directory_fd = _open_state_directory(destination.parent)
    archive_name = f'.{destination.name}.tar.partial'
    payload_name = f'.{destination.name}.partial'
    archive_fd: int | None = None
    payload_fd: int | None = None
    process: subprocess.Popen[bytes] | None = None
    stdout_stream: object | None = None
    stderr_stream: object | None = None
    selector: selectors.BaseSelector | None = None
    stderr = b''
    failed = True
    published = False
    try:
        try:
            existing_fd = os.open(destination.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
        except FileNotFoundError:
            existing_fd = None
        except OSError as error:
            raise D2Error('Beat copy destination is unsafe') from error
        if existing_fd is not None:
            try:
                _secure_file_stat(existing_fd)
            finally:
                os.close(existing_fd)
            raise D2Error('Beat copy destination already exists')

        archive_fd = os.open(
            archive_name,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0),
            _STATE_FILE_MODE,
            dir_fd=directory_fd,
        )
        _secure_file_stat(archive_fd)
        process = subprocess.Popen(  # noqa: S603
            ['/usr/bin/docker', 'cp', f'{beat_source.container_id}:{selected_path}', '-'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd='/',
            env={
                'DOCKER_HOST': 'unix:///var/run/docker.sock',
                'DOCKER_CONTEXT': 'default',
            },
        )
        stdout_stream = process.stdout
        stderr_stream = process.stderr
        if stdout_stream is None or stderr_stream is None:
            raise D2Error('Beat copy streams are unavailable')

        deadline = time.monotonic() + timeout
        archive_size = 0
        archive_limit = max_bytes + 64 * 1024
        try:
            stdout_stream.fileno()  # type: ignore[attr-defined]
            stderr_stream.fileno()  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            while True:
                chunk = stdout_stream.read(64 * 1024)  # type: ignore[attr-defined]
                if not chunk:
                    break
                archive_size += len(chunk)
                if archive_size > archive_limit:
                    raise D2Error('Beat copy archive exceeds maximum size') from None
                os.write(archive_fd, chunk)
            stderr = stderr_stream.read(output_limit * 2) if output_limit else b''  # type: ignore[attr-defined]
        else:
            selector = selectors.DefaultSelector()
            selector.register(stdout_stream, selectors.EVENT_READ, 'stdout')
            selector.register(stderr_stream, selectors.EVENT_READ, 'stderr')
            open_streams = {'stdout', 'stderr'}
            while open_streams:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                events = selector.select(min(remaining, 0.2))
                if not events:
                    continue
                for key, _ in events:
                    chunk = os.read(key.fd, 64 * 1024)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        open_streams.discard(key.data)
                    elif key.data == 'stdout':
                        archive_size += len(chunk)
                        if archive_size > archive_limit:
                            raise D2Error('Beat copy archive exceeds maximum size')
                        os.write(archive_fd, chunk)
                    elif output_limit:
                        stderr = (stderr + chunk)[-output_limit * 2 :]
        returncode = process.wait(timeout=max(0.01, deadline - time.monotonic()))
        if returncode != 0:
            raise D2Error('Beat copy command failed')
        os.fsync(archive_fd)

        os.lseek(archive_fd, 0, os.SEEK_SET)
        with (
            os.fdopen(os.dup(archive_fd), 'rb') as archive_file,
            tarfile.open(fileobj=archive_file, mode='r:') as archive,
        ):
            members = archive.getmembers()
        expected_name = PurePosixPath(selected_path).name
        if len(members) != 1:
            raise D2Error('Beat copy archive must contain exactly one member')
        member = members[0]
        member_path = PurePosixPath(member.name)
        if (
            member.name != expected_name
            or member_path.is_absolute()
            or '..' in member_path.parts
            or not member.isreg()
            or member.size < 0
            or member.size > max_bytes
        ):
            raise D2Error('Beat copy archive member is unsafe')

        payload_fd = os.open(
            payload_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0),
            _STATE_FILE_MODE,
            dir_fd=directory_fd,
        )
        _secure_file_stat(payload_fd)
        os.lseek(archive_fd, member.offset_data, os.SEEK_SET)
        digest = hashlib.sha256()
        size = 0
        remaining = member.size
        while remaining:
            chunk = os.read(archive_fd, min(64 * 1024, remaining))
            if not chunk:
                raise D2Error('Beat copy archive payload is truncated')
            size += len(chunk)
            if size > max_bytes:
                raise D2Error('Beat copy payload exceeds maximum size')
            os.write(payload_fd, chunk)
            digest.update(chunk)
            remaining -= len(chunk)
        os.fsync(payload_fd)
        os.close(payload_fd)
        payload_fd = None
        os.close(archive_fd)
        archive_fd = None
        os.unlink(archive_name, dir_fd=directory_fd)
        os.replace(payload_name, destination.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        published = True
        os.fsync(directory_fd)
        failed = False
        return BeatCopyMetadata(
            destination,
            size,
            digest.hexdigest(),
            returncode,
            redact_and_bound(stderr.decode('utf-8', 'replace'), secrets, limit=output_limit),
        )
    except (TimeoutError, subprocess.TimeoutExpired) as error:
        raise D2Error('bounded Beat copy timed out') from error
    except D2Error:
        raise
    except BaseException as error:
        raise D2Error('Beat copy failed') from error
    finally:
        if failed and process is not None:
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass
        if selector is not None:
            try:
                selector.close()
            except OSError:
                pass
        for stream in (stdout_stream, stderr_stream):
            if stream is not None:
                try:
                    stream.close()  # type: ignore[attr-defined]
                except OSError:
                    pass
        if payload_fd is not None:
            os.close(payload_fd)
        if archive_fd is not None:
            os.close(archive_fd)
        if failed:
            failed_names = [payload_name, archive_name]
            if published:
                failed_names.append(destination.name)
            for partial_name in failed_names:
                try:
                    os.replace(
                        partial_name,
                        f'{destination.name}.quarantined.{time.time_ns()}',
                        src_dir_fd=directory_fd,
                        dst_dir_fd=directory_fd,
                    )
                except OSError:
                    try:
                        os.unlink(partial_name, dir_fd=directory_fd)
                    except OSError:
                        pass
        os.close(directory_fd)


@dataclass(frozen=True)
class NodeSnapshot:
    node: str
    queue: str
    cancellation_ack: bool
    active_queues: tuple[str, ...]
    active: int
    reserved: int
    scheduled: int

    def __post_init__(self) -> None:
        object.__setattr__(self, 'active_queues', tuple(self.active_queues))


@dataclass(frozen=True)
class QueueSnapshot:
    queue: str
    ready: int
    unacknowledged: int
    native_delay: int


@dataclass(frozen=True, init=False)
class QuiescenceSample:
    nodes: tuple[NodeSnapshot, ...]
    queues: tuple[QueueSnapshot, ...]
    captured_at: float

    def __init__(self, nodes: Sequence[NodeSnapshot], queues: Sequence[QueueSnapshot]) -> None:
        object.__setattr__(self, 'nodes', tuple(nodes))
        object.__setattr__(self, 'queues', tuple(queues))
        object.__setattr__(self, 'captured_at', time.monotonic())


def capture_quiescence_sample(
    nodes: Sequence[NodeSnapshot],
    queues: Sequence[QueueSnapshot],
) -> QuiescenceSample:
    return QuiescenceSample(nodes, queues)


def reconcile_quiescence(  # noqa: C901
    first: QuiescenceSample,
    second: QuiescenceSample,
    *,
    minimum_interval: float = 5.0,
) -> bool:
    required_interval = max(5.0, minimum_interval)
    if (
        not math.isfinite(first.captured_at)
        or not math.isfinite(second.captured_at)
        or second.captured_at <= first.captured_at
        or second.captured_at - first.captured_at < required_interval
    ):
        return False
    if len(first.nodes) != 5 or len(second.nodes) != 5 or len(first.queues) != 5 or len(second.queues) != 5:
        return False
    first_nodes = {item.node: item for item in first.nodes}
    second_nodes = {item.node: item for item in second.nodes}
    first_queues = {item.queue: item for item in first.queues}
    second_queues = {item.queue: item for item in second.queues}
    if len(first_nodes) != 5 or len(second_nodes) != 5 or len(first_queues) != 5 or len(second_queues) != 5:
        return False
    if set(first_nodes) != set(second_nodes) or set(first_queues) != set(second_queues):
        return False
    if {item.queue for item in first.nodes} != set(first_queues):
        return False
    if {item.queue for item in second.nodes} != set(second_queues):
        return False
    for node in first_nodes.values():
        if (
            not node.cancellation_ack
            or node.active_queues
            or node.active != 0
            or node.reserved != 0
            or node.scheduled != 0
        ):
            return False
    for queue, snapshot in first_queues.items():
        if snapshot.unacknowledged != 0 or snapshot.native_delay != 0:
            return False
        if second_queues[queue] != snapshot:
            return False
    return all(second_nodes[node] == snapshot for node, snapshot in first_nodes.items())


def decide_authority_recovery(journal: Journal, *, target_processing: bool) -> str:
    if not isinstance(journal, Journal):
        raise D2Error('Authority recovery must be derived from the Journal')
    try:
        journal.path.stat()
    except FileNotFoundError:
        persisted = journal
    else:
        persisted = Journal.load(
            journal.path,
            expected_identity=journal.identity,
            secrets=journal._secrets,
        )
    decision = persisted.recovery_decision
    if decision == 'target-only':
        return decision
    if target_processing:
        raise D2Error('Target processing is forbidden before authority commit')
    return decision


class Journal:
    schema = 2

    def __init__(
        self,
        path: Path,
        identity: JournalIdentity | Mapping[str, object],
        *,
        secrets: Sequence[str] = (),
    ) -> None:
        self.path = path
        self.identity = identity if isinstance(identity, JournalIdentity) else JournalIdentity.from_mapping(identity)
        self._secrets = tuple(secrets)
        self._data: dict[str, object] = {
            'schema': self.schema,
            'phase': Phase.PRECHECK.value,
            'authority_state': AuthorityState.PRE_COMMIT.value,
            'recovery_decision': 'abort-old-source',
            'identity': self.identity.to_mapping(),
            'actions': [],
        }

    @property
    def data(self) -> dict[str, object]:
        return deepcopy(self._data)

    @property
    def authority_state(self) -> AuthorityState:
        try:
            return AuthorityState(self._data['authority_state'])
        except (KeyError, TypeError, ValueError) as error:
            raise D2Error('D2 journal authority state is malformed') from error

    @property
    def recovery_decision(self) -> str:
        state = self.authority_state
        decision = self._data.get('recovery_decision')
        expected = 'target-only' if state is AuthorityState.COMMITTED else 'abort-old-source'
        if decision != expected:
            raise D2Error('D2 journal recovery decision is inconsistent with authority state')
        return expected

    @classmethod
    def load_or_new(
        cls,
        path: Path,
        identity: JournalIdentity | Mapping[str, object],
        *,
        phase: Phase = Phase.PRECHECK,
        secrets: Sequence[str] = (),
    ) -> Journal:
        try:
            path.stat()
        except FileNotFoundError:
            if phase is not Phase.PRECHECK:
                raise D2Error('A missing journal may only start at PRECHECK') from None
            return cls(path, identity, secrets=secrets)
        return cls.load(path, expected_identity=identity, secrets=secrets)

    @classmethod
    def load(  # noqa: C901
        cls,
        path: Path,
        *,
        expected_identity: JournalIdentity | Mapping[str, object] | None = None,
        secrets: Sequence[str] = (),
    ) -> Journal:
        payload = _read_json_secure(path)
        if set(payload) != {'schema', 'phase', 'authority_state', 'recovery_decision', 'identity', 'actions'}:
            raise D2Error('D2 journal fields are not exact')
        if payload.get('schema') != cls.schema:
            raise D2Error('D2 journal schema is unsupported')
        phase_value = payload.get('phase')
        try:
            phase = Phase(phase_value)
        except (TypeError, ValueError) as error:
            raise D2Error('D2 journal phase is malformed') from error
        identity_payload = payload.get('identity')
        if not isinstance(identity_payload, Mapping):
            raise D2Error('D2 journal identity is malformed')
        identity = JournalIdentity.from_mapping(identity_payload)
        if expected_identity is not None:
            expected = (
                expected_identity
                if isinstance(expected_identity, JournalIdentity)
                else JournalIdentity.from_mapping(expected_identity)
            )
            if identity != expected:
                raise D2Error('D2 journal identity does not match live state')
        actions = payload.get('actions')
        if not isinstance(actions, list):
            raise D2Error('D2 journal actions are malformed')
        last_action: str | None = None
        previous_completed_at: int | float | None = None
        for index, action in enumerate(actions):
            if not isinstance(action, Mapping) or not isinstance(action.get('intent'), str):
                raise D2Error('D2 journal action is malformed')
            name = action['intent']
            if name not in _ACTION_PHASES or (_ACTION_ORDER.index(name) != index):
                raise D2Error('D2 journal action name or order is unsupported')
            complete = action.get('complete')
            if not isinstance(complete, bool) or (not complete and index != len(actions) - 1):
                raise D2Error('D2 journal contains an unreconciled action before its end')
            expected_keys = (
                {'intent', 'complete', 'timestamp', 'result', 'completed_at'}
                if complete
                else {'intent', 'complete', 'timestamp'}
            )
            if set(action) != expected_keys:
                raise D2Error('D2 journal action fields are not exact')
            timestamp = action.get('timestamp')
            if type(timestamp) not in {int, float} or not math.isfinite(timestamp):
                raise D2Error('D2 journal action timestamp is malformed')
            if previous_completed_at is not None and timestamp < previous_completed_at:
                raise D2Error('D2 journal action timestamps are out of order')
            if complete:
                completed_at = action.get('completed_at')
                if (
                    type(completed_at) not in {int, float}
                    or not math.isfinite(completed_at)
                    or completed_at < timestamp
                ):
                    raise D2Error('D2 journal action completion timestamp is malformed')
                result = action.get('result')
                if not isinstance(result, Mapping):
                    raise D2Error('D2 journal completion evidence is malformed')
                if _sanitize_action_evidence(name, result, secrets=secrets) != result:
                    raise D2Error('D2 journal completion evidence is not redacted or exact')
                previous_completed_at = completed_at
            last_action = name
        if phase is Phase.PRECHECK:
            if actions:
                raise D2Error('D2 journal phase is inconsistent with actions')
        elif phase is Phase.COMPLETE:
            if last_action != Action.OLD_ASYNC_RESTORE.value or not actions[-1].get('complete'):
                raise D2Error('D2 journal complete phase lacks completed restore action')
        else:
            expected_intent, expected_complete = next(
                ((intent, complete) for intent, complete in _ACTION_PHASES.values() if phase in {intent, complete}),
                (None, None),
            )
            expected_name = next((name for name, pair in _ACTION_PHASES.items() if phase in pair), None)
            if expected_name != last_action:
                raise D2Error('D2 journal phase is inconsistent with actions')
            action_complete = bool(actions[-1].get('complete'))
            if (phase is expected_intent and action_complete) or (phase is expected_complete and not action_complete):
                raise D2Error('D2 journal phase does not exactly match action completion')
        authority_state = payload.get('authority_state')
        expected_state = (
            AuthorityState.COMMITTED
            if phase
            in {
                Phase.TARGET_AUTHORITY_COMMITTED,
                Phase.OLD_ASYNC_RESTORE_INTENT,
                Phase.OLD_ASYNC_RESTORED,
                Phase.COMPLETE,
            }
            or any(
                action.get('intent') == Action.TARGET_AUTHORITY_COMMIT.value and action.get('complete') is True
                for action in actions
            )
            else AuthorityState.PRE_COMMIT
        )
        if authority_state != expected_state.value:
            raise D2Error('D2 journal authority state is inconsistent with actions')
        recovery_decision = payload.get('recovery_decision')
        expected_decision = 'target-only' if expected_state is AuthorityState.COMMITTED else 'abort-old-source'
        if recovery_decision != expected_decision:
            raise D2Error('D2 journal recovery decision is inconsistent with authority state')
        journal = cls(path, identity, secrets=secrets)
        journal._data = dict(payload)
        return journal

    def write(self) -> None:
        atomic_write_json(self.path, self._data)

    def set_phase(self, phase: Phase) -> None:
        try:
            current = Phase(self._data.get('phase'))
        except (TypeError, ValueError) as error:
            raise D2Error('D2 journal phase is malformed') from error
        if _LEGAL_NEXT_PHASE.get(current) is not phase:
            raise D2Error(f'illegal D2 phase transition {current.value} -> {phase.value}')
        actions = self._data.get('actions')
        if not isinstance(actions, list):
            raise D2Error('D2 journal actions are malformed')
        action_name = next((name for name, pair in _ACTION_PHASES.items() if phase in pair), None)
        if phase is Phase.COMPLETE:
            if len(actions) != len(_ACTION_ORDER) or any(
                not isinstance(action, Mapping)
                or action.get('intent') != _ACTION_ORDER[index]
                or action.get('complete') is not True
                for index, action in enumerate(actions)
            ):
                raise D2Error('D2 final phase requires all completed actions')
        else:
            if (
                action_name is None
                or not actions
                or not isinstance(actions[-1], Mapping)
                or actions[-1].get('intent') != action_name
            ):
                raise D2Error('D2 phase requires its matching action')
            if phase in {pair[1] for pair in _ACTION_PHASES.values()} and actions[-1].get('complete') is not True:
                raise D2Error('D2 complete phase requires completed action')
        self._data['phase'] = phase.value
        if phase in {
            Phase.TARGET_AUTHORITY_COMMITTED,
            Phase.OLD_ASYNC_RESTORE_INTENT,
            Phase.OLD_ASYNC_RESTORED,
            Phase.COMPLETE,
        }:
            self._data['authority_state'] = AuthorityState.COMMITTED.value
            self._data['recovery_decision'] = 'target-only'
        self.write()

    def record_intent(self, name: str) -> None:
        if name not in _ACTION_PHASES:
            raise D2Error('Action intent name is unsupported')
        actions = self._data.get('actions')
        if not isinstance(actions, list) or (
            actions and (not isinstance(actions[-1], Mapping) or not actions[-1].get('complete', False))
        ):
            raise D2Error('An incomplete action intent must be reconciled first')
        if len(actions) != _ACTION_ORDER.index(name):
            raise D2Error('Action intent is out of phase order')
        current = Phase(self._data['phase'])
        expected_intent, _ = _ACTION_PHASES[name]
        if _LEGAL_NEXT_PHASE.get(current) is not expected_intent:
            raise D2Error('Action intent phase is inconsistent with journal phase')
        actions.append({'intent': name, 'complete': False, 'timestamp': time.time()})
        self._data['phase'] = expected_intent.value
        self.write()

    def record_completion(self, name: str, result: Mapping[str, object]) -> None:
        actions = self._data.get('actions')
        if (
            not isinstance(actions, list)
            or not actions
            or not isinstance(actions[-1], dict)
            or actions[-1].get('intent') != name
            or actions[-1].get('complete') is not False
        ):
            raise D2Error('No matching incomplete journaled action intent')
        sanitized = _sanitize_action_evidence(name, result, secrets=self._secrets)
        actions[-1]['result'] = sanitized
        actions[-1]['complete'] = True
        actions[-1]['completed_at'] = time.time()
        _, complete_phase = _ACTION_PHASES[name]
        self._data['phase'] = complete_phase.value
        if name == Action.TARGET_AUTHORITY_COMMIT.value:
            self._data['authority_state'] = AuthorityState.COMMITTED.value
            self._data['recovery_decision'] = 'target-only'
        self.write()


@dataclass(frozen=True)
class TargetIdentity:
    attempt_id: str
    rabbit_volume: str
    beat_volume: str
    checkpoint: str = 'd2-storage-bootstrap'

    def __post_init__(self) -> None:
        rabbit_volume, beat_volume = volume_names(self.attempt_id)
        if (self.rabbit_volume, self.beat_volume) != (rabbit_volume, beat_volume):
            raise D2Error('Target volume names do not match attempt identity')

    def labels(self) -> dict[str, str]:
        return {
            'd2.checkpoint': self.checkpoint,
            'd2.attempt_id': self.attempt_id,
            'd2.source_class': 'rabbit-bind-beat-container-file',
        }


def volume_names(attempt_id: str) -> tuple[str, str]:
    if re.fullmatch(r'[0-9a-f]{8,32}', attempt_id) is None:
        raise D2Error('Attempt id must be lowercase hexadecimal')
    return f'engram_d2_rabbitmq_{attempt_id}', f'engram_d2_beat_{attempt_id}'


def validate_target_labels(labels: Mapping[str, object], identity: TargetIdentity) -> None:
    expected = identity.labels()
    if dict(labels) != expected or 'd2.lifecycle' in labels:
        raise D2Error('Target volume labels are immutable and journal-owned')
