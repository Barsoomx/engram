from __future__ import annotations

import hashlib
import json
import uuid
from argparse import Namespace
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO
from urllib.parse import urlparse

from engram_cli.config import (
    as_string,
    as_string_list,
    credential_fingerprint,
    local_paths,
    read_json,
    remove_if_exists,
    write_json,
    write_secret_json,
)
from engram_cli.http import Transport, get_health, post_dry_run, post_json, urllib_transport


class CliError(Exception):
    def __init__(self, code: str, detail: str, remediation: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.remediation = remediation


ERROR_REMEDIATION: dict[str, str] = {
    'missing_server_url': 'Pass --server with the Engram server URL.',
    'missing_api_key': 'Pass --api-key with a scoped Engram API key.',
    'missing_project': 'Pass --project with the Engram project id.',
    'missing_config': 'Run `engram connect` before doctor.',
    'missing_credential': 'Run `engram connect` again to write credentials.',
    'missing_hook_config': 'Run `engram connect` again to write hook manifests.',
    'server_unavailable': 'Check the server URL and /-/healthz/ endpoint.',
    'http_error': 'Check the server response and retry.',
    'invalid_response': 'Upgrade the CLI or server so response schemas match.',
    'invalid_key': 'Use a valid scoped Engram API key.',
    'expired_key': 'Rotate the API key and run `engram connect` again.',
    'missing_capability': 'Use a key with observations:write for hook dry-run.',
    'project_scope_denied': 'Use a key scoped to the requested project.',
    'team_scope_denied': 'Use a key scoped to the requested team.',
}


def run_connect(
    args: Namespace,
    stdout: TextIO,
    stderr: TextIO,
    transport: Transport | None = None,
) -> int:
    api_key_for_redaction = args.api_key or ''
    try:
        server_url = normalize_server_url(args.server)
        api_key = required_value(args.api_key, 'missing_api_key', 'API key is required')
        api_key_for_redaction = api_key
        project_id = required_value(args.project, 'missing_project', 'Project id is required')
        team_id = args.team or ''
        agent_version = args.agent_version or ''
        runtimes = normalize_runtimes(args.agent)
        active_transport = transport or urllib_transport
        dry_run_results = [
            require_dry_run_ok(
                active_transport,
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
        scope = dry_run_results[0].get('scope', {})
        stdout.write(f'connected Engram CLI to {server_url}\n')
        stdout.write(f'project: {project_id}\n')
        if team_id:
            stdout.write(f'team: {team_id}\n')
        stdout.write(f'runtimes: {", ".join(runtimes)}\n')
        stdout.write(f'credential: {fingerprint}\n')
        if isinstance(scope, dict):
            organization_id = scope.get('organization_id')
            if organization_id:
                stdout.write(f'organization: {organization_id}\n')
            capabilities = scope.get('capabilities')
            if isinstance(capabilities, list):
                stdout.write(f'capabilities: {", ".join(str(item) for item in capabilities)}\n')

        return 0
    except CliError as error:
        emit_error(stderr, error, api_key_for_redaction)

        return 1


def run_doctor(
    args: Namespace,
    stdout: TextIO,
    stderr: TextIO,
    transport: Transport | None = None,
) -> int:
    checks: list[tuple[str, str, str]] = []
    api_key = ''
    try:
        paths = local_paths(args.config_dir)
        config = load_required_json(paths.config, 'missing_config', 'Engram config is missing')
        checks.append(('ok', 'config', 'loaded'))
        credentials = load_required_json(paths.credentials, 'missing_credential', 'Engram credential is missing')
        api_key = as_string(credentials.get('api_key'))
        if not api_key:
            raise CliError('missing_credential', 'Engram credential is missing', remediation_for('missing_credential'))
        checks.append(('ok', 'credential', 'loaded'))
        runtimes = as_string_list(config.get('agent_runtimes'))
        if not runtimes:
            raise CliError('missing_hook_config', 'No hook manifests are configured', remediation_for('missing_hook_config'))
        for runtime in runtimes:
            load_required_json(
                paths.hook_manifest(runtime),
                'missing_hook_config',
                f'Hook manifest for {runtime} is missing',
            )
        checks.append(('ok', 'hook_config', ', '.join(runtimes)))
        active_transport = transport or urllib_transport
        server_url = normalize_server_url(as_string(config.get('server_url')))
        status, body = get_health(transport=active_transport, server_url=server_url)
        if status != 200 or body.get('status') != 'ok':
            raise CliError('server_unavailable', 'Engram server health check failed', remediation_for('server_unavailable'))
        checks.append(('ok', 'server_health', server_url))
        for runtime in runtimes:
            require_dry_run_ok(
                active_transport,
                server_url=server_url,
                api_key=api_key,
                project_id=as_string(config.get('project_id')),
                team_id=as_string(config.get('team_id')),
                agent_runtime=runtime,
                agent_version=as_string(config.get('agent_version')),
            )
        checks.append(('ok', 'dry_run', ', '.join(runtimes)))
    except CliError as error:
        for status, name, detail in checks:
            stdout.write(f'{status} {name}: {detail}\n')
        stdout.write(f'fail {error.code}: {redact_secret(error.detail, api_key)}\n')
        emit_error(stderr, error, api_key)

        return 1

    for status, name, detail in checks:
        stdout.write(f'{status} {name}: {detail}\n')
    stdout.write('All required checks passed.\n')

    return 0


def run_disconnect(args: Namespace, stdout: TextIO, _stderr: TextIO) -> int:
    paths = local_paths(args.config_dir)
    removed = False
    removed = remove_if_exists(paths.config) or removed
    removed = remove_if_exists(paths.credentials) or removed
    for runtime in ('codex', 'claude_code'):
        removed = remove_if_exists(paths.hook_manifest(runtime)) or removed
    if paths.hooks_dir.exists():
        try:
            paths.hooks_dir.rmdir()
        except OSError:
            pass
    if removed:
        stdout.write('disconnected Engram local state.\n')
    else:
        stdout.write('nothing connected.\n')

    return 0


def run_hook(
    args: Namespace,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
    transport: Transport | None = None,
) -> int:
    api_key = ''
    try:
        paths = local_paths(args.config_dir)
        config = load_required_json(paths.config, 'missing_config', 'Engram config is missing')
        credentials = load_required_json(paths.credentials, 'missing_credential', 'Engram credential is missing')
        api_key = as_string(credentials.get('api_key'))
        if not api_key:
            raise CliError('missing_credential', 'Engram credential is missing', remediation_for('missing_credential'))
        server_url = normalize_server_url(as_string(config.get('server_url')))
        runtime = selected_runtime(args.agent, as_string_list(config.get('agent_runtimes')))
        input_payload = read_stdin_json(stdin)
        active_transport = transport or urllib_transport
        if args.hook_command == 'post-tool-use':
            status, body = send_hook_event(
                active_transport,
                server_url=server_url,
                api_key=api_key,
                config=config,
                runtime=runtime,
                input_payload=input_payload,
                path='/v1/hooks/post-tool-use',
                event_type='post_tool_use',
            )
        elif args.hook_command == 'session-start':
            hook_status, hook_body = send_hook_event(
                active_transport,
                server_url=server_url,
                api_key=api_key,
                config=config,
                runtime=runtime,
                input_payload=input_payload,
                path='/v1/hooks/session-start',
                event_type='session_start',
            )
            if hook_status < 200 or hook_status >= 300:
                raise error_from_body(hook_body, fallback='http_error')
            status, body = post_json(
                transport=active_transport,
                server_url=server_url,
                path='/v1/context/session-start',
                api_key=api_key,
                payload=build_session_start_payload(config, runtime, input_payload),
            )
        elif args.hook_command == 'error':
            status, body = send_hook_event(
                active_transport,
                server_url=server_url,
                api_key=api_key,
                config=config,
                runtime=runtime,
                input_payload=input_payload,
                path='/v1/hooks/error',
                event_type='error',
            )
        elif args.hook_command == 'decision':
            status, body = send_hook_event(
                active_transport,
                server_url=server_url,
                api_key=api_key,
                config=config,
                runtime=runtime,
                input_payload=input_payload,
                path='/v1/hooks/decision',
                event_type='decision',
            )
        else:
            raise CliError('invalid_response', 'Unsupported hook command', remediation_for('invalid_response'))

        if status < 200 or status >= 300:
            raise error_from_body(body, fallback='http_error')
        stdout.write(json.dumps(format_hook_response(body, args.response_format, args.hook_command), sort_keys=True) + '\n')

        return 0
    except CliError as error:
        emit_error(stderr, error, api_key)

        return 1


def normalize_server_url(value: str | None) -> str:
    server_url = required_value(value, 'missing_server_url', 'Server URL is required').rstrip('/')
    if not server_url:
        raise CliError('missing_server_url', 'Server URL is required', remediation_for('missing_server_url'))
    parsed = urlparse(server_url)
    if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
        raise CliError(
            'server_unavailable',
            'Server URL must start with http:// or https:// and include a host',
            remediation_for('server_unavailable'),
        )

    return server_url


def required_value(value: str | None, code: str, detail: str) -> str:
    if value is None or not value.strip():
        raise CliError(code, detail, remediation_for(code))

    return value.strip()


def normalize_runtimes(value: str | None) -> tuple[str, ...]:
    runtime = value or 'both'
    if runtime == 'claude-code':
        runtime = 'claude_code'
    if runtime == 'both':
        return ('codex', 'claude_code')
    if runtime in {'codex', 'claude_code'}:
        return (runtime,)

    raise CliError('invalid_response', f'Unsupported agent runtime {runtime}', remediation_for('invalid_response'))


def response_format_for_runtime(runtime: str) -> str:
    if runtime == 'claude_code':
        return 'claude-code'

    return runtime


def selected_runtime(value: str | None, configured_runtimes: list[str]) -> str:
    if not configured_runtimes:
        raise CliError('missing_hook_config', 'No hook manifests are configured', remediation_for('missing_hook_config'))
    if value is None:
        return configured_runtimes[0]
    runtime = normalize_runtimes(value)[0]
    if runtime not in configured_runtimes:
        raise CliError('missing_hook_config', f'Hook manifest for {runtime} is missing', remediation_for('missing_hook_config'))

    return runtime


def read_stdin_json(stdin: TextIO) -> dict[str, object]:
    try:
        payload = json.loads(stdin.read() or '{}')
    except json.JSONDecodeError as error:
        raise CliError('invalid_response', f'Hook input must be a JSON object: {error.msg}', remediation_for('invalid_response')) from error
    if not isinstance(payload, dict):
        raise CliError('invalid_response', 'Hook input must be a JSON object', remediation_for('invalid_response'))

    return payload


def build_post_tool_use_payload(
    config: dict[str, object],
    runtime: str,
    input_payload: dict[str, object],
) -> dict[str, object]:
    return build_generic_hook_payload(config, runtime, input_payload, 'post_tool_use')


def build_generic_hook_payload(
    config: dict[str, object],
    runtime: str,
    input_payload: dict[str, object],
    event_type: str,
) -> dict[str, object]:
    payload = dict_value(input_payload.get('payload'))
    observation = dict_value(input_payload.get('observation'))
    session_id = required_payload_string(input_payload, 'session_id')
    payload_schema_version = payload_string(input_payload, 'payload_schema_version') or 'v1'
    sequence_number = input_payload.get('sequence_number')
    stable_event_material = {
        'event_type': event_type,
        'session_id': session_id,
        'request_id': payload_string(input_payload, 'request_id'),
        'payload_schema_version': payload_schema_version,
        'sequence_number': sequence_number if isinstance(sequence_number, int) else None,
        'payload': payload,
        'observation': observation,
        'project_id': as_string(config.get('project_id')),
        'team_id': as_string(config.get('team_id')),
        'agent_runtime': runtime,
        'agent_external_id': payload_string(input_payload, 'agent_external_id'),
        'repository_url': payload_string(input_payload, 'repository_url'),
        'repository_root': payload_string(input_payload, 'repository_root'),
        'branch': payload_string(input_payload, 'branch'),
        'cwd': payload_string(input_payload, 'cwd'),
    }
    stable_hash = stable_content_hash(stable_event_material)
    event_id = payload_string(input_payload, 'event_id') or f'engram-cli-{stable_hash}'
    request_payload = base_hook_payload(config, runtime, input_payload)
    request_payload.update(
        {
            'session_id': session_id,
            'event_id': event_id,
            'idempotency_key': payload_string(input_payload, 'idempotency_key') or event_id,
            'event_type': event_type,
            'payload_schema_version': payload_schema_version,
            'content_hash': payload_string(input_payload, 'content_hash') or stable_hash,
            'request_id': payload_string(input_payload, 'request_id') or event_id,
            'payload': payload,
        },
    )
    if observation:
        request_payload['observation'] = observation
    copy_optional_strings(
        request_payload,
        input_payload,
        ('agent_external_id', 'correlation_id', 'trace_id', 'repository_url', 'repository_root', 'branch', 'cwd'),
    )

    return request_payload


def build_session_start_hook_payload(
    config: dict[str, object],
    runtime: str,
    input_payload: dict[str, object],
) -> dict[str, object]:
    payload = dict_value(input_payload.get('payload'))
    if payload:
        return build_generic_hook_payload(config, runtime, input_payload, 'session_start')

    lifecycle_input_payload = dict(input_payload)
    lifecycle_payload: dict[str, object] = {'trigger': 'session_start'}
    copy_optional_strings(lifecycle_payload, input_payload, ('repository_root', 'branch', 'cwd'))
    lifecycle_input_payload['payload'] = lifecycle_payload

    return build_generic_hook_payload(config, runtime, lifecycle_input_payload, 'session_start')


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
    if event_type == 'session_start':
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


def format_hook_response(body: dict[str, object], response_format: str, hook_command: str) -> dict[str, object]:
    if response_format == 'server':
        return body
    if hook_command == 'session-start':
        rendered = as_string(body.get('rendered_context'))
        if response_format == 'claude-code':
            return {
                'systemMessage': rendered,
                'hookSpecificOutput': {
                    'hookEventName': 'SessionStart',
                    'additionalContext': rendered,
                },
            }

        return {
            'continue': True,
            'systemMessage': rendered,
            'hookSpecificOutput': {
                'hookEventName': 'SessionStart',
                'additionalContext': rendered,
            },
        }
    if response_format == 'claude-code':
        return {}

    return {'continue': True}


def build_session_start_payload(
    config: dict[str, object],
    runtime: str,
    input_payload: dict[str, object],
) -> dict[str, object]:
    request_payload = base_hook_payload(config, runtime, input_payload)
    request_payload.update(
        {
            'session_id': required_payload_string(input_payload, 'session_id'),
            'request_id': payload_string(input_payload, 'request_id') or f'engram-cli-{uuid.uuid4()}',
            'query': payload_string(input_payload, 'query'),
            'file_paths': list_value(input_payload.get('file_paths')),
            'symbols': list_value(input_payload.get('symbols')),
        },
    )
    for field in ('limit', 'token_budget'):
        value = input_payload.get(field)
        if isinstance(value, int):
            request_payload[field] = value
    copy_optional_strings(
        request_payload,
        input_payload,
        ('agent_external_id', 'correlation_id', 'trace_id', 'repository_url', 'repository_root', 'branch', 'cwd'),
    )

    return request_payload


def base_hook_payload(
    config: dict[str, object],
    runtime: str,
    input_payload: dict[str, object],
) -> dict[str, object]:
    payload = {
        'project_id': as_string(config.get('project_id')),
        'agent_runtime': runtime,
        'agent_version': as_string(config.get('agent_version')),
    }
    team_id = as_string(config.get('team_id'))
    if team_id:
        payload['team_id'] = team_id
    if not payload['project_id']:
        raise CliError('missing_project', 'Project id is missing from local config', remediation_for('missing_project'))
    input_team_id = payload_string(input_payload, 'team_id')
    if input_team_id and input_team_id != team_id:
        raise CliError('team_scope_denied', 'Hook input team does not match connected team', remediation_for('team_scope_denied'))

    return payload


def payload_string(payload: dict[str, object], field: str) -> str:
    return as_string(payload.get(field)).strip()


def required_payload_string(payload: dict[str, object], field: str) -> str:
    value = payload_string(payload, field)
    if not value:
        raise CliError('invalid_response', f'Hook input missing {field}', remediation_for('invalid_response'))

    return value


def dict_value(value: object) -> dict[str, object]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)

    raise CliError('invalid_response', 'Hook payload fields must be JSON objects', remediation_for('invalid_response'))


def list_value(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise CliError('invalid_response', 'Hook list fields must be arrays', remediation_for('invalid_response'))

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
    data = json.dumps(payload, sort_keys=True, separators=(',', ':'))

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
        request_id=f'engram-cli-{uuid.uuid4()}',
    )
    if status < 200 or status >= 300:
        raise error_from_body(body, fallback='http_error')
    if body.get('status') != 'ok':
        raise error_from_body(body, fallback='invalid_response')

    return body


def error_from_body(body: dict[str, object], fallback: str) -> CliError:
    code = as_string(body.get('code')) or fallback
    detail = as_string(body.get('detail')) or code

    return CliError(code, detail, remediation_for(code))


def load_required_json(path: Path, code: str, detail: str) -> dict[str, object]:
    if not path.exists():
        raise CliError(code, detail, remediation_for(code))
    try:
        return read_json(path)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise CliError('invalid_response', f'Could not read {path.name}: {error}', remediation_for('invalid_response')) from error


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
) -> None:
    paths = local_paths(str(paths_root))
    connected_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    config_payload: dict[str, object] = {
        'version': 1,
        'server_url': server_url,
        'project_id': project_id,
        'team_id': team_id or None,
        'agent_runtimes': list(runtimes),
        'agent_version': agent_version,
        'credential_fingerprint': fingerprint,
        'connected_at': connected_at,
        'resolved_actor': dry_run_result.get('resolved_actor', {}),
        'resolved_scope': dry_run_result.get('scope', {}),
    }
    credential_payload: dict[str, object] = {
        'version': 1,
        'api_key': api_key,
        'credential_fingerprint': fingerprint,
        'created_at': connected_at,
    }
    hook_payloads = {
        runtime: {
            'version': 1,
            'agent_runtime': runtime,
            'server_url': server_url,
            'project_id': project_id,
            'team_id': team_id or None,
            'credential_fingerprint': fingerprint,
            'commands': {
                'SessionStart': (
                    f'engram hook session-start --agent {runtime} '
                    f'--response-format {response_format_for_runtime(runtime)}'
                ),
                'PostToolUse': (
                    f'engram hook post-tool-use --agent {runtime} '
                    f'--response-format {response_format_for_runtime(runtime)}'
                ),
                'Error': (
                    f'engram hook error --agent {runtime} '
                    f'--response-format {response_format_for_runtime(runtime)}'
                ),
                'Decision': (
                    f'engram hook decision --agent {runtime} '
                    f'--response-format {response_format_for_runtime(runtime)}'
                ),
            },
        }
        for runtime in runtimes
    }
    write_json(paths.config, config_payload)
    write_secret_json(paths.credentials, credential_payload)
    for runtime, hook_payload in hook_payloads.items():
        write_json(paths.hook_manifest(runtime), hook_payload)


def emit_error(stderr: TextIO, error: CliError, secret: str = '') -> None:
    stderr.write(f'{error.code}: {redact_secret(error.detail, secret)}\n')
    stderr.write(f'remediation: {error.remediation}\n')


def remediation_for(code: str) -> str:
    return ERROR_REMEDIATION.get(code, ERROR_REMEDIATION['http_error'])


def redact_secret(value: str, secret: str) -> str:
    if not secret:
        return value

    return value.replace(secret, '[REDACTED]')
