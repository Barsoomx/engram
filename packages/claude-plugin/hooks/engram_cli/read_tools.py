from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from engram_cli.http import Transport, get_json

_CONTROL_CHARS = re.compile(r'[\x00-\x1f]+')

MEMORY_GET_VALIDITY_NOTE = (
    '(status, confidence, kind, and conflict/stale/refuted validity come from '
    'engram_search, not this tool)'
)


def collapse_control_chars(value: Any) -> str:
    if value is None:
        return ''

    return _CONTROL_CHARS.sub(' ', str(value))


def _text(value: Any) -> str:
    if value is None:
        return ''

    return str(value)


def memory_not_found_message(memory_id: str) -> str:
    return f'Memory {memory_id} was not found (or not visible with this key/project).'


def project_scope_denied_message(project_id: str, repository_url: str = '') -> str:
    if project_id:
        target = f'project {project_id}'
    elif repository_url:
        target = f'the project for repository {repository_url}'
    else:
        target = 'the requested project'

    return (
        f'This key cannot resolve {target}. Use a project-bound key, or the '
        'projects:agent key from the Connect-agent modal, then retry.'
    )


def team_scope_denied_message(team_id: str, memory_id: str) -> str:
    if team_id:
        return (
            f'This key cannot access team {team_id} for memory {memory_id}. Use a key bound to '
            'that team (or one with team admin), then retry.'
        )

    return (
        f'This key cannot access the team scope of memory {memory_id}. Use a key bound to that '
        "memory's team (or one with team admin), then retry."
    )


def memory_read_missing_capability_message() -> str:
    return (
        'This key cannot read this memory. Re-issue the API key with the memories:read '
        'capability from the Engram console, then retry.'
    )


def audit_team_scope_denied_message(
    team_id: str,
    target_id: str,
    target_type: str,
    project_id: str,
) -> str:
    if target_id:
        safe_type = collapse_control_chars(target_type) or 'memory'
        subject = f'{safe_type} {collapse_control_chars(target_id)}'
    elif project_id:
        subject = f'project {collapse_control_chars(project_id)}'
    else:
        subject = 'this project'

    if team_id:
        return (
            f'This key cannot access team {team_id} for {subject}. Use a key bound to '
            'that team (or one with team admin), then retry.'
        )

    return (
        f'This key cannot access the team scope of {subject}. Use a key bound to that '
        'team (or one with team admin), then retry.'
    )


def audit_needs_project_message() -> str:
    return 'engram_audit needs a project_id — pass project_id or connect a project.'


def audit_missing_capability_message() -> str:
    return (
        'This key cannot read audit events. Re-issue the API key with the audit:read '
        'capability from the Engram console, then retry.'
    )


def diff_unavailable_note(from_version: int, to_version: int) -> str:
    return f'(diff unavailable: version {from_version} or {to_version} not found)'


def _render_links_line(links_status: int | None, links_body: dict[str, Any] | None) -> str | None:
    if links_status is None:
        return None

    if links_status not in (200, 201):
        return (
            f'links: unavailable (HTTP {links_status}) — could not confirm links; '
            'this record may have links not shown here'
        )

    items = (links_body or {}).get('items')
    if not isinstance(items, list) or not items:
        return None

    parts: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        link_type = collapse_control_chars(item.get('link_type'))
        target = collapse_control_chars(item.get('target'))
        label = collapse_control_chars(item.get('label'))
        rendered = f'{link_type}: {target}'
        if label:
            rendered += f' ({label})'
        parts.append(rendered)

    return 'links: ' + '; '.join(parts)


def render_memory_get(
    memory_id: str,
    version_body: dict[str, Any],
    links_status: int | None,
    links_body: dict[str, Any] | None,
    diff: dict[str, Any] | None,
    diff_error: str | None,
) -> str:
    items = version_body.get('items') or []
    current = items[0] if items else {}

    lines: list[str] = [
        f'memory_id={memory_id} current_version={current.get("version")}',
        MEMORY_GET_VALIDITY_NOTE,
        '',
        _text(current.get('body')),
        '',
    ]

    version_parts = [f'v{item.get("version")} ({item.get("created_at")})' for item in items]
    lines.append('versions: ' + ', '.join(version_parts))

    links_line = _render_links_line(links_status, links_body)
    if links_line is not None:
        lines.append(links_line)

    if diff is not None:
        from_slice = diff.get('from') or {}
        to_slice = diff.get('to') or {}
        lines.append('')
        lines.append(f'diff v{from_slice.get("version")} -> v{to_slice.get("version")}')
        lines.append(f'--- v{from_slice.get("version")} ({from_slice.get("created_at")})')
        lines.append(_text(from_slice.get('body')))
        lines.append(f'--- v{to_slice.get("version")} ({to_slice.get("created_at")})')
        lines.append(_text(to_slice.get('body')))
    elif diff_error is not None:
        lines.append('')
        lines.append(diff_error)

    return '\n'.join(lines)


