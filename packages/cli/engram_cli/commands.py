from __future__ import annotations

import os
import hashlib
import json
import shutil
import subprocess
import sys
import uuid
from argparse import Namespace
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO
from urllib.parse import urlparse

from engram_cli.config import (
    as_string,
    as_string_list,
    credential_fingerprint,
    default_claude_code_config_path,
    default_claude_desktop_config_path,
    local_paths,
    read_json,
    remove_if_exists,
    write_json,
    write_secret_json,
)
from engram_cli.http import (
    Transport,
    admin_get,
    admin_post,
    get_health,
    get_json,
    post_dry_run,
    post_json,
    post_login,
    probe_health,
    urllib_transport,
)


class CliError(Exception):
    def __init__(self, code: str, detail: str, remediation: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.remediation = remediation


PromptFn = Callable[[str], str]
Runner = Callable[[list[str]], tuple[int, str, str]]

DEFAULT_SERVER_URL = "http://localhost:8000"
WIZARD_API_KEY_CAPABILITIES = (
    "memories:read",
    "observations:write",
    "search:query",
)
MAX_LOGIN_RETRIES = 3
MAX_SERVER_RETRIES = 3
PLUGIN_COMMAND_TIMEOUT_SECONDS = 120


ERROR_REMEDIATION: dict[str, str] = {
    "missing_server_url": "Pass --server with the Engram server URL.",
    "missing_api_key": "Pass --api-key with a scoped Engram API key.",
    "missing_project": "Pass --project with the Engram project id.",
    "missing_config": "Run `engram connect` first.",
    "missing_credential": "Run `engram connect` again to write credentials.",
    "missing_hook_config": "Run `engram connect` again to write hook manifests.",
    "server_unavailable": "Check the server URL and /-/healthz/ endpoint.",
    "http_error": "Check the server response and retry.",
    "invalid_response": "Upgrade the CLI or server so response schemas match.",
    "invalid_key": "Use a valid scoped Engram API key.",
    "expired_key": "Rotate the API key and run `engram connect` again.",
    "missing_capability": "Use a key with observations:write for hook dry-run.",
    "project_scope_denied": "Use a key scoped to the requested project.",
    "team_scope_denied": "Use a key scoped to the requested team.",
    "invalid_credentials": "Check the username and password and try again.",
    "wizard_aborted": "Connect wizard was cancelled.",
    "no_organizations": "Ask an admin to add your account to an organization.",
    "no_projects": "Ask an admin to create a project in this organization.",
    "api_key_issue_failed": "Check server logs and key capabilities.",
    "missing_mcp_target": "Pass --claude-code-config or --claude-desktop-config.",
    "invalid_agent_target": "Use claude_code, claude_desktop, or both.",
    "claude_cli_not_found": "Install the Claude Code CLI and ensure 'claude' is on PATH.",
    "plugin_install_failed": "Check 'claude plugin' output and marketplace source.",
    "python_runtime_missing": "Install python3 >= 3.12 so bundled hooks can run.",
}


def run_connect(
    args: Namespace,
    stdout: TextIO,
    stderr: TextIO,
    transport: Transport | None = None,
    *,
    prompt: PromptFn | None = None,
    interactive: bool | None = None,
) -> int:
    active_transport = transport or urllib_transport
    is_interactive = interactive if interactive is not None else stdin_is_tty()
    if not is_interactive or all_flags_present(args):
        return run_connect_flags(args, stdout, stderr, active_transport)

    prompt_fn = prompt if prompt is not None else builtin_input

    return run_connect_wizard(
        args, stdout, stderr, active_transport, prompt_fn
    )


def all_flags_present(args: Namespace) -> bool:
    return bool(args.server and args.api_key and args.project)


def stdin_is_tty() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def builtin_input(message: str) -> str:
    return input(message)


def run_connect_flags(
    args: Namespace,
    stdout: TextIO,
    stderr: TextIO,
    transport: Transport,
) -> int:
    api_key_for_redaction = args.api_key or ""
    try:
        server_url = normalize_server_url(args.server)
        api_key = required_value(args.api_key, "missing_api_key", "API key is required")
        api_key_for_redaction = api_key
        project_id = (args.project or "").strip()
        team_id = args.team or ""
        agent_version = args.agent_version or ""
        runtimes = normalize_runtimes(args.agent)
        dry_run_results = [
            require_dry_run_ok(
                transport,
                server_url=server_url,
                api_key=api_key,
                project_id=project_id,
                team_id=team_id,
                agent_runtime=runtime,
                agent_version=agent_version,
            )
            for runtime in runtimes
        ]
        paths = local_paths(args.config_dir)
        fingerprint = credential_fingerprint(api_key)
        write_local_state(
            paths_root=paths.root,
            server_url=server_url,
            project_id=project_id,
            team_id=team_id,
            runtimes=runtimes,
            agent_version=agent_version,
            api_key=api_key,
            fingerprint=fingerprint,
            dry_run_result=dry_run_results[0],
        )
        scope = dry_run_results[0].get("scope", {})
        stdout.write(f"connected Engram CLI to {server_url}\n")
        stdout.write(f"project: {project_id}\n")
        if team_id:
            stdout.write(f"team: {team_id}\n")
        stdout.write(f"runtimes: {', '.join(runtimes)}\n")
        stdout.write(f"credential: {fingerprint}\n")
        if isinstance(scope, dict):
            organization_id = scope.get("organization_id")
            if organization_id:
                stdout.write(f"organization: {organization_id}\n")
            capabilities = scope.get("capabilities")
            if isinstance(capabilities, list):
                stdout.write(
                    f"capabilities: {', '.join(str(item) for item in capabilities)}\n"
                )

        return 0
    except CliError as error:
        emit_error(stderr, error, api_key_for_redaction)

        return 1


def run_connect_wizard(
    args: Namespace,
    stdout: TextIO,
    stderr: TextIO,
    transport: Transport,
    prompt: PromptFn,
) -> int:
    try:
        team_id = args.team or ""
        agent_version = args.agent_version or ""
        runtimes = normalize_runtimes(args.agent)
        server_url = prompt_server_url(prompt, stderr, transport)
        drf_token = prompt_login(prompt, stdout, stderr, transport, server_url)
        organizations = fetch_organizations(transport, server_url, drf_token)
        organization_id = prompt_organization(prompt, stdout, organizations)
        projects = fetch_projects(
            transport, server_url, drf_token, organization_id
        )
        project_id = prompt_project(prompt, stdout, projects)
        api_key_name = prompt_api_key_name(prompt)
        api_key = issue_wizard_api_key(
            transport,
            server_url=server_url,
            drf_token=drf_token,
            organization_id=organization_id,
            api_key_name=api_key_name,
        )
        paths = local_paths(args.config_dir)
        fingerprint = credential_fingerprint(api_key)
        write_local_state(
            paths_root=paths.root,
            server_url=server_url,
            project_id=project_id,
            team_id=team_id,
            runtimes=runtimes,
            agent_version=agent_version,
            api_key=api_key,
            fingerprint=fingerprint,
            dry_run_result={
                "resolved_actor": {},
                "scope": {"organization_id": organization_id},
            },
            organization_id=organization_id,
        )
        stdout.write(f"connected Engram CLI to {server_url}\n")
        stdout.write(f"organization: {organization_id}\n")
        stdout.write(f"project: {project_id}\n")
        stdout.write(f"runtimes: {', '.join(runtimes)}\n")
        stdout.write(f"credential: {fingerprint}\n")

        return 0
    except CliError as error:
        emit_error(stderr, error, "")

        return 1


def prompt_server_url(
    prompt: PromptFn, stderr: TextIO, transport: Transport
) -> str:
    for _ in range(MAX_SERVER_RETRIES):
        raw = prompt(f"Server URL [{DEFAULT_SERVER_URL}]: ").strip()
        if raw.lower() in {"quit", "exit"}:
            raise CliError(
                "wizard_aborted",
                "Connect wizard cancelled",
                remediation_for("wizard_aborted"),
            )
        candidate = raw or DEFAULT_SERVER_URL
        try:
            server_url = normalize_server_url(candidate)
        except CliError as error:
            stderr.write(f"{error.detail}\n")

            continue
        if probe_health(transport=transport, server_url=server_url):

            return server_url
        stderr.write(
            f"Could not reach {server_url}/-/healthz/. Try another URL.\n"
        )

    raise CliError(
        "server_unavailable",
        "Server health check failed after retries",
        remediation_for("server_unavailable"),
    )


def prompt_login(
    prompt: PromptFn,
    stdout: TextIO,
    stderr: TextIO,
    transport: Transport,
    server_url: str,
) -> str:
    for _ in range(MAX_LOGIN_RETRIES):
        username = prompt("Username: ").strip()
        if username.lower() in {"quit", "exit"}:
            raise CliError(
                "wizard_aborted",
                "Connect wizard cancelled",
                remediation_for("wizard_aborted"),
            )
        password = prompt("Password: ").strip()
        status, body = post_login(
            transport=transport,
            server_url=server_url,
            username=username,
            password=password,
        )
        if status >= 200 and status < 300:
            token = as_string(body.get("token"))
            if token:

                return token
            raise CliError(
                "invalid_response",
                "Login response did not include a token",
                remediation_for("invalid_response"),
            )
        code = as_string(body.get("code")) or "invalid_credentials"
        detail = as_string(body.get("detail")) or remediation_for(code)
        stderr.write(f"Login failed: {detail}\n")

    raise CliError(
        "invalid_credentials",
        "Login failed after retries",
        remediation_for("invalid_credentials"),
    )


def fetch_organizations(
    transport: Transport, server_url: str, drf_token: str
) -> list[dict[str, object]]:
    status, body = admin_get(
        transport=transport,
        server_url=server_url,
        path="/v1/admin/organizations/",
        drf_token=drf_token,
    )
    if status < 200 or status >= 300:
        raise error_from_body(body, fallback="http_error")
    items = extract_results(body)
    if not items:
        raise CliError(
            "no_organizations",
            "Your account has no accessible organizations",
            remediation_for("no_organizations"),
        )

    return items


def prompt_organization(
    prompt: PromptFn, stdout: TextIO, organizations: list[dict[str, object]]
) -> str:
    stdout.write("Organizations:\n")
    for index, org in enumerate(organizations, start=1):
        stdout.write(f"  {index}. {org.get('name')} ({org.get('slug')})\n")
    selection = pick_from_list(prompt, organizations, "organization")

    return as_string(organizations[selection - 1].get("id"))


def fetch_projects(
    transport: Transport,
    server_url: str,
    drf_token: str,
    organization_id: str,
) -> list[dict[str, object]]:
    status, body = admin_get(
        transport=transport,
        server_url=server_url,
        path="/v1/admin/projects/",
        drf_token=drf_token,
        organization_id=organization_id,
    )
    if status < 200 or status >= 300:
        raise error_from_body(body, fallback="http_error")
    items = extract_results(body)
    if not items:
        raise CliError(
            "no_projects",
            "This organization has no projects",
            remediation_for("no_projects"),
        )

    return items


def prompt_project(
    prompt: PromptFn, stdout: TextIO, projects: list[dict[str, object]]
) -> str:
    stdout.write("Projects:\n")
    for index, project in enumerate(projects, start=1):
        stdout.write(f"  {index}. {project.get('name')} ({project.get('slug')})\n")
    selection = pick_from_list(prompt, projects, "project")

    return as_string(projects[selection - 1].get("id"))


def prompt_api_key_name(prompt: PromptFn) -> str:
    raw = prompt("API key name [engram-cli]: ").strip()

    return raw or "engram-cli"


def issue_wizard_api_key(
    transport: Transport,
    *,
    server_url: str,
    drf_token: str,
    organization_id: str,
    api_key_name: str,
) -> str:
    status, body = admin_post(
        transport=transport,
        server_url=server_url,
        path="/v1/admin/api-keys/",
        drf_token=drf_token,
        organization_id=organization_id,
        payload={
            "name": api_key_name,
            "capabilities": list(WIZARD_API_KEY_CAPABILITIES),
        },
    )
    if status < 200 or status >= 300:
        raise error_from_body(body, fallback="api_key_issue_failed")
    plaintext = as_string(body.get("plaintext"))
    if not plaintext:
        raise CliError(
            "api_key_issue_failed",
            "API key response did not include plaintext",
            remediation_for("api_key_issue_failed"),
        )

    return plaintext


def pick_from_list(
    prompt: PromptFn, items: list[dict[str, object]], label: str
) -> int:
    raw = prompt(f"Select {label} [1-{len(items)}]: ").strip()
    if raw.lower() in {"quit", "exit"}:
        raise CliError(
            "wizard_aborted",
            "Connect wizard cancelled",
            remediation_for("wizard_aborted"),
        )
    try:
        selection = int(raw)
    except ValueError as error:
        raise CliError(
            "invalid_response",
            f"Selection must be a number",
            remediation_for("invalid_response"),
        ) from error
    if selection < 1 or selection > len(items):
        raise CliError(
            "invalid_response",
            f"Selection must be between 1 and {len(items)}",
            remediation_for("invalid_response"),
        )

    return selection


def extract_results(body: dict[str, object]) -> list[dict[str, object]]:
    results = body.get("results")
    if isinstance(results, list):
        return [item for item in results if isinstance(item, dict)]
    if isinstance(body.get("count"), int) and not results:

        return []
    candidate = [
        item for item in body.values() if isinstance(item, list)
    ]
    for items in candidate:
        normalized = [item for item in items if isinstance(item, dict)]
        if normalized:

            return normalized

    return []


def run_doctor(
    args: Namespace,
    stdout: TextIO,
    stderr: TextIO,
    transport: Transport | None = None,
) -> int:
    checks: list[tuple[str, str, str]] = []
    api_key = ""
    try:
        paths = local_paths(args.config_dir)
        config = load_required_json(
            paths.config, "missing_config", "Engram config is missing"
        )
        checks.append(("ok", "config", "loaded"))
        credentials = load_required_json(
            paths.credentials, "missing_credential", "Engram credential is missing"
        )
        api_key = as_string(credentials.get("api_key"))
        if not api_key:
            raise CliError(
                "missing_credential",
                "Engram credential is missing",
                remediation_for("missing_credential"),
            )
        checks.append(("ok", "credential", "loaded"))
        runtimes = as_string_list(config.get("agent_runtimes"))
        if not runtimes:
            raise CliError(
                "missing_hook_config",
                "No hook manifests are configured",
                remediation_for("missing_hook_config"),
            )
        for runtime in runtimes:
            load_required_json(
                paths.hook_manifest(runtime),
                "missing_hook_config",
                f"Hook manifest for {runtime} is missing",
            )
        checks.append(("ok", "hook_config", ", ".join(runtimes)))
        active_transport = transport or urllib_transport
        server_url = normalize_server_url(as_string(config.get("server_url")))
        status, body = get_health(transport=active_transport, server_url=server_url)
        if status != 200 or body.get("status") != "ok":
            raise CliError(
                "server_unavailable",
                "Engram server health check failed",
                remediation_for("server_unavailable"),
            )
        checks.append(("ok", "server_health", server_url))
        for runtime in runtimes:
            require_dry_run_ok(
                active_transport,
                server_url=server_url,
                api_key=api_key,
                project_id=as_string(config.get("project_id")),
                team_id=as_string(config.get("team_id")),
                agent_runtime=runtime,
                agent_version=as_string(config.get("agent_version")),
            )
        checks.append(("ok", "dry_run", ", ".join(runtimes)))
    except CliError as error:
        for status, name, detail in checks:
            stdout.write(f"{status} {name}: {detail}\n")
        stdout.write(f"fail {error.code}: {redact_secret(error.detail, api_key)}\n")
        emit_error(stderr, error, api_key)

        return 1

    for status, name, detail in checks:
        stdout.write(f"{status} {name}: {detail}\n")
    stdout.write("All required checks passed.\n")

    return 0


def subprocess_runner(cmd: list[str]) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=PLUGIN_COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        note = f"command timed out after {PLUGIN_COMMAND_TIMEOUT_SECONDS}s"

        return 124, _as_text(error.stdout), f"{_as_text(error.stderr)}\n{note}".strip()

    return result.returncode, result.stdout, result.stderr


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")

    return str(value)


def git_remote_url(path: str) -> str:
    if not path:
        return ""
    try:
        result = subprocess.run(
            ["git", "-C", path, "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""

    return result.stdout.strip()


def run_install(
    args: Namespace,
    stdout: TextIO,
    stderr: TextIO,
    transport: Transport | None = None,
    *,
    runner: Runner | None = None,
) -> int:
    active_transport = transport or urllib_transport
    connect_code = run_connect_flags(args, stdout, stderr, active_transport)
    if connect_code != 0:
        return connect_code

    try:
        if not args.skip_plugin_install:
            claude_bin = shutil.which(args.claude_bin or "claude")
            if not claude_bin:
                raise CliError(
                    "claude_cli_not_found",
                    "Could not find the Claude Code CLI on PATH",
                    remediation_for("claude_cli_not_found"),
                )
            install_claude_plugin(
                runner or subprocess_runner,
                claude_bin=claude_bin,
                marketplace_source=args.marketplace_source,
                marketplace_name=args.marketplace_name,
                plugin_name=args.plugin_name,
                api_key=args.api_key or "",
            )
    except CliError as error:
        emit_error(stderr, error, args.api_key or "")

        return 1

    doctor_code = run_doctor(args, stdout, stderr, active_transport)
    stdout.write("MCP tools ship with the Claude Code plugin (no extra setup).\n")

    return doctor_code


def install_claude_plugin(
    runner: Runner,
    *,
    claude_bin: str,
    marketplace_source: str,
    marketplace_name: str,
    plugin_name: str,
    api_key: str,
) -> None:
    plugin_commands = (
        [claude_bin, "plugin", "marketplace", "add", marketplace_source],
        [claude_bin, "plugin", "install", f"{plugin_name}@{marketplace_name}"],
    )
    for command in plugin_commands:
        returncode, command_stdout, command_stderr = runner(command)
        if returncode != 0:
            combined = "\n".join(
                part.strip()
                for part in (command_stdout, command_stderr)
                if part.strip()
            )
            raise CliError(
                "plugin_install_failed",
                redact_secret(combined or "claude plugin install failed", api_key),
                remediation_for("plugin_install_failed"),
            )


def run_disconnect(args: Namespace, stdout: TextIO, _stderr: TextIO) -> int:
    paths = local_paths(args.config_dir)
    removed = False
    removed = remove_if_exists(paths.config) or removed
    removed = remove_if_exists(paths.credentials) or removed
    for runtime in ("codex", "claude_code"):
        removed = remove_if_exists(paths.hook_manifest(runtime)) or removed
    if paths.hooks_dir.exists():
        try:
            paths.hooks_dir.rmdir()
        except OSError:
            pass
    if removed:
        stdout.write("disconnected Engram local state.\n")
    else:
        stdout.write("nothing connected.\n")

    return 0


def run_hook(
    args: Namespace,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
    transport: Transport | None = None,
) -> int:
    api_key = ""
    try:
        paths = local_paths(args.config_dir)
        config = load_required_json(
            paths.config, "missing_config", "Engram config is missing"
        )
        credentials = load_required_json(
            paths.credentials, "missing_credential", "Engram credential is missing"
        )
        api_key = as_string(credentials.get("api_key"))
        if not api_key:
            raise CliError(
                "missing_credential",
                "Engram credential is missing",
                remediation_for("missing_credential"),
            )
        server_url = normalize_server_url(as_string(config.get("server_url")))
        runtime = selected_runtime(
            args.agent, as_string_list(config.get("agent_runtimes"))
        )
        input_payload = read_stdin_json(stdin)
        active_transport = transport or urllib_transport
        if args.hook_command == "post-tool-use":
            status, body = send_hook_event(
                active_transport,
                server_url=server_url,
                api_key=api_key,
                config=config,
                runtime=runtime,
                input_payload=input_payload,
                path="/v1/hooks/post-tool-use",
                event_type="post_tool_use",
            )
        elif args.hook_command == "session-start":
            hook_status, hook_body = send_hook_event(
                active_transport,
                server_url=server_url,
                api_key=api_key,
                config=config,
                runtime=runtime,
                input_payload=input_payload,
                path="/v1/hooks/session-start",
                event_type="session_start",
            )
            if hook_status < 200 or hook_status >= 300:
                raise error_from_body(hook_body, fallback="http_error")
            status, body = post_json(
                transport=active_transport,
                server_url=server_url,
                path="/v1/context/session-start",
                api_key=api_key,
                payload=build_session_start_payload(config, runtime, input_payload),
            )
        elif args.hook_command == "error":
            status, body = send_hook_event(
                active_transport,
                server_url=server_url,
                api_key=api_key,
                config=config,
                runtime=runtime,
                input_payload=input_payload,
                path="/v1/hooks/error",
                event_type="error",
            )
        elif args.hook_command == "decision":
            status, body = send_hook_event(
                active_transport,
                server_url=server_url,
                api_key=api_key,
                config=config,
                runtime=runtime,
                input_payload=input_payload,
                path="/v1/hooks/decision",
                event_type="decision",
            )
        elif args.hook_command == "session-end":
            status, body = send_hook_event(
                active_transport,
                server_url=server_url,
                api_key=api_key,
                config=config,
                runtime=runtime,
                input_payload=input_payload,
                path="/v1/hooks/session-end",
                event_type="session_end",
            )
        elif args.hook_command == "user-prompt-submit":
            hook_status, hook_body = send_hook_event(
                active_transport,
                server_url=server_url,
                api_key=api_key,
                config=config,
                runtime=runtime,
                input_payload=input_payload,
                path="/v1/hooks/user-prompt-submit",
                event_type="user_prompt_submit",
            )
            if hook_status < 200 or hook_status >= 300:
                raise error_from_body(hook_body, fallback="http_error")
            status, body = post_json(
                transport=active_transport,
                server_url=server_url,
                path="/v1/context/user-prompt-submit",
                api_key=api_key,
                payload=build_user_prompt_submit_payload(config, runtime, input_payload),
            )
        else:
            raise CliError(
                "invalid_response",
                "Unsupported hook command",
                remediation_for("invalid_response"),
            )

        if status < 200 or status >= 300:
            raise error_from_body(body, fallback="http_error")
        stdout.write(
            json.dumps(
                format_hook_response(body, args.response_format, args.hook_command),
                sort_keys=True,
            )
            + "\n"
        )

        return 0
    except CliError as error:
        emit_error(stderr, error, api_key)

        return 1


def normalize_server_url(value: str | None) -> str:
    server_url = required_value(
        value, "missing_server_url", "Server URL is required"
    ).rstrip("/")
    if not server_url:
        raise CliError(
            "missing_server_url",
            "Server URL is required",
            remediation_for("missing_server_url"),
        )
    parsed = urlparse(server_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise CliError(
            "server_unavailable",
            "Server URL must start with http:// or https:// and include a host",
            remediation_for("server_unavailable"),
        )

    return server_url


def required_value(value: str | None, code: str, detail: str) -> str:
    if value is None or not value.strip():
        raise CliError(code, detail, remediation_for(code))

    return value.strip()


def normalize_runtimes(value: str | None) -> tuple[str, ...]:
    runtime = value or "both"
    if runtime == "claude-code":
        runtime = "claude_code"
    if runtime == "both":
        return ("codex", "claude_code")
    if runtime in {"codex", "claude_code"}:
        return (runtime,)

    raise CliError(
        "invalid_response",
        f"Unsupported agent runtime {runtime}",
        remediation_for("invalid_response"),
    )


def response_format_for_runtime(runtime: str) -> str:
    if runtime == "claude_code":
        return "claude-code"

    return runtime


def selected_runtime(value: str | None, configured_runtimes: list[str]) -> str:
    if not configured_runtimes:
        raise CliError(
            "missing_hook_config",
            "No hook manifests are configured",
            remediation_for("missing_hook_config"),
        )
    if value is None:
        return configured_runtimes[0]
    runtime = normalize_runtimes(value)[0]
    if runtime not in configured_runtimes:
        raise CliError(
            "missing_hook_config",
            f"Hook manifest for {runtime} is missing",
            remediation_for("missing_hook_config"),
        )

    return runtime


def read_stdin_json(stdin: TextIO) -> dict[str, object]:
    try:
        payload = json.loads(stdin.read() or "{}")
    except json.JSONDecodeError as error:
        raise CliError(
            "invalid_response",
            f"Hook input must be a JSON object: {error.msg}",
            remediation_for("invalid_response"),
        ) from error
    if not isinstance(payload, dict):
        raise CliError(
            "invalid_response",
            "Hook input must be a JSON object",
            remediation_for("invalid_response"),
        )

    return payload


def build_post_tool_use_payload(
    config: dict[str, object],
    runtime: str,
    input_payload: dict[str, object],
) -> dict[str, object]:
    return build_generic_hook_payload(config, runtime, input_payload, "post_tool_use")


OBSERVATION_BODY_MAX_LENGTH = 16000
OBSERVATION_PATH_MAX_LENGTH = 1024
OBSERVATION_TOOL_INPUT_PREVIEW_CHARS = 2000
OBSERVATION_TOOL_RESPONSE_PREVIEW_CHARS = 6000
PAYLOAD_TOOL_INPUT_MAX_BYTES = 32768
PAYLOAD_TOOL_INPUT_PREVIEW_CHARS = 2000
FILES_READ_TOOLS = ("Read",)
FILES_MODIFIED_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


def compact_json(value: object, limit: int) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        text = str(value)
    if len(text) > limit:
        return text[:limit] + "…[truncated]"

    return text


def tool_file_paths(tool_name: str, tool_input: object) -> list[str]:
    if tool_name not in FILES_READ_TOOLS + FILES_MODIFIED_TOOLS:
        return []
    if not isinstance(tool_input, dict):
        return []
    path = tool_input.get("file_path") or tool_input.get("notebook_path")
    if isinstance(path, str) and path.strip():
        return [path.strip()[:OBSERVATION_PATH_MAX_LENGTH]]

    return []


def synthesize_observation(
    input_payload: dict[str, object], event_type: str
) -> dict[str, object]:
    parts: list[str] = []
    prompt = payload_string(input_payload, "prompt")
    if prompt:
        parts.append(prompt)
    for field in ("query", "message", "reason", "source"):
        value = payload_string(input_payload, field)
        if value:
            parts.append(f"{field}: {value}")
    tool_name = payload_string(input_payload, "tool_name")
    if tool_name:
        parts.append(f"tool: {tool_name}")
        tool_input = input_payload.get("tool_input")
        if isinstance(tool_input, dict) and tool_input:
            parts.append(
                "input: "
                + compact_json(tool_input, OBSERVATION_TOOL_INPUT_PREVIEW_CHARS)
            )
        tool_response = input_payload.get("tool_response")
        if tool_response:
            parts.append(
                "result: "
                + compact_json(tool_response, OBSERVATION_TOOL_RESPONSE_PREVIEW_CHARS)
            )
    body = "\n".join(parts)[:OBSERVATION_BODY_MAX_LENGTH]
    if not body:
        return {}
    observation: dict[str, object] = {"type": event_type, "body": body}
    file_paths = tool_file_paths(tool_name, input_payload.get("tool_input"))
    if file_paths:
        field = "files_modified" if tool_name in FILES_MODIFIED_TOOLS else "files_read"
        observation[field] = file_paths

    return observation


def build_generic_hook_payload(
    config: dict[str, object],
    runtime: str,
    input_payload: dict[str, object],
    event_type: str,
) -> dict[str, object]:
    payload = dict_value(input_payload.get("payload"))
    if not payload:
        payload = {"trigger": event_type}
        for field in ("prompt", "query", "tool_name", "message", "reason"):
            value = payload_string(input_payload, field)
            if value:
                payload[field] = value
        tool_input = input_payload.get("tool_input")
        if isinstance(tool_input, dict) and tool_input:
            serialized = json.dumps(tool_input, sort_keys=True)
            if len(serialized) > PAYLOAD_TOOL_INPUT_MAX_BYTES:
                payload["tool_input_preview"] = (
                    serialized[:PAYLOAD_TOOL_INPUT_PREVIEW_CHARS] + "…[truncated]"
                )
            else:
                payload["tool_input"] = tool_input
    observation = dict_value(input_payload.get("observation"))
    if not observation:
        observation = synthesize_observation(input_payload, event_type)
    session_id = required_payload_string(input_payload, "session_id")
    payload_schema_version = (
        payload_string(input_payload, "payload_schema_version") or "v1"
    )
    sequence_number = input_payload.get("sequence_number")
    stable_event_material = {
        "event_type": event_type,
        "session_id": session_id,
        "request_id": payload_string(input_payload, "request_id"),
        "payload_schema_version": payload_schema_version,
        "sequence_number": sequence_number
        if isinstance(sequence_number, int)
        else None,
        "payload": payload,
        "observation": observation,
        "project_id": as_string(config.get("project_id")),
        "team_id": as_string(config.get("team_id")),
        "agent_runtime": runtime,
        "agent_external_id": payload_string(input_payload, "agent_external_id"),
        "repository_url": payload_string(input_payload, "repository_url"),
        "repository_root": payload_string(input_payload, "repository_root"),
        "branch": payload_string(input_payload, "branch"),
        "cwd": payload_string(input_payload, "cwd"),
    }
    stable_hash = stable_content_hash(stable_event_material)
    event_id = payload_string(input_payload, "event_id") or f"engram-cli-{stable_hash}"
    request_payload = base_hook_payload(config, runtime, input_payload)
    request_payload.update(
        {
            "session_id": session_id,
            "event_id": event_id,
            "idempotency_key": payload_string(input_payload, "idempotency_key")
            or event_id,
            "event_type": event_type,
            "payload_schema_version": payload_schema_version,
            "content_hash": payload_string(input_payload, "content_hash")
            or stable_hash,
            "request_id": payload_string(input_payload, "request_id") or event_id,
            "payload": payload,
            "occurred_at": payload_string(input_payload, "occurred_at")
            or datetime.now(UTC).isoformat(),
        },
    )
    if observation:
        request_payload["observation"] = observation
    copy_optional_strings(
        request_payload,
        input_payload,
        (
            "agent_external_id",
            "correlation_id",
            "trace_id",
            "repository_url",
            "repository_root",
            "branch",
            "cwd",
        ),
    )

    return request_payload


def extract_model_id(input_payload: dict[str, object]) -> str:
    model_id = payload_string(input_payload, "model_id")
    if model_id:
        return model_id

    model = input_payload.get("model")
    if isinstance(model, str):
        return model.strip()
    if isinstance(model, dict):
        return payload_string(model, "id")

    return ""


def build_session_start_hook_payload(
    config: dict[str, object],
    runtime: str,
    input_payload: dict[str, object],
) -> dict[str, object]:
    payload = dict_value(input_payload.get("payload"))
    if payload:
        if not payload_string(payload, "model_id"):
            model_id = extract_model_id(input_payload)
            if model_id:
                payload["model_id"] = model_id
        lifecycle_input_payload = dict(input_payload)
        lifecycle_input_payload["payload"] = payload

        return build_generic_hook_payload(
            config, runtime, lifecycle_input_payload, "session_start"
        )

    lifecycle_input_payload = dict(input_payload)
    lifecycle_payload: dict[str, object] = {"trigger": "session_start"}
    copy_optional_strings(
        lifecycle_payload, input_payload, ("repository_root", "branch", "cwd")
    )
    model_id = extract_model_id(input_payload)
    if model_id:
        lifecycle_payload["model_id"] = model_id
    lifecycle_input_payload["payload"] = lifecycle_payload

    return build_generic_hook_payload(
        config, runtime, lifecycle_input_payload, "session_start"
    )


def send_hook_event(
    transport: Transport,
    *,
    server_url: str,
    api_key: str,
    config: dict[str, object],
    runtime: str,
    input_payload: dict[str, object],
    path: str,
    event_type: str,
) -> tuple[int, dict[str, object]]:
    if event_type == "session_start":
        payload = build_session_start_hook_payload(config, runtime, input_payload)
    else:
        payload = build_generic_hook_payload(config, runtime, input_payload, event_type)

    return post_json(
        transport=transport,
        server_url=server_url,
        path=path,
        api_key=api_key,
        payload=payload,
    )


def format_hook_response(
    body: dict[str, object], response_format: str, hook_command: str
) -> dict[str, object]:
    if response_format == "server":
        return body
    if hook_command == "session-start":
        rendered = as_string(body.get("rendered_context"))
        if response_format == "claude-code":
            return {
                "systemMessage": rendered,
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": rendered,
                },
            }

        return {
            "continue": True,
            "systemMessage": rendered,
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": rendered,
            },
        }
    if hook_command == "user-prompt-submit":
        rendered = as_string(body.get("rendered_context"))
        if response_format == "claude-code":
            return {
                "systemMessage": rendered,
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": rendered,
                },
            }

        return {
            "continue": True,
            "systemMessage": rendered,
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": rendered,
            },
        }
    if response_format == "claude-code":
        return {}

    return {"continue": True}


