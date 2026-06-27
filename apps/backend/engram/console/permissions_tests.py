from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from engram.console.permissions import RequireCapability


def _request_with_caps(caps: tuple[str, ...]) -> MagicMock:
    request = MagicMock()

    request.effective_scope = SimpleNamespace(capabilities=caps)

    return request


def test_require_capability_grants_wildcard() -> None:
    permission = RequireCapability('api_keys:read')

    assert permission.has_object_permission_override(_request_with_caps(('api_keys:*',))) is True


def test_require_capability_grants_exact() -> None:
    permission = RequireCapability('teams:admin')

    assert permission.has_object_permission_override(_request_with_caps(('teams:admin',))) is True


def test_require_capability_denies_missing() -> None:
    permission = RequireCapability('members:admin')

    assert permission.has_object_permission_override(_request_with_caps(('members:read',))) is False


def test_require_capability_denies_when_scope_absent() -> None:
    permission = RequireCapability('members:admin')

    request = SimpleNamespace()

    assert permission.has_object_permission_override(request) is False


def test_require_capability_has_permission_delegates() -> None:
    permission = RequireCapability('teams:admin')

    assert permission.has_permission(_request_with_caps(('teams:admin',)), view=MagicMock()) is True
