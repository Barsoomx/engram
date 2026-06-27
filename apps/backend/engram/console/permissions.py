from __future__ import annotations

from typing import Any

from rest_framework.permissions import BasePermission

from engram.console.services import audit_admin_action


class RequireCapability(BasePermission):
    def __init__(self, code: str) -> None:
        self.code = code

    def has_object_permission_override(self, request: Any) -> bool:
        scope = getattr(request, 'effective_scope', None)

        if scope is None:
            return False

        granted = set(scope.capabilities)
        group = self.code.split(':')[0]

        return self.code in granted or f'{group}:*' in granted

    def has_permission(self, request: Any, view: Any) -> bool:
        allowed = self.has_object_permission_override(request)

        if allowed:
            return True

        self._audit_denial(request)

        return False

    def has_object_permission(self, request: Any, view: Any, obj: Any) -> bool:
        allowed = self.has_object_permission_override(request)

        if allowed:
            return True

        self._audit_denial(request)

        return False

    def _audit_denial(self, request: Any) -> None:
        organization = getattr(request, 'active_organization', None)
        actor_identity = getattr(request, 'user_identity', None)

        if organization is None or actor_identity is None:
            return

        audit_admin_action(
            organization=organization,
            actor_identity=actor_identity,
            event_type='AccessDenied',
            target_type='admin',
            target_id='',
            result='denied',
            metadata={'required_capability': self.code},
        )
