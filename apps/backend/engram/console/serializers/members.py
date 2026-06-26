from __future__ import annotations

from rest_framework import serializers

from engram.access.models import Identity, IdentityType, OrganizationMembership, Role
from engram.core.models import Organization


class MemberReadSerializer(serializers.ModelSerializer):
    role = serializers.SerializerMethodField()
    identity_type = serializers.CharField(source='identity.identity_type')

    external_id = serializers.CharField(source='identity.external_id')

    display_name = serializers.CharField(source='identity.display_name')

    email = serializers.CharField(source='identity.email')

    class Meta:
        model = OrganizationMembership
        fields = [
            'id',
            'external_id',
            'display_name',
            'email',
            'identity_type',
            'active',
            'role',
        ]

    def get_role(self, obj: OrganizationMembership) -> str:
        return obj.role.code


class MemberWriteSerializer(serializers.ModelSerializer):
    external_id = serializers.CharField(max_length=255)

    display_name = serializers.CharField(max_length=255)

    email = serializers.EmailField(required=False, allow_blank=True, default='')

    role = serializers.SlugRelatedField(
        slug_field='code',
        queryset=Role.objects.all(),
    )

    class Meta:
        model = OrganizationMembership
        fields = ['external_id', 'display_name', 'email', 'role']

    @property
    def _organization(self) -> Organization | None:
        organization = self.context.get('organization')

        if isinstance(organization, Organization):
            return organization

        return None

    def validate_external_id(self, value: str) -> str:
        if not value:
            raise serializers.ValidationError('external_id is required')

        organization = self._organization

        if organization is None:
            return value

        qs = Identity.objects.filter(
            organization=organization,
            identity_type=IdentityType.USER,
            external_id=value,
        )

        if qs.exists():
            raise serializers.ValidationError(
                'member with this external_id already exists in this organization',
            )

        return value

    def validate_role(self, value: Role) -> Role:
        allowed = {'organization_owner', 'organization_admin', 'developer', 'auditor'}

        if value.code not in allowed:
            raise serializers.ValidationError('unknown role')

        return value

    def to_representation(self, instance: OrganizationMembership) -> dict:
        return MemberReadSerializer(instance, context=self.context).data
