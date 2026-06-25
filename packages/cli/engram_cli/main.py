from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import TextIO

from engram_cli.commands import run_connect, run_disconnect, run_doctor
from engram_cli.http import Transport


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    transport: Transport | None = None,
) -> int:
    output = stdout or sys.stdout
    errors = stderr or sys.stderr
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as error:
        return int(error.code)
    if args.command == 'connect':
        return run_connect(args, output, errors, transport)
    if args.command == 'doctor':
        return run_doctor(args, output, errors, transport)
    if args.command == 'disconnect':
        return run_disconnect(args, output, errors)

    parser.print_help(file=errors)

    return 1


def console_main() -> None:
    raise SystemExit(main())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='engram')
    subparsers = parser.add_subparsers(dest='command')

    connect = subparsers.add_parser('connect')
    connect.add_argument('--server')
    connect.add_argument('--api-key')
    connect.add_argument('--project')
    connect.add_argument('--team')
    connect.add_argument('--agent', choices=('codex', 'claude-code', 'claude_code', 'both'), default='both')
    connect.add_argument('--agent-version', default='')
    connect.add_argument('--config-dir')

    doctor = subparsers.add_parser('doctor')
    doctor.add_argument('--config-dir')

    disconnect = subparsers.add_parser('disconnect')
    disconnect.add_argument('--config-dir')

    return parser