def build_user_prompt_submit_payload(
    config: dict[str, object],
    runtime: str,
    input_payload: dict[str, object],
) -> dict[str, object]:
    request_payload = base_hook_payload(config, runtime, input_payload)
    request_payload.update(
        {
            "session_id": required_payload_string(input_payload, "session_id"),
            "request_id": payload_string(input_payload, "request_id")
            or f"engram-cli-{uuid.uuid4()}",
            "query": payload_string(input_payload, "query"),
            "file_paths": list_value(input_payload.get("file_paths")),
            "symbols": list_value(input_payload.get("symbols")),
        },
    )
    for field in ("limit", "token_budget"):
        value = input_payload.get(field)
        if isinstance(value, int):
            request_payload[field] = value
    copy_optional_strings(
        request_payload,
        input_payload,
        (
            "agent_external_id",
            "correlation_id",
            "trace_id",
            "repository_url",
            "repository_root",
            "branch",
            "cwd",
        ),
    )

    return request_payload


def build_session_start_payload(
    config: dict[str, object],
    runtime: str,
    input_payload: dict[str, object],
) -> dict[str, object]:
    request_payload = base_hook_payload(config, runtime, input_payload)
    request_payload.update(
        {
            "session_id": required_payload_string(input_payload, "session_id"),
            "request_id": payload_string(input_payload, "request_id")
            or f"engram-cli-{uuid.uuid4()}",
            "query": payload_string(input_payload, "query"),
            "file_paths": list_value(input_payload.get("file_paths")),
            "symbols": list_value(input_payload.get("symbols")),
        },
    )
    for field in ("limit", "token_budget"):
        value = input_payload.get(field)
        if isinstance(value, int):
            request_payload[field] = value
    copy_optional_strings(
        request_payload,
        input_payload,
        (
            "agent_external_id",
            "correlation_id",
            "trace_id",
            "repository_url",
            "repository_root",
            "branch",
            "cwd",
        ),
    )

    return request_payload