def audit_scope_header(target_id: str, target_type: str, limit: int) -> str:
    if not target_id:
        return f'project-wide audit events (most recent {limit})'

    safe_id = collapse_control_chars(target_id)
    safe_type = collapse_control_chars(target_type) or 'memory'
    if safe_type == 'memory':
        return (
            f'audit trace for memory {safe_id} (own events only: '
            'promotion/revise/refute/stale/restore/supersede/archive/candidate-merge-in/'
            'merge-as-source; result-side-of-direct-merge, decay, and link events not shown)'
        )

    return f'audit trace for {safe_type} {safe_id}'


def _render_audit_event(item: dict[str, Any]) -> str:
    event_type = collapse_control_chars(item.get('event_type'))
    metadata = item.get('metadata')
    metadata = metadata if isinstance(metadata, dict) else {}
    transition_type = collapse_control_chars(metadata.get('transition_type'))
    reason = collapse_control_chars(metadata.get('reason'))
    actor_id = collapse_control_chars(item.get('actor_id'))
    actor_display = collapse_control_chars(item.get('actor_display'))
    result = collapse_control_chars(item.get('result'))
    target_id = collapse_control_chars(item.get('target_id'))
    target_display = collapse_control_chars(item.get('target_display'))
    target_type = collapse_control_chars(item.get('target_type'))
    capability = collapse_control_chars(item.get('capability'))
    created_at = collapse_control_chars(item.get('created_at'))

    line = f'{created_at} {event_type}'
    if transition_type:
        line += f' ({transition_type})'
    line += f' actor={actor_id}'
    if actor_display:
        line += f' ({actor_display})'
    line += f' result={result}'
    line += f' target={target_id}'
    if target_display:
        line += f' ({target_display})'
    line += f' target_type={target_type} capability={capability}'
    if reason:
        line += f' reason={reason}'

    return line


def audit_truncation_note(count: int, limit: int) -> str | None:
    if count <= limit:
        return None

    omitted = count - limit

    return (
        f'(showing most recent {limit} of {count} events; {omitted} older omitted — '
        'narrow with since/until/event_type)'
    )


def render_audit(
    target_id: str,
    target_type: str,
    limit: int,
    body: dict[str, Any],
) -> str:
    header = audit_scope_header(target_id, target_type, limit)
    items = body.get('items')
    if not isinstance(items, list) or not items:
        return header + '\nNo audit events found.'

    lines = [header]
    for item in items:
        if not isinstance(item, dict):
            continue
        lines.append(_render_audit_event(item))

    count = body.get('count')
    if isinstance(count, int):
        note = audit_truncation_note(count, limit)
        if note is not None:
            lines.append(note)

    return '\n'.join(lines)


@dataclass(frozen=True)
class ReadScope:
    server_url: str
    api_key: str
    project_id: str = ''
    repository_url: str = ''
    team_id: str = ''


@dataclass(frozen=True)
class ReadError:
    code: str
    message: str = ''
    status: int = 0
    body: dict[str, Any] | None = None


@dataclass(frozen=True)
class ReadOutcome:
    text: str | None = None
    error: ReadError | None = None


def scope_params(scope: ReadScope) -> dict[str, str]:
    params: dict[str, str] = {}
    if scope.project_id:
        params['project_id'] = scope.project_id
    elif scope.repository_url:
        params['repository_url'] = scope.repository_url
    if scope.team_id:
        params['team_id'] = scope.team_id

    return params


def resolve_audit_target(target_id_arg: str, memory_id_arg: str, target_type_arg: str) -> tuple[str, str]:
    target_id = target_id_arg or memory_id_arg
    target_type = ''
    if target_id:
        target_type = target_type_arg or 'memory'

    return target_id, target_type


