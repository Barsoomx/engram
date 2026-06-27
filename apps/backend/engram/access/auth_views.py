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

AUTH_STATUS = {
    'invalid_credentials': status.HTTP_401_UNAUTHORIZED,
    'inactive_user': status.HTTP_403_FORBIDDEN,
    'invalid_token': status.HTTP_401_UNAUTHORIZED,
    'identity_missing': status.HTTP_403_FORBIDDEN,
    'membership_missing': status.HTTP_403_FORBIDDEN,
    'default_role_missing': status.HTTP_500_INTERNAL_SERVER_ERROR,
}


def auth_error_response(error: AuthError) -> Response:
    return Response(
        {'code': error.code, 'detail': str(error)},
        status=AUTH_STATUS.get(error.code, status.HTTP_400_BAD_REQUEST),
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

        try:
            result = LoginUser(
                LoginInput(
                    raw_username=data['username'],
                    raw_password=data['password'],
                ),
            ).execute()
        except AuthError as error:
            return auth_error_response(error)

        return Response(result.to_response(), status=status.HTTP_200_OK)


class MeView(APIView):
    authentication_classes: list[type] = [TokenAuthentication]
    permission_classes: list[type] = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        try:
            result = GetCurrentUser(_token_from_request(request)).execute()
        except AuthError as error:
            return auth_error_response(error)

        return Response(result.to_response(), status=status.HTTP_200_OK)


class LogoutView(APIView):
    authentication_classes: list[type] = [TokenAuthentication]
    permission_classes: list[type] = [IsAuthenticated]

    def post(self, request: Request) -> Response:
        try:
            LogoutUser(_token_from_request(request)).execute()
        except AuthError as error:
            return auth_error_response(error)

        return Response(status=status.HTTP_204_NO_CONTENT)


def _token_from_request(request: Request) -> str:
    if request.auth is not None:
        return request.auth.key

    return bearer_token(request)
