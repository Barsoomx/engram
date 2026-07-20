from __future__ import annotations

import re
from typing import Any

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


def project_scope_denied_message(project_id: str) -> str:
    return (
        f'This key cannot resolve project {project_id}. Use a project-bound key, or the '
        'projects:agent key from the Connect-agent modal, then retry.'
    )


def team_scope_denied_message(team_id: str, memory_id: str) -> str:
    return (
        f'This key cannot access team {team_id} for memory {memory_id}. Use a key bound to '
        'that team (or one with team admin), then retry.'
    )


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
