from __future__ import annotations

from engram.core.domain.usecases.errors import DomainError


class LastOwnerError(DomainError):
    default_error_code = 'last_owner'
    default_status_code = 409
