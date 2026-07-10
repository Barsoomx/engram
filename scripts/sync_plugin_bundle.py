from __future__ import annotations

import argparse
import shutil
from collections.abc import Sequence
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = ROOT / 'packages/cli/engram_cli'
BUNDLE_DIRS = (
    ROOT / 'packages/claude-plugin/hooks/engram_cli',
    ROOT / 'packages/codex-plugin/hooks/engram_cli',
)


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
    for bundle_dir in BUNDLE_DIRS:
        if bundle_dir.exists():
            shutil.rmtree(bundle_dir)

        bundle_dir.mkdir(parents=True)
        for name in runtime_module_names():
            shutil.copyfile(SOURCE_DIR / name, bundle_dir / name)


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
    problems: list[str] = []
    for bundle_dir in BUNDLE_DIRS:
        bundled = (
            {path.name for path in bundle_dir.iterdir() if path.is_file()}
            if bundle_dir.exists()
            else set()
        )
        bundle_name = bundle_dir.relative_to(ROOT)
        for name in sorted(expected - bundled):
            problems.append(f'{bundle_name}: missing bundled file {name}')

        for name in sorted(bundled - expected):
            problems.append(f'{bundle_name}: unexpected bundled file {name}')

        for name in sorted(expected & bundled):
            if (SOURCE_DIR / name).read_bytes() != (bundle_dir / name).read_bytes():
                problems.append(f'{bundle_name}: out-of-date bundled file {name}')

    return problems


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='sync_plugin_bundle')
    parser.add_argument('--check', action='store_true')

    return parser


def progress(message: str) -> None:
    print(f'[sync-plugin-bundle] {message}', flush=True)


if __name__ == '__main__':
    raise SystemExit(main())
