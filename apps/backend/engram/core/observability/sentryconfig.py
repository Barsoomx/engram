import json
import os
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode, urlparse

import sentry_sdk

from engram.core.redaction import redact_value

BeforeSendFn = Callable[[dict[str, Any], Any], dict[str, Any] | None]

SENTRY_DSN = os.environ.get('SENTRY_DSN')
SENTRY_ENV = os.environ.get('SENTRY_ENV')
EVENT_LEVEL = 40

SENTRY_ORG = os.environ.get('SENTRY_ORG', 'engram')
SENTRY_BASE_URL = os.environ.get('SENTRY_BASE_URL', 'https://sentry.example.com')
SHORT_LOGS_BASE = os.environ.get('LOGS_SHORT_BASE_URL', 'https://api.engram.local')
GRAFANA_BASE_URL = os.environ.get('GRAFANA_BASE_URL', 'https://grafana.engram.local')
GRAFANA_LOKI_DATASOURCE_UID = os.environ.get('GRAFANA_LOKI_DATASOURCE_UID', 'engram-loki')


def _get_namespace() -> str:
    mapping = {
        'production': 'engram',
        'staging': 'engram-staging',
    }
    return os.environ.get('NAMESPACE') or mapping.get(SENTRY_ENV, 'engram')


def grafana_logs_link(trace_id: str) -> str:
    namespace = _get_namespace()
    ds_uid = GRAFANA_LOKI_DATASOURCE_UID
    expr = f'{{namespace="{namespace}"}} |= `{trace_id}` | json | __error__=``'

    panes = {
        'vuh': {
            'datasource': ds_uid,
            'queries': [
                {
                    'refId': 'A',
                    'expr': expr,
                    'queryType': 'range',
                    'datasource': {'type': 'loki', 'uid': ds_uid},
                    'editorMode': 'builder',
                },
            ],
            'range': {'from': 'now-1h', 'to': 'now'},
        },
    }

    params = urlencode(
        {
            'schemaVersion': '1',
            'panes': json.dumps(panes, separators=(',', ':')),
            'orgId': '1',
        },
    )

    return f'{GRAFANA_BASE_URL}/explore?{params}'


def short_logs_link(trace_id: str) -> str:
    return f'{SHORT_LOGS_BASE}/lk/shorter/logs/{trace_id}'


def sentry_trace_link(trace_id: str) -> str:
    return f'{SENTRY_BASE_URL}/organizations/{SENTRY_ORG}/performance/trace/{trace_id}'


def is_healthcheck(event: dict[str, Any]) -> bool:
    if url_string := event.get('request', {}).get('url'):
        parsed_url = urlparse(url_string)
        return parsed_url.path.startswith('/-/')

    return False


def is_request_finished(event: dict[str, Any]) -> bool:
    message = event.get('message') or event.get('logentry', {}).get('message')
    return message == 'request_finished'


def redact_sentry_event(event: dict[str, Any]) -> dict[str, Any]:
    result = redact_value(event).value
    if isinstance(result, dict):
        return result

    return event


def create_before_send(sentry_tags: dict[str, str] | None = None) -> BeforeSendFn:
    tags = sentry_tags or {}

    def before_send(event: dict[str, Any], _: Any) -> dict[str, Any] | None:
        if is_request_finished(event):
            return None

        if url := event.get('request', {}).get('url'):
            if 'admin' in url:
                event.setdefault('tags', {}).update({'admin': True})

        if tags:
            event.setdefault('tags', {}).update(tags)

        trace_id = event.get('contexts', {}).get('trace', {}).get('trace_id')
        if trace_id:
            event.setdefault('extra', {}).update(
                {
                    'logs_link_short': short_logs_link(trace_id),
                    'logs_link_grafana': grafana_logs_link(trace_id),
                    'trace_id': trace_id,
                },
            )

        return redact_sentry_event(event)

    return before_send


DEFAULT_SAMPLE_RATE = float(os.environ.get('SENTRY_TRACES_SAMPLE_RATE', '0.1'))

_DROP_TRANSACTION_NAME_PREFIXES = ('engram.core.tasks.account_consistency',)


def traces_sampler(sampling_context: dict) -> float:
    name = sampling_context.get('transaction_context', {}).get('name', '') or ''

    if any(name.startswith(prefix) for prefix in _DROP_TRANSACTION_NAME_PREFIXES):
        return 0.0

    parent = sampling_context.get('parent_sampled')
    if parent is not None:
        return 1.0 if parent else 0.0

    return 1.0


def _is_important_transaction(event: dict[str, Any]) -> bool:
    tags = event.get('tags', {})
    return 'transaction_token' in tags or 'transaction_external_id' in tags


def _should_send_transaction(event: dict[str, Any]) -> bool:
    if _is_important_transaction(event):
        return True

    trace_ctx = event.get('contexts', {}).get('trace', {})
    dsc = trace_ctx.get('dynamic_sampling_context', {})

    if dsc.get('sampled') == 'true':
        return True

    sample_rand = dsc.get('sample_rand')
    if sample_rand is not None:
        return float(sample_rand) < DEFAULT_SAMPLE_RATE

    return True


def create_before_send_transaction(sentry_tags: dict[str, str] | None = None) -> BeforeSendFn:
    before_send = create_before_send(sentry_tags)

    def before_send_transaction(event: dict[str, Any], hint: Any) -> dict[str, Any] | None:
        if is_healthcheck(event):
            return None

        if not _should_send_transaction(event):
            return None

        return before_send(event, hint)

    return before_send_transaction


def propagate_sentry_tracing() -> dict[str, Any]:
    scope = sentry_sdk.get_current_scope()
    sentry_trace_id, sentry_parent_span_id, baggage = None, None, None
    if scope and scope.transaction:
        sentry_trace_id = scope.transaction.trace_id
        sentry_parent_span_id = scope.transaction.span_id
        baggage = scope.transaction.get_baggage()

    return {
        'trace_id': sentry_trace_id,
        'parent_span_id': sentry_parent_span_id,
        'parent_sampled': True if sentry_parent_span_id else None,
        'baggage': baggage,
    }