def _body_code(body: dict[str, Any] | None) -> str:
    if isinstance(body, dict):
        value = body.get('code')
        if isinstance(value, str):
            return value

    return ''


def _memory_read_denial(
    status: int,
    body: dict[str, Any] | None,
    scope: ReadScope,
    memory_id: str,
) -> ReadError | None:
    if status != 403:
        return None

    code = _body_code(body)
    if code == 'missing_capability':
        return ReadError('missing_capability', memory_read_missing_capability_message())

    if code == 'project_scope_denied':
        return ReadError('project_scope_denied', project_scope_denied_message(scope.project_id, scope.repository_url))

    if code == 'team_scope_denied':
        return ReadError('team_scope_denied', team_scope_denied_message(scope.team_id, memory_id))

    return None


def fetch_memory_get(
    transport: Transport,
    scope: ReadScope,
    memory_id: str,
    from_version: int,
    to_version: int,
) -> ReadOutcome:
    params = scope_params(scope)
    version_status, version_body = get_json(
        transport=transport,
        server_url=scope.server_url,
        path=f'/v1/memories/{memory_id}/version',
        api_key=scope.api_key,
        params=dict(params),
    )
    if version_status != 200:
        denial = _memory_read_denial(version_status, version_body, scope, memory_id)
        if denial is not None:
            return ReadOutcome(error=denial)

        return ReadOutcome(error=ReadError('http_error', status=version_status, body=version_body))

    items = version_body.get('items')
    if not isinstance(items, list) or not items:
        return ReadOutcome(error=ReadError('memory_not_found', memory_not_found_message(memory_id)))

    links_status, links_body = get_json(
        transport=transport,
        server_url=scope.server_url,
        path=f'/v1/memories/{memory_id}/links',
        api_key=scope.api_key,
        params=dict(params),
    )

    diff: dict[str, Any] | None = None
    diff_error: str | None = None
    if from_version >= 1 and to_version >= 1:
        diff_params = dict(params)
        diff_params['from_version'] = str(from_version)
        diff_params['to_version'] = str(to_version)
        diff_status, diff_body = get_json(
            transport=transport,
            server_url=scope.server_url,
            path=f'/v1/memories/{memory_id}/diff',
            api_key=scope.api_key,
            params=diff_params,
        )
        if diff_status == 200:
            diff = diff_body
        elif diff_status == 404:
            diff_error = diff_unavailable_note(from_version, to_version)
        else:
            return ReadOutcome(error=ReadError('http_error', status=diff_status, body=diff_body))

    text = render_memory_get(memory_id, version_body, links_status, links_body, diff, diff_error)

    return ReadOutcome(text=text)


def fetch_audit(
    transport: Transport,
    scope: ReadScope,
    target_id: str,
    target_type: str,
    limit: int,
    filters: dict[str, str],
) -> ReadOutcome:
    params: dict[str, str] = {
        'project_id': scope.project_id,
        'ordering': '-created_at',
        'limit': str(limit),
    }
    if target_id:
        params['target_id'] = target_id
        params['target_type'] = target_type
    for key, value in filters.items():
        if value:
            params[key] = value
    if scope.team_id:
        params['team_id'] = scope.team_id

    status, body = get_json(
        transport=transport,
        server_url=scope.server_url,
        path='/v1/inspection/audit-events',
        api_key=scope.api_key,
        params=params,
    )
    if status == 403:
        code = _body_code(body)
        if code == 'missing_capability':
            return ReadOutcome(error=ReadError('missing_capability', audit_missing_capability_message()))

        if code == 'project_scope_denied':
            return ReadOutcome(
                error=ReadError(
                    'project_scope_denied',
                    project_scope_denied_message(scope.project_id, scope.repository_url),
                ),
            )

        if code == 'team_scope_denied':
            return ReadOutcome(
                error=ReadError(
                    'team_scope_denied',
                    audit_team_scope_denied_message(scope.team_id, target_id, target_type, scope.project_id),
                ),
            )

    if status != 200:
        return ReadOutcome(error=ReadError('http_error', status=status, body=body))

    text = render_audit(target_id, target_type, limit, body)

    return ReadOutcome(text=text)
