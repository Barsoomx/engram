from __future__ import annotations

from rest_framework import serializers

from engram.core.models import Organization, Project


class ProjectReadSerializer(serializers.ModelSerializer):
    memory_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = Project
        fields = [
            'id',
            'name',
            'slug',
            'repository_url',
            'default_branch',
            'created_at',
            'updated_at',
            'memory_count',
        ]
        read_only_fields = [
            'id',
            'name',
            'slug',
            'repository_url',
            'default_branch',
            'created_at',
            'updated_at',
            'memory_count',
        ]


class ProjectWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = Project
        fields = ['name', 'slug', 'repository_url', 'default_branch']

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
            qs = Project.objects.filter(organization=organization, slug=value)

            if self.instance is not None:
                qs = qs.exclude(id=self.instance.id)

            if qs.exists():
                raise serializers.ValidationError(
                    'project with this slug already exists in this organization',
                )

        return value
