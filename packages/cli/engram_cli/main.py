from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import TextIO

from engram_cli.commands import run_connect, run_disconnect, run_doctor, run_hook, run_search
from engram_cli.http import Transport


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin: TextIO | None = None,
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
    if args.command == 'hook':
        return run_hook(args, stdin or sys.stdin, output, errors, transport)
    if args.command == 'search':
        return run_search(args, output, errors, transport)

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

    hook = subparsers.add_parser('hook')
    hook_subparsers = hook.add_subparsers(dest='hook_command')
    for command in ('post-tool-use', 'session-start', 'error', 'decision'):
        hook_command = hook_subparsers.add_parser(command)
        hook_command.add_argument('--agent', choices=('codex', 'claude-code', 'claude_code'))
        hook_command.add_argument('--config-dir')
        hook_command.add_argument('--response-format', choices=('server', 'codex', 'claude-code'), default='server')

    search = subparsers.add_parser('search')
    search.add_argument('--query', default='')
    search.add_argument('--file-path', action='append', default=[])
    search.add_argument('--symbol', action='append', default=[])
    search.add_argument('--limit', type=int, default=5)
    search.add_argument('--config-dir')
    search.add_argument('--json', action='store_true', dest='as_json')

    return parser
