from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


SCAN_ROOT_FILES: tuple[str, ...] = (
    'README.md',
    'CONTRIBUTING.md',
    'SECURITY.md',
    'CODEOWNERS',
)

SCAN_DIRECTORIES: tuple[str, ...] = (
    '.github',
    'apps',
    'deploy',
    'docs',
    'packages',
    'plugin-repository',
    'scripts',
    'tests',
)

SKIP_DIRECTORIES: frozenset[str] = frozenset({
    '.git',
    '.idea',
    '__pycache__',
})

INCOMPLETE_MARKERS: tuple[str, ...] = (
    'T' + 'ODO',
    'T' + 'BD',
    'F' + 'IXME',
    'PLACE' + 'HOLDER',
)

PRIVATE_REFERENCE_TERMS: tuple[str, ...] = (
    'alt' + 'yn',
    '\u0430\u043b\u0442\u044b\u043d',
)

PRIVATE_REFERENCE_ALLOWED_PATHS: frozenset[str] = frozenset({
    'docs/reference-gates.md',
})


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    code: str
    message: str


def scan_text(path: str, text: str) -> list[Finding]:
    findings: list[Finding] = []
    allow_private_reference = path in PRIVATE_REFERENCE_ALLOWED_PATHS

    for line_number, line in enumerate(text.splitlines(), start=1):
        for marker in INCOMPLETE_MARKERS:
            if marker in line:
                findings.append(
                    Finding(
                        path=path,
                        line=line_number,
                        code='incomplete-marker',
                        message=f'incomplete work marker {marker!r}',
                    ),
                )

        lowered = line.casefold()
        if not allow_private_reference:
            for term in PRIVATE_REFERENCE_TERMS:
                if term in lowered:
                    findings.append(
                        Finding(
                            path=path,
                            line=line_number,
                            code='private-reference-term',
                            message='private reference term outside allowlist',
                        ),
                    )

    return findings


def scan_root(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in iter_scan_paths(root):
        text = read_text_or_none(path)
        if text is None:
            continue

        relative_path = path.relative_to(root).as_posix()
        findings.extend(scan_text(relative_path, text))

    return findings


def iter_scan_paths(root: Path) -> Iterable[Path]:
    for file_name in SCAN_ROOT_FILES:
        path = root / file_name
        if path.is_file():
            yield path

    for directory_name in SCAN_DIRECTORIES:
        directory = root / directory_name
        if not directory.is_dir():
            continue

        for path in sorted(directory.rglob('*')):
            if path.is_file() and should_scan_path(path):
                yield path


def should_scan_path(path: Path) -> bool:
    return not any(part in SKIP_DIRECTORIES for part in path.parts)


def read_text_or_none(path: Path) -> str | None:
    try:
        return path.read_text(encoding='utf-8')
    except UnicodeDecodeError:
        return None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default='.')
    args = parser.parse_args(argv)

    findings = scan_root(Path(args.root))
    for finding in findings:
        print(
            f'{finding.path}:{finding.line}: '
            f'{finding.code}: {finding.message}',
        )

    return 1 if findings else 0


if __name__ == '__main__':
    raise SystemExit(main())
