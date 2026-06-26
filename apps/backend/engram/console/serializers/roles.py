from __future__ import annotations

from rest_framework import serializers

from engram.access.models import Role, RoleCapability


class RoleReadSerializer(serializers.ModelSerializer):
    capabilities = serializers.SerializerMethodField()

    class Meta:
        model = Role
        fields = ['id', 'code', 'name', 'built_in', 'capabilities']
        read_only_fields = ['id', 'code', 'name', 'built_in', 'capabilities']

    def get_capabilities(self, role: Role) -> list[str]:
        codes = RoleCapability.objects.filter(role=role).values_list('capability__code', flat=True)

        return sorted(codes)
