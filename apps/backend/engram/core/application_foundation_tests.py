from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import Mock, patch

import pytest
from django.conf import settings
from django.http import JsonResponse
from django.test import RequestFactory
from rest_framework import status
from rest_framework.exceptions import ValidationError as DRFValidationError

from engram.core.domain import BaseUseCase, DomainError, DomainEvent, EventStore, UseCaseTransactional
from engram.core.domain.event_dispatcher import get_dispatcher
from engram.core.domain.usecases.base import BaseUseCaseInputDTO, BaseUseCaseOutputDTO
from engram.core.middlewares.domain_exception import ExceptionHandlingMiddleware
from engram.core.middlewares.drf_exception_handler import build_domain_error_payload, custom_exception_handler
from engram.core.middlewares.request_response_logging import (
    ApiRequestResponseLoggingMiddleware,
    _extract_request_body,
    _extract_request_headers,
    _extract_response_body,
    _extract_response_headers,
    _truncate,
)
from engram.core.observability.sentryconfig import (
    create_before_send,
    create_before_send_transaction,
    grafana_logs_link,
    is_healthcheck,
    short_logs_link,
)
from engram.core.redaction import REDACTED_VALUE


class SampleInput(BaseUseCaseInputDTO):
    value: int
    arbitrary: object | None = None


class SampleOutput(BaseUseCaseOutputDTO):
    value: int


class SampleEvent(DomainEvent):
    value: int


class RecordingTransaction:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def __enter__(self) -> None:
        self._events.append('enter')

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        self._events.append('exit')


class AddOneUseCase(BaseUseCase[SampleInput, SampleOutput]):
    def _execute(self, input_dto: SampleInput | None) -> SampleOutput:
        assert input_dto is not None
        self._event_store.add_event(SampleEvent(value=input_dto.value))

        return SampleOutput(value=input_dto.value + 1)


class TransactionalUseCase(UseCaseTransactional[SampleInput, SampleOutput]):
    def __init__(self, events: list[str]) -> None:
        super().__init__(user=None, transaction=RecordingTransaction(events))
        self._events = events

    def _pre_commit(self, input_dto: SampleInput | None) -> None:
        self._events.append(f'pre:{input_dto.value if input_dto else "none"}')

    def _execute(self, input_dto: SampleInput | None) -> SampleOutput:
        assert input_dto is not None
        self._events.append('execute')
        self._event_store.add_event(SampleEvent(value=input_dto.value))

        return SampleOutput(value=input_dto.value + 1)

    def _post_commit(self, input_dto: SampleInput | None, output_dto: SampleOutput | None) -> None:
        self._events.append(f'post:{output_dto.value if output_dto else "none"}')
        super()._post_commit(input_dto=input_dto, output_dto=output_dto)


@pytest.fixture(autouse=True)
def f_clear_dispatcher_handlers() -> Iterator[None]:
    dispatcher = get_dispatcher()
    dispatcher._handlers.clear()

    yield

    dispatcher._handlers.clear()


def test_domain_error_metadata_and_status_normalization() -> None:
    exc = DomainError(
        'memory conflict',
        related_param='memory_id',
        error_code='memory_conflict',
        status_code=status.HTTP_409_CONFLICT,
    )

    assert str(exc) == 'memory conflict'
    assert exc.detail == 'memory conflict'
    assert exc.related_param == 'memory_id'
    assert exc.error_code == 'memory_conflict'
    assert exc.status_code == status.HTTP_409_CONFLICT

    invalid = DomainError('bad status', status_code=status.HTTP_200_OK)

    assert invalid.status_code == status.HTTP_400_BAD_REQUEST


def test_domain_error_subclasses_accept_error_code_and_status_code_kwargs() -> None:
    class LegacyDomainError(DomainError):
        def __init__(self, detail: str, marker: str) -> None:
            super().__init__(detail)
            self.marker = marker

    exc = LegacyDomainError(
        'legacy',
        'marker',
        error_code='legacy_error',
        status_code=status.HTTP_403_FORBIDDEN,
    )

    assert exc.marker == 'marker'
    assert exc.error_code == 'legacy_error'
    assert exc.status_code == status.HTTP_403_FORBIDDEN


def test_domain_error_payload_matches_drf_handler_and_middleware() -> None:
    request = RequestFactory().get('/v1/example/')
    exc = DomainError(
        'processing unavailable',
        error_code='processing_unavailable',
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
    )

    payload = build_domain_error_payload(exc)
    drf_response = custom_exception_handler(exc, {'request': request})
    middleware_response = ExceptionHandlingMiddleware(lambda request: request).process_exception(request, exc)

    assert payload == {
        'detail': 'processing unavailable',
        'error_code': 'processing_unavailable',
    }
    assert drf_response is not None
    assert drf_response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert drf_response.data == payload
    assert middleware_response is not None
    assert middleware_response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE


def test_drf_validation_error_preserves_field_payload() -> None:
    request = RequestFactory().post('/v1/context/session-start')
    exc = DRFValidationError({'query': {'code': ['context_query_too_large']}})

    response = custom_exception_handler(exc, {'request': request})

    assert response is not None
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.data['query']['code'] == ['context_query_too_large']


def test_base_use_case_uses_pydantic_dtos_and_dispatches_events() -> None:
    handled: list[SampleEvent] = []
    dispatcher = get_dispatcher()
    dispatcher.add_handler(SampleEvent, handled.append)

    output = AddOneUseCase().execute(SampleInput(value=41, arbitrary=object()))

    assert output == SampleOutput(value=42)
    assert [event.value for event in handled] == [41]