def base_hook_payload(
    config: dict[str, object],
    runtime: str,
    input_payload: dict[str, object],
) -> dict[str, object]:
    payload: dict[str, object] = {
        "agent_runtime": runtime,
        "agent_version": as_string(config.get("agent_version")),
    }
    project_id = as_string(config.get("project_id"))
    if project_id:
        payload["project_id"] = project_id
    team_id = as_string(config.get("team_id"))
    if team_id:
        payload["team_id"] = team_id
    repository_url = payload_string(input_payload, "repository_url") or git_remote_url(
        payload_string(input_payload, "repository_root")
        or payload_string(input_payload, "cwd")
    )
    if repository_url:
        payload["repository_url"] = repository_url
    if not project_id and not repository_url:
        raise CliError(
            "missing_project",
            "Set --project or run the agent inside a git repository",
            remediation_for("missing_project"),
        )
    input_team_id = payload_string(input_payload, "team_id")
    if input_team_id and input_team_id != team_id:
        raise CliError(
            "team_scope_denied",
            "Hook input team does not match connected team",
            remediation_for("team_scope_denied"),
        )

    return payload


def payload_string(payload: dict[str, object], field: str) -> str:
    return as_string(payload.get(field)).strip()


def required_payload_string(payload: dict[str, object], field: str) -> str:
    value = payload_string(payload, field)
    if not value:
        raise CliError(
            "invalid_response",
            f"Hook input missing {field}",
            remediation_for("invalid_response"),
        )

    return value


