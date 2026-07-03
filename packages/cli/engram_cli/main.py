from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import TextIO

from engram_cli.commands import (
    run_connect,
    run_disconnect,
    run_doctor,
    run_hook,
    run_install,
    run_mcp_install,
    run_memory_link,
    run_memory_links,
    run_memory_version,
    run_observations,
    run_search,
)
from engram_cli.http import Transport
from engram_cli.mcp_server import run_mcp_serve


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
    if args.command == "connect":
        return run_connect(args, output, errors, transport)
    if args.command == "install":
        return run_install(args, output, errors, transport)
    if args.command == "doctor":
        return run_doctor(args, output, errors, transport)
    if args.command == "disconnect":
        return run_disconnect(args, output, errors)
    if args.command == "mcp-install":
        return run_mcp_install(args, output, errors, transport)
    if args.command == "mcp":
        if args.mcp_command == "install":
            return run_mcp_install(args, output, errors, transport)
        if args.mcp_command == "serve":
            return run_mcp_serve(args, stdin or sys.stdin, output, transport)
    if args.command == "hook":
        return run_hook(args, stdin or sys.stdin, output, errors, transport)
    if args.command == "search":
        return run_search(args, output, errors, transport)
    if args.command == "observations":
        return run_observations(args, output, errors, transport)
    if args.command == "memory":
        if args.memory_command == "version":
            return run_memory_version(args, output, errors, transport)
        if args.memory_command == "link":
            return run_memory_link(args, output, errors, transport)
        if args.memory_command == "links":
            return run_memory_links(args, output, errors, transport)

    parser.print_help(file=errors)

    return 1


def console_main() -> None:
    raise SystemExit(main())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="engram")
    subparsers = parser.add_subparsers(dest="command")

    connect = subparsers.add_parser("connect")
    connect.add_argument("--server")
    connect.add_argument("--api-key")
    connect.add_argument("--project")
    connect.add_argument("--team")
    connect.add_argument(
        "--agent",
        choices=("codex", "claude-code", "claude_code", "both"),
        default="both",
    )
    connect.add_argument("--agent-version", default="")
    connect.add_argument("--config-dir")

    install = subparsers.add_parser("install")
    install.add_argument("--server")
    install.add_argument("--api-key")
    install.add_argument("--project")
    install.add_argument("--team")
    install.add_argument(
        "--agent",
        choices=("codex", "claude-code", "claude_code", "both"),
        default="claude-code",
    )
    install.add_argument("--agent-version", default="")
    install.add_argument("--config-dir")
    install.add_argument("--marketplace-source", default="Barsoomx/engram")
    install.add_argument("--marketplace-name", default="engram-marketplace")
    install.add_argument("--plugin-name", default="engram")
    install.add_argument("--claude-bin", default="claude")
    install.add_argument("--skip-plugin-install", action="store_true")

    doctor = subparsers.add_parser("doctor")
    doctor.add_argument("--config-dir")

    disconnect = subparsers.add_parser("disconnect")
    disconnect.add_argument("--config-dir")

    mcp_install = subparsers.add_parser("mcp-install")
    mcp_install.add_argument(
        "--agent",
        choices=("claude_code", "claude_desktop", "both"),
        default="both",
    )
    mcp_install.add_argument("--config-dir")
    mcp_install.add_argument("--claude-code-config")
    mcp_install.add_argument("--claude-desktop-config")

    mcp = subparsers.add_parser("mcp")
    mcp_subparsers = mcp.add_subparsers(dest="mcp_command")
    mcp_install_group = mcp_subparsers.add_parser("install")
    mcp_install_group.add_argument(
        "--agent",
        choices=("claude_code", "claude_desktop", "both"),
        default="both",
    )
    mcp_install_group.add_argument("--config-dir")
    mcp_install_group.add_argument("--claude-code-config")
    mcp_install_group.add_argument("--claude-desktop-config")
    mcp_serve = mcp_subparsers.add_parser("serve")
    mcp_serve.add_argument("--config-dir")

    hook = subparsers.add_parser("hook")
    hook_subparsers = hook.add_subparsers(dest="hook_command")
    for command in ("post-tool-use", "session-start", "error", "decision", "session-end", "user-prompt-submit"):
        hook_command = hook_subparsers.add_parser(command)
        hook_command.add_argument(
            "--agent", choices=("codex", "claude-code", "claude_code")
        )
        hook_command.add_argument("--config-dir")
        hook_command.add_argument(
            "--response-format",
            choices=("server", "codex", "claude-code"),
            default="server",
        )

    search = subparsers.add_parser("search")
    search.add_argument("--query", default="")
    search.add_argument("--file-path", action="append", default=[])
    search.add_argument("--symbol", action="append", default=[])
    search.add_argument("--limit", type=int, default=5)
    search.add_argument("--config-dir")
    search.add_argument("--json", action="store_true", dest="as_json")
    search.add_argument("--project", default="")

    memory = subparsers.add_parser("memory")
    memory_subparsers = memory.add_subparsers(dest="memory_command")

    memory_version = memory_subparsers.add_parser("version")
    memory_version.add_argument("memory_id")
    memory_version.add_argument("--body", required=True)
    memory_version.add_argument("--reason", default="")
    memory_version.add_argument("--request-id", dest="request_id", default="")
    memory_version.add_argument("--config-dir")
    memory_version.add_argument("--project", default="")

    memory_link = memory_subparsers.add_parser("link")
    memory_link.add_argument("memory_id")
    memory_link.add_argument(
        "--link-type",
        dest="link_type",
        required=True,
        choices=("file", "symbol", "commit", "issue"),
    )
    memory_link.add_argument("--target", required=True)
    memory_link.add_argument("--label", default="")
    memory_link.add_argument("--request-id", dest="request_id", default="")
    memory_link.add_argument("--config-dir")
    memory_link.add_argument("--project", default="")

    memory_links = memory_subparsers.add_parser("links")
    memory_links.add_argument("memory_id")
    memory_links.add_argument("--config-dir")
    memory_links.add_argument("--project", default="")

    observations = subparsers.add_parser("observations")
    observations.add_argument("--limit", type=int, default=20)
    observations.add_argument("--config-dir")
    observations.add_argument("--project", default="")

    return parser
