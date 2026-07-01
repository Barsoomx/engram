from __future__ import annotations

from rest_framework import status
from rest_framework.authentication import TokenAuthentication
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from engram.access.auth_serializers import LoginSerializer
from engram.access.auth_services import (
    AuthError,
    GetCurrentUser,
    LoginInput,
    LoginUser,
    LogoutUser,
)


def bearer_token(request: Request) -> str:
    header = request.META.get('HTTP_AUTHORIZATION', '')
    prefix = 'Token '
    if not header.startswith(prefix) or not header[len(prefix) :].strip():
        raise AuthError('invalid_token', 'Missing bearer token')

    return header[len(prefix) :].strip()


class LoginView(APIView):
    authentication_classes: list[type] = []
    permission_classes: list[type] = []

    def post(self, request: Request) -> Response:
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        result = LoginUser(
            LoginInput(
                raw_username=data['username'],
                raw_password=data['password'],
            ),
        ).execute()

        return Response(result.to_response(), status=status.HTTP_200_OK)


class MeView(APIView):
    authentication_classes: list[type] = [TokenAuthentication]
    permission_classes: list[type] = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        result = GetCurrentUser(_token_from_request(request)).execute()

        return Response(result.to_response(), status=status.HTTP_200_OK)


class LogoutView(APIView):
    authentication_classes: list[type] = [TokenAuthentication]
    permission_classes: list[type] = [IsAuthenticated]

    def post(self, request: Request) -> Response:
        LogoutUser(_token_from_request(request)).execute()

        return Response(status=status.HTTP_204_NO_CONTENT)


def _token_from_request(request: Request) -> str:
    if request.auth is not None:
        return request.auth.key

    return bearer_token(request)
