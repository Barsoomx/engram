from __future__ import annotations

from rest_framework import status

from engram.core.domain.usecases.errors import DomainError

ERROR_STATUS = {
    'policy_scope_mismatch': status.HTTP_400_BAD_REQUEST,
    'team_required': status.HTTP_400_BAD_REQUEST,
    'model_policy_not_found': status.HTTP_404_NOT_FOUND,
    'secret_scope_denied': status.HTTP_403_FORBIDDEN,
}


class ModelPolicyError(DomainError):
    def __init__(self, code: str, message: str, *, retryable: bool = False, http_status: int | None = None) -> None:
        super().__init__(
            message,
            error_code=code,
            status_code=ERROR_STATUS.get(code, status.HTTP_400_BAD_REQUEST),
        )
        self.code = code
        self.retryable = retryable
        self.http_status = http_status


class ProviderSecretError(DomainError):
    pass
