from __future__ import annotations

from rest_framework import serializers

from engram.core.models import Organization


class OrganizationReadSerializer(serializers.ModelSerializer):
    class Meta:
        model = Organization
        fields = ['id', 'name', 'slug', 'created_at', 'updated_at']
        read_only_fields = ['id', 'name', 'slug', 'created_at', 'updated_at']


class OrganizationWriteSerializer(serializers.ModelSerializer):
    slug = serializers.CharField(read_only=True)

    class Meta:
        model = Organization
        fields = ['name', 'slug']

    def to_internal_value(self, data: dict) -> dict:
        if 'slug' in data:
            raise serializers.ValidationError({'slug': 'slug is immutable'})

        return super().to_internal_value(data)
