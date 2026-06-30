from __future__ import annotations

from rest_framework import serializers

from engram.core.models import Organization


class OrganizationReadSerializer(serializers.ModelSerializer):
    member_count = serializers.SerializerMethodField()
    viewer_role = serializers.SerializerMethodField()

    class Meta:
        model = Organization
        fields = ['id', 'name', 'slug', 'created_at', 'updated_at', 'member_count', 'viewer_role']
        read_only_fields = ['id', 'name', 'slug', 'created_at', 'updated_at', 'member_count', 'viewer_role']

    def get_member_count(self, obj: Organization) -> int | None:
        value = getattr(obj, 'member_count', None)

        return value

    def get_viewer_role(self, obj: Organization) -> str | None:
        viewer_memberships = getattr(obj, 'viewer_memberships', None)

        if not viewer_memberships:
            return None

        return viewer_memberships[0].role.code


class OrganizationWriteSerializer(serializers.ModelSerializer):
    slug = serializers.CharField(read_only=True)

    class Meta:
        model = Organization
        fields = ['name', 'slug']

    def to_internal_value(self, data: dict) -> dict:
        if 'slug' in data:
            raise serializers.ValidationError({'slug': 'slug is immutable'})

        return super().to_internal_value(data)