def dict_value(value: object) -> dict[str, object]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)

    raise CliError(
        "invalid_response",
        "Hook payload fields must be JSON objects",
        remediation_for("invalid_response"),
    )


def list_value(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise CliError(
            "invalid_response",
            "Hook list fields must be arrays",
            remediation_for("invalid_response"),
        )

    return [item for item in value if isinstance(item, str)]


def copy_optional_strings(
    target: dict[str, object],
    source: dict[str, object],
    fields: tuple[str, ...],
) -> None:
    for field in fields:
        value = payload_string(source, field)
        if value:
            target[field] = value


def stable_content_hash(payload: dict[str, object]) -> str:
    data = json.dumps(payload, sort_keys=True, separators=(",", ":"))

    return hashlib.sha256(data.encode()).hexdigest()


def require_dry_run_ok(
    transport: Transport,
    *,
    server_url: str,
    api_key: str,
    project_id: str,
    team_id: str,
    agent_runtime: str,
    agent_version: str,
) -> dict[str, object]:
    status, body = post_dry_run(
        transport=transport,
        server_url=server_url,
        api_key=api_key,
        project_id=project_id,
        team_id=team_id,
        agent_runtime=agent_runtime,
        agent_version=agent_version,
        request_id=f"engram-cli-{uuid.uuid4()}",
    )
    if status < 200 or status >= 300:
        raise error_from_body(body, fallback="http_error")
    if body.get("status") != "ok":
        raise error_from_body(body, fallback="invalid_response")

    return body


def error_from_body(body: dict[str, object], fallback: str) -> CliError:
    code = as_string(body.get("code")) or fallback
    detail = as_string(body.get("detail")) or code

    return CliError(code, detail, remediation_for(code))


def load_required_json(path: Path, code: str, detail: str) -> dict[str, object]:
    if not path.exists():
        raise CliError(code, detail, remediation_for(code))
    try:
        return read_json(path)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise CliError(
            "invalid_response",
            f"Could not read {path.name}: {error}",
            remediation_for("invalid_response"),
        ) from error


def write_local_state(
    *,
    paths_root: Path,
    server_url: str,
    project_id: str,
    team_id: str,
    runtimes: tuple[str, ...],
    agent_version: str,
    api_key: str,
    fingerprint: str,
    dry_run_result: dict[str, object],
    organization_id: str = "",
) -> None:
    paths = local_paths(str(paths_root))
    connected_at = (
        datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )
    config_payload: dict[str, object] = {
        "version": 1,
        "server_url": server_url,
        "project_id": project_id or None,
        "team_id": team_id or None,
        "agent_runtimes": list(runtimes),
        "agent_version": agent_version,
        "credential_fingerprint": fingerprint,
        "connected_at": connected_at,
        "resolved_actor": dry_run_result.get("resolved_actor", {}),
        "resolved_scope": dry_run_result.get("scope", {}),
    }
    if organization_id:
        config_payload["organization_id"] = organization_id
    credential_payload: dict[str, object] = {
        "version": 1,
        "api_key": api_key,
        "credential_fingerprint": fingerprint,
        "created_at": connected_at,
    }
    hook_payloads = {
        runtime: {
            "version": 1,
            "agent_runtime": runtime,
            "server_url": server_url,
            "project_id": project_id,
            "team_id": team_id or None,
            "credential_fingerprint": fingerprint,
            "commands": {
                "SessionStart": (
                    f"engram hook session-start --agent {runtime} "
                    f"--response-format {response_format_for_runtime(runtime)}"
                ),
                "PostToolUse": (
                    f"engram hook post-tool-use --agent {runtime} "
                    f"--response-format {response_format_for_runtime(runtime)}"
                ),
                "Error": (
                    f"engram hook error --agent {runtime} "
                    f"--response-format {response_format_for_runtime(runtime)}"
                ),
                "Decision": (
                    f"engram hook decision --agent {runtime} "
                    f"--response-format {response_format_for_runtime(runtime)}"
                ),
                "SessionEnd": (
                    f"engram hook session-end --agent {runtime} "
                    f"--response-format {response_format_for_runtime(runtime)}"
                ),
                "UserPromptSubmit": (
                    f"engram hook user-prompt-submit --agent {runtime} "
                    f"--response-format {response_format_for_runtime(runtime)}"
                ),
            },
        }
        for runtime in runtimes
    }
    write_json(paths.config, config_payload)
    write_secret_json(paths.credentials, credential_payload)
    for runtime, hook_payload in hook_payloads.items():
        write_json(paths.hook_manifest(runtime), hook_payload)


def emit_error(stderr: TextIO, error: CliError, secret: str = "") -> None:
    stderr.write(f"{error.code}: {redact_secret(error.detail, secret)}\n")
    stderr.write(f"remediation: {error.remediation}\n")


def remediation_for(code: str) -> str:
    return ERROR_REMEDIATION.get(code, ERROR_REMEDIATION["http_error"])


def redact_secret(value: str, secret: str) -> str:
    if not secret:
        return value

    return value.replace(secret, "[REDACTED]")


def run_mcp_install(
    args: Namespace,
    stdout: TextIO,
    stderr: TextIO,
    transport: Transport | None = None,
) -> int:
    api_key = ""
    try:
        paths = local_paths(args.config_dir)
        config = load_required_json(
            paths.config, "missing_config", "Engram config is missing"
        )
        credentials = load_required_json(
            paths.credentials, "missing_credential", "Engram credential is missing"
        )
        api_key = as_string(credentials.get("api_key"))
        if not api_key:
            raise CliError(
                "missing_credential",
                "Engram credential is missing",
                remediation_for("missing_credential"),
            )
        server_url = as_string(config.get("server_url"))
        project_id = as_string(config.get("project_id"))
        if not server_url or not project_id:
            raise CliError(
                "missing_config",
                "Engram config is incomplete",
                remediation_for("missing_config"),
            )
        targets = resolve_mcp_targets(args)
        entry = build_engram_mcp_entry(config_dir=args.config_dir)
        written: list[str] = []
        skipped: list[str] = []
        for label, path in targets:
            if label == "claude_desktop" and not desktop_target_writable(
                args, path
            ):
                skipped.append(label)

                continue
            write_engram_mcp_entry(path, entry)
            written.append(str(path))
        if not written:
            raise CliError(
                "missing_mcp_target",
                "No Claude config target was available",
                remediation_for("missing_mcp_target"),
            )
        for path_str in written:
            stdout.write(f"wrote engram MCP server to {path_str}\n")
        for label in skipped:
            stderr.write(
                f"skipped {label}: config path not found (install Claude "
                f"Desktop or pass --claude-desktop-config).\n"
            )
        stdout.write("installed engram MCP server.\n")
        if skipped:
            stdout.write(
                f"skipped: {', '.join(skipped)}\n"
            )

        return 0
    except CliError as error:
        emit_error(stderr, error, api_key)

        return 1


def build_engram_mcp_entry(*, config_dir: str | None = None) -> dict[str, object]:
    engram_bin = shutil.which("engram")
    if engram_bin:
        command = engram_bin
        args_list = ["mcp", "serve"]
    else:
        command = sys.executable
        args_list = ["-m", "engram_cli", "mcp", "serve"]
    if config_dir:
        args_list.extend(["--config-dir", config_dir])

    return {"command": command, "args": args_list}


def write_engram_mcp_entry(path: Path, entry: dict[str, object]) -> None:
    if path.exists():
        try:
            data = read_json(path)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise CliError(
                "invalid_response",
                f"Could not read {path.name}: {error}",
                remediation_for("invalid_response"),
            ) from error
    else:
        data = {}
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
        data["mcpServers"] = servers
    servers["engram"] = entry
    write_json(path, data)


def resolve_mcp_targets(
    args: Namespace,
) -> list[tuple[str, Path]]:
    agent = normalize_mcp_agent(getattr(args, "agent", "both"))
    targets: list[tuple[str, Path]] = []
    code_override = getattr(args, "claude_code_config", None)
    desktop_override = getattr(args, "claude_desktop_config", None)
    if "claude_code" in agent:
        targets.append(
            (
                "claude_code",
                Path(code_override).expanduser()
                if code_override
                else default_claude_code_config_path(),
            )
        )
    if "claude_desktop" in agent:
        targets.append(
            (
                "claude_desktop",
                Path(desktop_override).expanduser()
                if desktop_override
                else default_claude_desktop_config_path(),
            )
        )

    return targets


def desktop_target_writable(args: Namespace, path: Path) -> bool:
    if getattr(args, "claude_desktop_config", None):
        return True

    return path.parent.exists()


def normalize_mcp_agent(value: str | None) -> tuple[str, ...]:
    agent = (value or "both").strip()
    if agent == "both":
        return ("claude_code", "claude_desktop")
    if agent == "claude_code":
        return ("claude_code",)
    if agent == "claude_desktop":
        return ("claude_desktop",)
    raise CliError(
        "invalid_agent_target",
        f"Unsupported --agent value {agent}",
        remediation_for("invalid_agent_target"),
    )


def build_search_payload(
    config: dict[str, object],
    *,
    query: str,
    file_paths: list[str],
    symbols: list[str],
    limit: int,
    repository_url: str,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "query": query,
        "file_paths": file_paths,
        "symbols": symbols,
        "limit": limit,
    }
    project_id = as_string(config.get("project_id"))
    if project_id:
        payload["project_id"] = project_id
    elif repository_url:
        payload["repository_url"] = repository_url
    team_id = as_string(config.get("team_id"))
    if team_id:
        payload["team_id"] = team_id

    return payload


def run_search(
    args: Namespace,
    stdout: TextIO,
    stderr: TextIO,
    transport: Transport | None = None,
) -> int:
    api_key = ""
    try:
        paths = local_paths(args.config_dir)
        config = load_required_json(
            paths.config, "missing_config", "Engram config is missing"
        )
        credentials = load_required_json(
            paths.credentials, "missing_credential", "Engram credential is missing"
        )
        api_key = as_string(credentials.get("api_key"))
        if not api_key:
            raise CliError(
                "missing_credential",
                "Engram credential is missing",
                remediation_for("missing_credential"),
            )
        server_url = normalize_server_url(as_string(config.get("server_url")))
        repository_url = ""
        if not as_string(config.get("project_id")):
            repository_url = git_remote_url(os.getcwd())
        payload = build_search_payload(
            config,
            query=args.query or "",
            file_paths=list(args.file_path or []),
            symbols=list(args.symbol or []),
            limit=args.limit,
            repository_url=repository_url,
        )
        active_transport = transport or urllib_transport
        status, body = post_json(
            transport=active_transport,
            server_url=server_url,
            path="/v1/search/",
            api_key=api_key,
            payload=payload,
        )
        if status < 200 or status >= 300:
            raise error_from_body(body, fallback="http_error")
        items = body.get("items", [])
        if getattr(args, "as_json", False):
            stdout.write(json.dumps(body, sort_keys=True) + "\n")

            return 0

        if not items:
            stdout.write("No memory matched the search.\n")

            return 0

        for item in items:
            stdout.write(f"{item.get('citation')}: {item.get('title')}\n")
            stdout.write(f"  {item.get('body')}\n")

        return 0
    except CliError as error:
        emit_error(stderr, error, api_key)

        return 1


def _load_cli_scope(args: Namespace) -> tuple[Path, str, str, dict[str, object], str]:
    paths = local_paths(args.config_dir)
    config = load_required_json(
        paths.config, "missing_config", "Engram config is missing"
    )
    credentials = load_required_json(
        paths.credentials, "missing_credential", "Engram credential is missing"
    )
    api_key = as_string(credentials.get("api_key"))
    if not api_key:
        raise CliError(
            "missing_credential",
            "Engram credential is missing",
            remediation_for("missing_credential"),
        )
    server_url = normalize_server_url(as_string(config.get("server_url")))

    return paths, api_key, server_url, config, as_string(config.get("team_id"))


def run_memory_version(
    args: Namespace,
    stdout: TextIO,
    stderr: TextIO,
    transport: Transport | None = None,
) -> int:
    api_key = ""
    try:
        _paths, api_key, server_url, config, team_id = _load_cli_scope(args)
        payload: dict[str, object] = {
            "project_id": as_string(config.get("project_id")),
            "body": args.body,
            "request_id": args.request_id or f"engram-cli-{uuid.uuid4()}",
        }
        if args.reason:
            payload["reason"] = args.reason
        if team_id:
            payload["team_id"] = team_id
        active_transport = transport or urllib_transport
        status, body = post_json(
            transport=active_transport,
            server_url=server_url,
            path=f"/v1/memories/{args.memory_id}/version",
            api_key=api_key,
            payload=payload,
        )
        if status < 200 or status >= 300:
            raise error_from_body(body, fallback="http_error")
        stdout.write(f"memory_id={body.get('memory_id')}\n")
        stdout.write(f"current_version={body.get('current_version')}\n")
        stdout.write(f"memory_version_id={body.get('memory_version_id')}\n")

        return 0
    except CliError as error:
        emit_error(stderr, error, api_key)

        return 1


def run_memory_link(
    args: Namespace,
    stdout: TextIO,
    stderr: TextIO,
    transport: Transport | None = None,
) -> int:
    api_key = ""
    try:
        _paths, api_key, server_url, config, team_id = _load_cli_scope(args)
        payload: dict[str, object] = {
            "project_id": as_string(config.get("project_id")),
            "link_type": args.link_type,
            "target": args.target,
            "request_id": args.request_id or f"engram-cli-{uuid.uuid4()}",
        }
        if args.label:
            payload["label"] = args.label
        if team_id:
            payload["team_id"] = team_id
        active_transport = transport or urllib_transport
        status, body = post_json(
            transport=active_transport,
            server_url=server_url,
            path=f"/v1/memories/{args.memory_id}/links",
            api_key=api_key,
            payload=payload,
        )
        if status < 200 or status >= 300:
            raise error_from_body(body, fallback="http_error")
        stdout.write(f"link_id={body.get('link_id')}\n")
        stdout.write(f"link_type={body.get('link_type')}\n")
        stdout.write(f"target={body.get('target')}\n")

        return 0
    except CliError as error:
        emit_error(stderr, error, api_key)

        return 1


def run_memory_links(
    args: Namespace,
    stdout: TextIO,
    stderr: TextIO,
    transport: Transport | None = None,
) -> int:
    api_key = ""
    try:
        _paths, api_key, server_url, config, team_id = _load_cli_scope(args)
        params: dict[str, str] = {"project_id": as_string(config.get("project_id"))}
        if team_id:
            params["team_id"] = team_id
        active_transport = transport or urllib_transport
        status, body = get_json(
            transport=active_transport,
            server_url=server_url,
            path=f"/v1/memories/{args.memory_id}/links",
            api_key=api_key,
            params=params,
        )
        if status < 200 or status >= 300:
            raise error_from_body(body, fallback="http_error")
        items = body.get("items", [])
        if not items:
            stdout.write("No links recorded for this memory.\n")

            return 0
        for item in items:
            stdout.write(f"{item.get('link_type')}: {item.get('target')}\n")

        return 0
    except CliError as error:
        emit_error(stderr, error, api_key)

        return 1


def run_observations(
    args: Namespace,
    stdout: TextIO,
    stderr: TextIO,
    transport: Transport | None = None,
) -> int:
    api_key = ""
    try:
        paths = local_paths(args.config_dir)
        config = load_required_json(
            paths.config, "missing_config", "Engram config is missing"
        )
        credentials = load_required_json(
            paths.credentials, "missing_credential", "Engram credential is missing"
        )
        api_key = as_string(credentials.get("api_key"))
        if not api_key:
            raise CliError(
                "missing_credential",
                "Engram credential is missing",
                remediation_for("missing_credential"),
            )
        server_url = normalize_server_url(as_string(config.get("server_url")))
        params: dict[str, str] = {
            "project_id": as_string(config.get("project_id")),
            "limit": str(args.limit),
        }
        team_id = as_string(config.get("team_id"))
        if team_id:
            params["team_id"] = team_id
        active_transport = transport or urllib_transport
        status, body = get_json(
            transport=active_transport,
            server_url=server_url,
            path="/v1/observations/",
            api_key=api_key,
            params=params,
        )
        if status < 200 or status >= 300:
            raise error_from_body(body, fallback="http_error")
        items = body.get("items", [])
        if not items:
            stdout.write("No observations recorded for this project.\n")

            return 0
        for item in items:
            stdout.write(f"{item.get('observation_type')}: {item.get('title')}\n")
            stdout.write(f"  {item.get('body')}\n")

        return 0
    except CliError as error:
        emit_error(stderr, error, api_key)

        return 1
