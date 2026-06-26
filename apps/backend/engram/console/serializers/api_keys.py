from __future__ import annotations

from rest_framework import serializers

from engram.access.models import ApiKey, ApiKeyCapability


class ApiKeyReadSerializer(serializers.ModelSerializer):
    owner_identity = serializers.SerializerMethodField()

    capabilities = serializers.SerializerMethodField()

    class Meta:
        model = ApiKey

        fields = [
            'id',
            'name',
            'key_prefix',
            'key_fingerprint',
            'owner_identity',
            'capabilities',
            'created_at',
            'expires_at',
            'last_used_at',
            'active',
            'revoked_at',
        ]

        read_only_fields = fields

    def get_owner_identity(self, obj: ApiKey) -> dict:
        identity = obj.owner_identity

        return {
            'id': str(identity.id),
            'display_name': identity.display_name,
        }

    def get_capabilities(self, obj: ApiKey) -> list[str]:
        return list(
            ApiKeyCapability.objects.filter(api_key=obj).values_list(
                'capability__code',
                flat=True,
            ),
        )


class ApiKeyIssueInputSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=255)

    capabilities = serializers.ListField(
        child=serializers.CharField(max_length=120),
        min_length=1,
        allow_empty=False,
    )

    team = serializers.PrimaryKeyRelatedField(read_only=True, required=False)

    project = serializers.PrimaryKeyRelatedField(read_only=True, required=False)

    expires_at = serializers.DateTimeField(required=False, allow_null=True)


class ApiKeyIssueResultSerializer(serializers.ModelSerializer):
    capabilities = serializers.SerializerMethodField()

    plaintext = serializers.SerializerMethodField()

    class Meta:
        model = ApiKey

        fields = [
            'id',
            'name',
            'key_prefix',
            'key_fingerprint',
            'plaintext',
            'capabilities',
            'created_at',
        ]

        read_only_fields = fields

    def get_plaintext(self, obj: ApiKey) -> str:
        plaintext = self.context.get('plaintext')

        if not plaintext:
            raise serializers.ValidationError('plaintext must be provided via context')

        return plaintext

    def get_capabilities(self, obj: ApiKey) -> list[str]:
        return list(
            ApiKeyCapability.objects.filter(api_key=obj).values_list(
                'capability__code',
                flat=True,
            ),
        )
