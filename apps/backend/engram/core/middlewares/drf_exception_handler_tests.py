from __future__ import annotations

from django import db
from rest_framework.exceptions import ValidationError as DRFValidationError

from engram.core.middlewares.drf_exception_handler import (
    GENERIC_ERROR_DETAIL,
    custom_exception_handler,
)


def _context() -> dict[str, object]:
    return {'view': None, 'request': None}


def test_unexpected_exception_returns_generic_detail_without_echo() -> None:
    response = custom_exception_handler(RuntimeError('secret path /var/keys/provider'), _context())

    assert response is not None
    assert response.status_code == 500
    assert response.data['detail'] == GENERIC_ERROR_DETAIL
    assert 'secret path' not in str(response.data)


def test_value_error_message_is_not_echoed() -> None:
    response = custom_exception_handler(ValueError('/internal/service/token=abc'), _context())

    assert response is not None
    assert response.status_code == 500
    assert response.data['detail'] == GENERIC_ERROR_DETAIL
    assert 'token=abc' not in str(response.data)


def test_database_error_is_masked() -> None:
    response = custom_exception_handler(db.Error('duplicate key value violates unique constraint'), _context())

    assert response is not None
    assert response.status_code == 500
    assert 'duplicate key' not in str(response.data)


def test_drf_validation_error_detail_is_preserved() -> None:
    response = custom_exception_handler(DRFValidationError({'field': ['is required']}), _context())

    assert response is not None
    assert response.status_code == 400
    assert 'is required' in str(response.data)
