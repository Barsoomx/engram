from typing import Any

import psycopg
import redis
import structlog
from django import db
from pydantic import ValidationError
from rest_framework import status
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.response import Response
from rest_framework.views import exception_handler

from engram.core.domain import DomainError

logger = structlog.getLogger('rest_logger')

GENERIC_ERROR_DETAIL = 'internal server error'

MANAGER_DOMAIN_EXTRA_FIELDS = {
    'missing_capability': str,
    'limit': int,
    'limit_days': int,
    'scope': str,
    'current_access_version': int,
    'active_import_id': str,
}


def _translate_error(err: dict[str, Any]) -> str:
    error_messages = {
        'missing': 'Обязательное поле',
        'string_type': 'Значение должно быть строкой',
        'float_type': 'Значение должно быть числом',
        'float_parsing': 'Некорректное число',
        'int_parsing': 'Некорректное целое число',
        'greater_than': 'Значение должно быть больше {gt}',
        'greater_than_equal': 'Значение должно быть не меньше {ge}',
        'less_than': 'Значение должно быть меньше {lt}',
        'less_than_equal': 'Значение должно быть не больше {le}',
    }

    msg = error_messages.get(err.get('type'))
    if msg:
        ctx = err.get('ctx') or {}
        for k, v in ctx.items():
            if isinstance(v, float) and v.is_integer():
                ctx[k] = int(v)
        msg = msg.format(**ctx)
    else:
        msg = err.get('msg', '')
        if isinstance(msg, str):
            prefixes = {
                'value_error': 'Value error, ',
                'assertion_error': 'Assertion failed, ',
            }
            prefix = prefixes.get(err.get('type'))
            if prefix and msg.startswith(prefix):
                msg = msg[len(prefix) :]
    return msg


def _get_error_fields(exc: ValidationError) -> dict[str, str]:
    fields: dict[str, list[str]] = {}
    for err in exc.errors():
        loc = '.'.join(str(part) for part in err.get('loc', []))
        msg = _translate_error(err)
        fields.setdefault(loc, []).append(msg)
    return {loc: '; '.join(msgs) for loc, msgs in fields.items()}


def _format_drf_validation_error_detail(detail: Any) -> str:
    def _normalize(d: Any) -> str:
        if isinstance(d, dict):
            parts: list[str] = []
            for k, v in d.items():
                if isinstance(v, list | tuple):
                    msgs = ', '.join(str(item) for item in v)
                else:
                    msgs = str(v)
                parts.append(f'{k}: {msgs}')
            return '; '.join(parts)
        if isinstance(d, list | tuple):
            return '; '.join(str(item) for item in d)
        return str(d)

    msg = _normalize(detail)
    return msg or 'Неправильно заполнена форма'


def _get_error_detail(exc: Exception) -> str:
    if isinstance(exc, str):
        return exc
    if isinstance(exc, ValidationError):
        return 'Неправильно заполнена форма'
    if isinstance(exc, DRFValidationError):
        detail = getattr(exc, 'detail', None) or getattr(exc, 'args', [None])[0]
        return _format_drf_validation_error_detail(detail)
    if isinstance(exc, db.Error) or isinstance(exc, psycopg.Error) or isinstance(exc, redis.RedisError):
        return 'Произошла неизвестная ошибка. Попробуйте еще раз или обратитесь в поддержку.'

    if hasattr(exc, 'args') and exc.args:
        if len(exc.args) == 1:
            arg = exc.args[0]
            if isinstance(arg, dict):
                return '; '.join(f'{k}: {v}' for k, v in arg.items())
            elif isinstance(arg, list | tuple):
                return ', '.join(str(item) for item in arg)
            else:
                return str(arg)
        else:
            return '; '.join(str(arg) for arg in exc.args)

    return str(exc)


def build_domain_error_payload(exc: DomainError) -> dict[str, Any]:
    error_code = getattr(exc, 'error_code', None)

    response_data: dict[str, Any] = {'detail': _get_error_detail(exc)}
    if error_code is not None:
        response_data['error_code'] = error_code
        response_data['code'] = error_code
    for field_name, field_type in MANAGER_DOMAIN_EXTRA_FIELDS.items():
        value = getattr(exc, field_name, None)
        if isinstance(value, field_type):
            response_data[field_name] = value
    return response_data


def _build_non_domain_response_data(
    *,
    exc: Exception,
    response: Response,
    detail: str,
    field_errors: dict[str, str] | None,
) -> dict[str, Any]:
    if isinstance(exc, DRFValidationError):
        return response.data if isinstance(response.data, dict) else {'detail': detail}

    response_data: dict[str, Any] = {'detail': detail}
    if field_errors:
        response_data['fields'] = field_errors
    return response_data


def _build_fallback_response_data(
    *,
    detail: str,
    field_errors: dict[str, str] | None,
) -> dict[str, Any]:
    return {
        'detail': detail,
        **({'fields': field_errors} if field_errors else {}),
    }


def custom_exception_handler(exc: Exception, context: dict[str, Any]) -> Response | None:
    response = exception_handler(exc, context)
    if response is None and isinstance(exc, DomainError):
        response = Response(status=exc.status_code)

    view = context.get('view')
    view_name = view.__class__.__name__ if view else 'UnknownView'

    field_errors: dict[str, str] | None = None
    if response is None and isinstance(exc, ValidationError):
        response = Response(status=status.HTTP_400_BAD_REQUEST)
        field_errors = _get_error_fields(exc)

    if isinstance(exc, DomainError):
        pass
    elif response is None or response.status_code >= 500:
        logger.exception(exc, view_name=view_name, request=context.get('request'))
    else:
        logger.warning(exc, view_name=view_name)

    detail = _get_error_detail(exc)

    if response is not None:
        if isinstance(exc, DomainError):
            response.data = build_domain_error_payload(exc)
        else:
            response.data = _build_non_domain_response_data(
                exc=exc,
                response=response,
                detail=detail,
                field_errors=field_errors,
            )
    else:
        response = Response(
            _build_fallback_response_data(
                detail=GENERIC_ERROR_DETAIL,
                field_errors=field_errors,
            ),
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    return response
