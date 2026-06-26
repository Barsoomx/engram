from __future__ import annotations

from typing import Any

from rest_framework.permissions import BasePermission


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
        return self.has_object_permission_override(request)

    def has_object_permission(self, request: Any, view: Any, obj: Any) -> bool:
        return self.has_object_permission_override(request)
