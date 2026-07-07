from __future__ import annotations

from rest_framework import serializers


class ValidateModelPoliciesSerializer(serializers.Serializer):
    policy_id = serializers.UUIDField(required=False, allow_null=True, default=None)
