from functools import wraps
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class DomainError(Exception):
    SKIP_LOGGING: bool = True
    default_error_code: str | None = None
    default_status_code: int = 400

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        subclass_init = cls.__dict__.get('__init__')
        if subclass_init is None:
            return

        @wraps(subclass_init)
        def wrapped_init(
            self: 'DomainError',
            *args: Any,
            error_code: str | None = None,
            status_code: int | None = None,
            **init_kwargs: Any,
        ) -> None:
            subclass_init(self, *args, **init_kwargs)
            if error_code is not None:
                self.error_code = error_code
            elif not hasattr(self, 'error_code'):
                self.error_code = cls.default_error_code

            if status_code is not None:
                self.status_code = self._normalize_status_code(status_code)
            elif not hasattr(self, 'status_code'):
                self.status_code = self._normalize_status_code(cls.default_status_code)

        cls.__init__ = wrapped_init  # type: ignore[method-assign]

    def __init__(
        self,
        detail: str,
        related_param: str = '',
        *,
        error_code: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.related_param = related_param
        self.error_code = self.default_error_code if error_code is None else error_code
        self.status_code = self._normalize_status_code(
            self.default_status_code if status_code is None else status_code,
        )

    @staticmethod
    def _normalize_status_code(status_code: int) -> int:
        if 400 <= status_code < 600:
            return status_code
        logger.warning(
            'domain_error_status_code_coerced',
            original_status_code=status_code,
            fallback_status_code=400,
        )
        return 400
