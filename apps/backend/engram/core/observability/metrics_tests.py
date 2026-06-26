from __future__ import annotations

import pytest
from django.test import Client

from engram.core.middlewares.metrics import (
    MetricsMiddleware,
    http_requests_total,
    status_class,
)
from engram.core.observability.metrics import Counter, render_prometheus


@pytest.fixture(autouse=True)
def f_reset_counters() -> None:
    http_requests_total.reset()

    yield

    http_requests_total.reset()


def test_counter_inc_accumulates_per_label_set() -> None:
    counter = Counter(
        name='test_counter',
        help_text='test counter',
        label_names=('method',),
    )

    counter.inc(method='get')
    counter.inc(method='get')
    counter.inc(method='post')

    assert counter.value(method='get') == 2.0
    assert counter.value(method='post') == 1.0


def test_counter_inc_rejects_wrong_label_keys() -> None:
    counter = Counter(
        name='test_counter',
        help_text='test counter',
        label_names=('method',),
    )

    with pytest.raises(ValueError):
        counter.inc(status='get')


def test_counter_inc_supports_custom_step() -> None:
    counter = Counter(
        name='test_counter',
        help_text='test counter',
        label_names=('method',),
    )

    counter.inc(method='get', value=5.0)

    assert counter.value(method='get') == 5.0


def test_render_prometheus_emits_help_type_and_metric_lines() -> None:
    counter = Counter(
        name='engram_http_requests_total',
        help_text='Total HTTP requests.',
        label_names=('view', 'status'),
    )

    counter.inc(view='healthz', status='2xx')
    counter.inc(view='healthz', status='2xx')
    counter.inc(view='context', status='5xx')

    output = render_prometheus([counter])

    assert '# HELP engram_http_requests_total Total HTTP requests.' in output
    assert '# TYPE engram_http_requests_total counter' in output
    assert 'engram_http_requests_total{status="2xx",view="healthz"} 2.0' in output
    assert 'engram_http_requests_total{status="5xx",view="context"} 1.0' in output


def test_render_prometheus_escapes_special_characters_in_label_values() -> None:
    counter = Counter(
        name='test_counter',
        help_text='test counter',
        label_names=('view',),
    )

    counter.inc(view='some "quoted" \\ path\nnewline')

    output = render_prometheus([counter])

    assert 'view="some \\"quoted\\" \\\\ path\\nnewline"' in output


def test_render_prometheus_empty_counters_returns_empty_string() -> None:
    assert render_prometheus([]) == ''


def test_status_class_groups_status_codes() -> None:
    assert status_class(200) == '2xx'
    assert status_class(201) == '2xx'
    assert status_class(301) == '3xx'
    assert status_class(404) == '4xx'
    assert status_class(500) == '5xx'


def test_metrics_endpoint_returns_text_plain_prometheus_format(client: Client) -> None:
    response = client.get('/-/metrics')

    assert response.status_code == 200
    assert response.headers['Content-Type'].startswith('text/plain')
    assert '# TYPE engram_http_requests_total counter' in response.content.decode('utf-8')


def test_metrics_endpoint_includes_http_requests_total_help_line(client: Client) -> None:
    client.get('/-/healthz/')
    response = client.get('/-/metrics')

    body = response.content.decode('utf-8')

    assert '# HELP engram_http_requests_total' in body
    assert 'engram_http_requests_total{' in body


@pytest.mark.django_db
def test_middleware_increments_counter_on_health_request(client: Client) -> None:
    response = client.get('/-/healthz/')

    assert response.status_code == 200

    assert http_requests_total.value(view='healthz', status='2xx') == 1.0


def test_middleware_uses_view_name_when_resolver_match_present() -> None:
    class _StubResponse:
        def __init__(self, status_code: int) -> None:
            self.status_code = status_code

    class _StubResolverMatch:
        view_name = 'context-task'
        url_name = 'context-task'

    class _StubRequest:
        def __init__(self) -> None:
            self.path = '/v1/context'
            self.resolver_match = _StubResolverMatch()

    def _fake_get_response(_request: _StubRequest) -> _StubResponse:
        return _StubResponse(200)

    http_requests_total.reset()
    middleware = MetricsMiddleware(_fake_get_response)

    response = middleware(_StubRequest())

    assert response.status_code == 200
    assert http_requests_total.value(view='context-task', status='2xx') == 1.0


def test_middleware_falls_back_to_path_when_resolver_match_missing() -> None:
    class _StubResponse:
        def __init__(self, status_code: int) -> None:
            self.status_code = status_code

    class _StubRequest:
        def __init__(self) -> None:
            self.path = '/v1/context'
            self.resolver_match = None

    def _fake_get_response(_request: _StubRequest) -> _StubResponse:
        return _StubResponse(500)

    http_requests_total.reset()
    middleware = MetricsMiddleware(_fake_get_response)

    middleware(_StubRequest())

    assert http_requests_total.value(view='unresolved', status='5xx') == 1.0
