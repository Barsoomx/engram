from __future__ import annotations

from rest_framework import serializers

AUTH_USERNAME_MAX_LENGTH = 255
AUTH_PASSWORD_MAX_LENGTH = 255


class LoginSerializer(serializers.Serializer):
    username = serializers.CharField(
        max_length=AUTH_USERNAME_MAX_LENGTH,
        allow_blank=False,
        trim_whitespace=False,
    )

    password = serializers.CharField(
        max_length=AUTH_PASSWORD_MAX_LENGTH,
        allow_blank=False,
        trim_whitespace=False,
        write_only=True,
    )
