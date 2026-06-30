from __future__ import annotations

from rest_framework import serializers

from engram.core.models import Organization, Team


class TeamReadSerializer(serializers.ModelSerializer):
    class Meta:
        model = Team
        fields = ['id', 'name', 'slug', 'created_at', 'updated_at', 'archived_at', 'organization']
        read_only_fields = ['id', 'name', 'slug', 'created_at', 'updated_at', 'archived_at', 'organization']


class TeamWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = Team
        fields = ['name', 'slug']

    @property
    def _organization(self) -> Organization | None:
        organization = self.context.get('organization')

        if isinstance(organization, Organization):
            return organization

        return None

    def validate_slug(self, value: str) -> str:
        if not value:
            raise serializers.ValidationError('slug is required')

        organization = self._organization

        if organization is not None:
            qs = Team.objects.filter(organization=organization, slug=value)

            if self.instance is not None:
                qs = qs.exclude(id=self.instance.id)

            if qs.exists():
                raise serializers.ValidationError('team with this slug already exists in this organization')

        return value