def test_transactional_use_case_runs_pre_transaction_execute_post_and_dispatch() -> None:
    events: list[str] = []
    handled: list[SampleEvent] = []
    get_dispatcher().add_handler(SampleEvent, handled.append)

    output = TransactionalUseCase(events).execute(SampleInput(value=9))

    assert output == SampleOutput(value=10)
    assert events == ['pre:9', 'enter', 'execute', 'exit', 'post:10']
    assert [event.value for event in handled] == [9]


@patch('engram.core.domain.usecases.base.logger.exception')
def test_base_use_case_logs_domain_error_only_when_not_skipped(m_logger_exception: Mock) -> None:
    class LoudDomainError(DomainError):
        SKIP_LOGGING = False

    class FailingUseCase(BaseUseCase[SampleInput, SampleOutput]):
        def _execute(self, input_dto: SampleInput | None) -> SampleOutput:
            raise LoudDomainError('loud')

    with pytest.raises(LoudDomainError):
        FailingUseCase().execute(SampleInput(value=1))

    m_logger_exception.assert_called_once()


def test_event_store_returns_and_clears_events() -> None:
    store = EventStore()
    event = SampleEvent(value=1)

    store.add_event(event)

    assert store.get_events() == [event]

    store.clear_events()

    assert store.get_events() == []


def test_observability_links_and_filters_match_reference_backend_contract() -> None:
    trace_id = 'abc123def456'

    assert short_logs_link(trace_id).endswith(f'/lk/shorter/logs/{trace_id}')
    assert trace_id in grafana_logs_link(trace_id)
    assert is_healthcheck({'request': {'url': 'https://example.com/-/readyz/'}}) is True
    assert is_healthcheck({'request': {'url': 'https://example.com/v1/context'}}) is False

    before_send = create_before_send({'app_name': 'engram'})
    event = {
        'request': {'url': 'https://example.com/admin/users'},
        'tags': {},
        'contexts': {'trace': {'trace_id': trace_id}},
    }

    result = before_send(event, None)

    assert result is not None
    assert result['tags']['admin'] is True
    assert result['tags']['app_name'] == 'engram'
    assert result['extra']['trace_id'] == trace_id

    assert before_send({'message': 'request_finished'}, None) is None


def test_observability_redacts_sentry_event_secrets() -> None:
    api_key = 'egk_sentry_secret_0123456789abcdefghijklmnopqrstuvwxyz'
    provider_key = 'sk-test_observability_secret_1234567890'
    before_send = create_before_send({'app_name': 'engram'})

    result = before_send(
        {
            'message': f'provider failed with {provider_key}',
            'request': {
                'url': 'https://example.com/v1/context',
                'headers': {'Authorization': f'Bearer {api_key}'},
                'data': {'providerApiKey': provider_key},
            },
            'extra': {'payload': {'token': api_key}},
        },
        None,
    )

    assert result is not None
    assert api_key not in str(result)
    assert provider_key not in str(result)
    assert result['request']['headers']['Authorization'] == REDACTED_VALUE
    assert result['request']['data']['providerApiKey'] == REDACTED_VALUE
    assert result['extra']['payload']['token'] == REDACTED_VALUE


def test_before_send_transaction_drops_healthcheck_transactions() -> None:
    before_send_transaction = create_before_send_transaction()

    assert before_send_transaction({'request': {'url': 'https://example.com/-/healthz/'}}, None) is None


def test_request_response_logging_truncates_large_bodies() -> None:
    assert _truncate('x' * 5001) == ('x' * 5000) + '...'


def test_request_response_logging_redacts_headers_and_body() -> None:
    api_key = 'egk_log_secret_0123456789abcdefghijklmnopqrstuvwxyz'
    provider_key = 'sk-test_logging_secret_1234567890'
    request = RequestFactory().post(
        '/api/context',
        data={'providerApiKey': provider_key, 'note': f'token {api_key}'},
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {api_key}',
    )
    response = JsonResponse({'token': api_key, 'status': 'ok'})
    response['X-Api-Key'] = provider_key

    request_headers = _extract_request_headers(request)
    request_body = _extract_request_body(request)
    response_headers = _extract_response_headers(response)
    response_body = _extract_response_body(response)

    assert api_key not in str(request_headers)
    assert api_key not in request_body
    assert api_key not in response_body
    assert provider_key not in str(response_headers)
    assert provider_key not in request_body
    assert request_headers['Authorization'] == REDACTED_VALUE
    assert response_headers['X-Api-Key'] == REDACTED_VALUE


def test_request_response_logging_redacts_url_query_secrets() -> None:
    api_key = 'egk_url_secret_0123456789abcdefghijklmnopqrstuvwxyz'
    request = RequestFactory().get(f'/api/context?api_key={api_key}&mode=test')
    logger = Mock()
    middleware = ApiRequestResponseLoggingMiddleware(lambda request: request)
    middleware.__dict__['_logger'] = logger

    middleware.process_request(request)

    logger.info.assert_called_once()
    logged_url = logger.info.call_args.kwargs['url']
    assert api_key not in logged_url
    assert f'api_key={REDACTED_VALUE}' in logged_url
    assert 'mode=test' in logged_url


def test_request_response_logging_middleware_is_installed() -> None:
    assert 'engram.core.middlewares.ApiRequestResponseLoggingMiddleware' in settings.MIDDLEWARE
