from __future__ import annotations

import argparse
import shutil
from collections.abc import Sequence
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = ROOT / 'packages/cli/engram_cli'
BUNDLE_DIR = ROOT / 'packages/claude-plugin/hooks/engram_cli'


def runtime_module_names() -> tuple[str, ...]:
    return tuple(
        sorted(
            path.name
            for path in SOURCE_DIR.iterdir()
            if path.is_file()
            and path.suffix == '.py'
            and not path.name.endswith('_tests.py')
        )
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.check:
        return check_bundle()

    sync_bundle()
    progress('bundle synced')

    return 0


def sync_bundle() -> None:
    if BUNDLE_DIR.exists():
        shutil.rmtree(BUNDLE_DIR)

    BUNDLE_DIR.mkdir(parents=True)
    for name in runtime_module_names():
        shutil.copyfile(SOURCE_DIR / name, BUNDLE_DIR / name)


def check_bundle() -> int:
    drift = bundle_drift()
    if drift:
        for problem in drift:
            progress(f'drift: {problem}')

        return 1

    progress('bundle is in sync')

    return 0


def bundle_drift() -> list[str]:
    expected = set(runtime_module_names())
    bundled = (
        {path.name for path in BUNDLE_DIR.iterdir() if path.is_file()}
        if BUNDLE_DIR.exists()
        else set()
    )
    problems: list[str] = []
    for name in sorted(expected - bundled):
        problems.append(f'missing bundled file {name}')

    for name in sorted(bundled - expected):
        problems.append(f'unexpected bundled file {name}')

    for name in sorted(expected & bundled):
        if (SOURCE_DIR / name).read_bytes() != (BUNDLE_DIR / name).read_bytes():
            problems.append(f'out-of-date bundled file {name}')

    return problems


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='sync_plugin_bundle')
    parser.add_argument('--check', action='store_true')

    return parser


def progress(message: str) -> None:
    print(f'[sync-plugin-bundle] {message}', flush=True)


if __name__ == '__main__':
    raise SystemExit(main())
