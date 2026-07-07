from __future__ import annotations

from engram.imports.serializers import MAX_REQUEST_BYTES
from engram.imports.views.support import request_too_large


class _StubRequest:
    def __init__(self, *, content_length: str | None, body: bytes) -> None:
        self.META = {} if content_length is None else {'CONTENT_LENGTH': content_length}
        self.body = body


def test_request_too_large_uses_declared_content_length() -> None:
    request = _StubRequest(content_length=str(MAX_REQUEST_BYTES + 1), body=b'')

    assert request_too_large(request) is True


def test_request_too_large_checks_actual_body_without_content_length() -> None:
    request = _StubRequest(content_length=None, body=b'x' * (MAX_REQUEST_BYTES + 1))

    assert request_too_large(request) is True


def test_request_too_large_checks_actual_body_with_understated_content_length() -> None:
    request = _StubRequest(content_length='10', body=b'x' * (MAX_REQUEST_BYTES + 1))

    assert request_too_large(request) is True


def test_request_too_large_accepts_small_body() -> None:
    request = _StubRequest(content_length=None, body=b'{"seq": 0}')

    assert request_too_large(request) is False
