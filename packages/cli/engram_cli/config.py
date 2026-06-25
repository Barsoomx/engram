from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LocalPaths:
    root: Path
    config: Path
    credentials: Path
    hooks_dir: Path

    def hook_manifest(self, runtime: str) -> Path:
        return self.hooks_dir / f'{runtime}.json'


def resolve_config_dir(value: str | None) -> Path:
    if value:
        return Path(value).expanduser()

    env_home = os.environ.get('ENGRAM_HOME')
    if env_home:
        return Path(env_home).expanduser()

    return Path.home() / '.engram'


def local_paths(config_dir: str | None) -> LocalPaths:
    root = resolve_config_dir(config_dir)

    return LocalPaths(
        root=root,
        config=root / 'config.json',
        credentials=root / 'credentials.json',
        hooks_dir=root / 'hooks',
    )


def read_json(path: Path) -> dict[str, object]:
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise ValueError('JSON document must be an object')

    return data


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n', encoding='utf-8')


def write_secret_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write('\n')
    finally:
        path.chmod(0o600)


def remove_if_exists(path: Path) -> bool:
    try:
        path.unlink()
    except FileNotFoundError:
        return False

    return True


def credential_fingerprint(raw_key: str) -> str:
    digest = hashlib.sha256(raw_key.encode()).hexdigest()

    return f'{raw_key[:12]}...{digest[-12:]}'


def as_string(value: object) -> str:
    if isinstance(value, str):
        return value

    return ''


def as_string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []

    return [item for item in value if isinstance(item, str)]
