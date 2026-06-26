from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import structlog
from django.http import HttpRequest, HttpResponse
from django.utils.deprecation import MiddlewareMixin
from django.utils.functional import cached_property

from engram.core.redaction import REDACTED_VALUE, is_sensitive_key, redact_value

_MAX_BODY_LENGTH = 5000


def _truncate(value: str) -> str:
    if len(value) <= _MAX_BODY_LENGTH:
        return value
    return value[:_MAX_BODY_LENGTH] + '...'


def _redact_mapping(value: dict[str, Any]) -> dict[str, Any]:
    result = redact_value(value).value
    if isinstance(result, dict):
        return result

    return {}


def _redact_body(value: str) -> str:
    result = redact_value(value).value
    if isinstance(result, str):
        return result

    return str(result)


def _redact_url(value: str) -> str:
    parsed = urlsplit(value)
    if not parsed.query:
        return _redact_body(value)

    query_pairs = []
    for key, item in parse_qsl(parsed.query, keep_blank_values=True):
        if is_sensitive_key(key):
            query_pairs.append((key, REDACTED_VALUE))
            continue

        redacted_item = redact_value(item).value
        query_pairs.append((key, redacted_item if isinstance(redacted_item, str) else str(redacted_item)))

    query = urlencode(query_pairs, doseq=True, safe='[]')

    return _redact_body(urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, parsed.fragment)))


def _extract_request_headers(request: HttpRequest) -> dict[str, Any]:
    return _redact_mapping(dict(request.headers))


def _extract_request_body(request: HttpRequest) -> str:
    try:
        raw_body = request.body
    except Exception:
        return '<unavailable>'
    if not raw_body:
        return ''
    if isinstance(raw_body, bytes):
        body_text = raw_body.decode('utf-8', errors='replace')
    else:
        body_text = str(raw_body)
    return _truncate(_redact_body(body_text))


def _extract_response_headers(response: HttpResponse) -> dict[str, Any]:
    try:
        return _redact_mapping(dict(response.items()))
    except Exception:
        return {}


def _extract_response_body(response: HttpResponse) -> str:
    if getattr(response, 'streaming', False):
        return '<streaming>'
    try:
        content = response.content
    except Exception:
        return '<unavailable>'
    if not content:
        return ''
    if isinstance(content, bytes):
        body_text = content.decode('utf-8', errors='replace')
    else:
        body_text = str(content)
    return _truncate(_redact_body(body_text))


class ApiRequestResponseLoggingMiddleware(MiddlewareMixin):
    path_prefix: str = '/api/'
    logger_name: str = 'api.requests'
    log_prefix: str = 'api'

    @cached_property
    def _logger(self) -> structlog.BoundLogger:
        return structlog.get_logger(self.logger_name)

    def _should_log(self, request: HttpRequest) -> bool:
        return request.path.startswith(self.path_prefix)

    def process_request(self, request: HttpRequest) -> None:
        if not self._should_log(request):
            return None
        self._logger.info(
            f'{self.log_prefix}.request',
            method=request.method,
            url=_redact_url(request.build_absolute_uri()),
            headers=_extract_request_headers(request),
            body=_extract_request_body(request),
        )
        return None

    def process_response(self, request: HttpRequest, response: HttpResponse) -> HttpResponse:
        if self._should_log(request):
            self._logger.info(
                f'{self.log_prefix}.response',
                url=_redact_url(request.build_absolute_uri()),
                status_code=response.status_code,
                headers=_extract_response_headers(response),
                body=_extract_response_body(response),
            )
        return response

    def process_exception(self, request: HttpRequest, exception: Exception) -> None:
        if not self._should_log(request):
            return None
        self._logger.exception(
            f'{self.log_prefix}.exception',
            method=request.method,
            url=_redact_url(request.build_absolute_uri()),
            headers=_extract_request_headers(request),
            body=_extract_request_body(request),
            error=str(exception),
        )
        return None
