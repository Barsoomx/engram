from __future__ import annotations

import uuid
from typing import Any

from django.db.models import Q, QuerySet
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.authentication import TokenAuthentication
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from engram.access.request_scope import resolve_request_scope
from engram.access.services import AccessDeniedError, EffectiveScope
from engram.context.views import access_error_response
from engram.model_policy.models import ModelPolicy, ProviderSecret
from engram.model_policy.serializers import (
    ModelPolicyCreateSerializer,
    ModelPolicyQuerySerializer,
    ModelPolicyResolveSerializer,
    ProviderSecretCreateSerializer,
    ProviderSecretDisableSerializer,
    ProviderSecretQuerySerializer,
    ProviderSecretRotateSerializer,
)
from engram.model_policy.services import (
    CreateModelPolicy,
    CreateProviderSecret,
    DisableProviderSecret,
    DisableProviderSecretInput,
    ModelPolicyError,
    ModelPolicyInput,
    ProviderSecretInput,
    ResolveModelPolicy,
    ResolveModelPolicyInput,
    RotateProviderSecret,
    RotateProviderSecretInput,
)

ERROR_STATUS = {
    'policy_scope_mismatch': status.HTTP_400_BAD_REQUEST,
    'team_required': status.HTTP_400_BAD_REQUEST,
    'model_policy_not_found': status.HTTP_404_NOT_FOUND,
    'secret_scope_denied': status.HTTP_403_FORBIDDEN,
}


class ModelPolicyBaseView(APIView):
    authentication_classes: list[type] = [TokenAuthentication]
    permission_classes: list[type] = []

    def _scope(
        self,
        request: Request,
        *,
        required_capability: str,
        project_id: uuid.UUID,
        team_id: uuid.UUID | None,
        target_type: str,
        target_id: str,
        request_id: str = '',
    ) -> EffectiveScope:
        return resolve_request_scope(
            request,
            required_capability=required_capability,
            project_id=project_id,
            team_id=team_id,
            request_id=request_id,
            target_type=target_type,
            target_id=target_id,
        )


class ProviderSecretListView(ModelPolicyBaseView):
    def get(self, request: Request) -> Response:
        serializer = ProviderSecretQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            scope = self._scope(
                request,
                required_capability='secrets:*',
                project_id=data['project_id'],
                team_id=data.get('team_id'),
                target_type='provider_secret',
                target_id='list',
            )
        except AccessDeniedError as error:
            return access_error_response(error)

        secrets = scoped_secrets(scope).order_by('-created_at')

        return Response([provider_secret_response(secret) for secret in secrets])

    def post(self, request: Request) -> Response:
        serializer = ProviderSecretCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            scope = self._scope(
                request,
                required_capability='secrets:*',
                project_id=data['project_id'],
                team_id=data.get('team_id'),
                target_type='provider_secret',
                target_id='create',
                request_id=data['request_id'],
            )
            secret = CreateProviderSecret().execute(
                ProviderSecretInput(
                    organization_id=scope.organization_id,
                    project_id=data['project_id'],
                    team_id=data.get('team_id'),
                    name=data['name'],
                    provider=data['provider'],
                    scope=data['scope'],
                    raw_secret=data['raw_secret'],
                    request_id=data['request_id'],
                    actor_id=scope.actor_id,
                    allowed_team_ids=scope.team_ids,
                ),
            )
        except AccessDeniedError as error:
            return access_error_response(error)
        except ModelPolicyError as error:
            return error_response(error)

        return Response(provider_secret_response(secret), status=status.HTTP_201_CREATED)


class ProviderSecretDetailView(ModelPolicyBaseView):
    def get(self, request: Request, secret_id: uuid.UUID) -> Response:
        serializer = ProviderSecretQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            scope = self._scope(
                request,
                required_capability='secrets:*',
                project_id=data['project_id'],
                team_id=data.get('team_id'),
                target_type='provider_secret',
                target_id=str(secret_id),
            )
        except AccessDeniedError as error:
            return access_error_response(error)

        secret = get_object_or_404(scoped_secrets(scope), id=secret_id)

        return Response(provider_secret_response(secret))


class ProviderSecretRotateView(ModelPolicyBaseView):
    def post(self, request: Request, secret_id: uuid.UUID) -> Response:
        serializer = ProviderSecretRotateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            scope = self._scope(
                request,
                required_capability='secrets:*',
                project_id=data['project_id'],
                team_id=data.get('team_id'),
                target_type='provider_secret',
                target_id=str(secret_id),
                request_id=data['request_id'],
            )
            secret = RotateProviderSecret().execute(
                RotateProviderSecretInput(
                    organization_id=scope.organization_id,
                    project_id=data['project_id'],
                    team_id=data.get('team_id'),
                    secret_id=secret_id,
                    raw_secret=data['raw_secret'],
                    request_id=data['request_id'],
                    actor_id=scope.actor_id,
                    allowed_team_ids=scope.team_ids,
                ),
            )
        except AccessDeniedError as error:
            return access_error_response(error)
        except ModelPolicyError as error:
            return error_response(error)

        return Response(provider_secret_response(secret))


class ProviderSecretDisableView(ModelPolicyBaseView):
    def post(self, request: Request, secret_id: uuid.UUID) -> Response:
        serializer = ProviderSecretDisableSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            scope = self._scope(
                request,
                required_capability='secrets:*',
                project_id=data['project_id'],
                team_id=data.get('team_id'),
                target_type='provider_secret',
                target_id=str(secret_id),
                request_id=data['request_id'],
            )
            secret = DisableProviderSecret().execute(
                DisableProviderSecretInput(
                    organization_id=scope.organization_id,
                    project_id=data['project_id'],
                    team_id=data.get('team_id'),
                    secret_id=secret_id,
                    request_id=data['request_id'],
                    actor_id=scope.actor_id,
                    allowed_team_ids=scope.team_ids,
                ),
            )
        except AccessDeniedError as error:
            return access_error_response(error)
        except ModelPolicyError as error:
            return error_response(error)

        return Response(provider_secret_response(secret))


class ModelPolicyListView(ModelPolicyBaseView):
    def get(self, request: Request) -> Response:
        serializer = ModelPolicyQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            scope = self._scope(
                request,
                required_capability='model_policy:*',
                project_id=data['project_id'],
                team_id=data.get('team_id'),
                target_type='model_policy',
                target_id='list',
            )
        except AccessDeniedError as error:
            return access_error_response(error)

        policies = scoped_policies(scope)
        task_type = data.get('task_type')
        if task_type:
            policies = policies.filter(task_type=task_type)

        return Response([model_policy_response(policy) for policy in policies.order_by('-created_at')])

    def post(self, request: Request) -> Response:
        serializer = ModelPolicyCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            scope = self._scope(
                request,
                required_capability='model_policy:*',
                project_id=data['project_id'],
                team_id=data.get('team_id'),
                target_type='model_policy',
                target_id='create',
                request_id=data['request_id'],
            )
            policy = CreateModelPolicy().execute(
                ModelPolicyInput(
                    organization_id=scope.organization_id,
                    project_id=data['project_id'],
                    team_id=data.get('team_id'),
                    name=data['name'],
                    scope=data['scope'],
                    task_type=data['task_type'],
                    provider=data['provider'],
                    model=data['model'],
                    secret_id=data['secret_id'],
                    request_id=data['request_id'],
                    actor_id=scope.actor_id,
                    scope_team_id=data.get('scope_team_id'),
                ),
            )
        except AccessDeniedError as error:
            return access_error_response(error)
        except ModelPolicyError as error:
            return error_response(error)

        return Response(model_policy_response(policy), status=status.HTTP_201_CREATED)


class ModelPolicyResolveView(ModelPolicyBaseView):
    def get(self, request: Request) -> Response:
        serializer = ModelPolicyResolveSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            scope = self._scope(
                request,
                required_capability='model_policy:*',
                project_id=data['project_id'],
                team_id=data.get('team_id'),
                target_type='model_policy',
                target_id='resolve',
            )
            resolved = ResolveModelPolicy().execute(
                ResolveModelPolicyInput(
                    organization_id=scope.organization_id,
                    project_id=data['project_id'],
                    team_id=data.get('team_id'),
                    task_type=data['task_type'],
                ),
            )
        except AccessDeniedError as error:
            return access_error_response(error)
        except ModelPolicyError as error:
            return error_response(error)

        return Response(model_policy_response(resolved.policy))


def provider_secret_response(secret: ProviderSecret) -> dict[str, Any]:
    return {
        'id': str(secret.id),
        'organization_id': str(secret.organization_id),
        'team_id': str(secret.team_id) if secret.team_id else None,
        'name': secret.name,
        'provider': secret.provider,
        'scope': secret.scope,
        'storage_mode': secret.storage_mode,
        'current_version': secret.current_version,
        'active': secret.active,
        'rotation_state': secret.rotation_state,
        'secret_fingerprint': secret.secret_fingerprint,
        'created_at': secret.created_at.isoformat() if secret.created_at else None,
        'updated_at': secret.updated_at.isoformat() if secret.updated_at else None,
    }


def scoped_secrets(scope: EffectiveScope) -> QuerySet[ProviderSecret]:
    return ProviderSecret.objects.filter(organization_id=scope.organization_id).filter(
        Q(team__isnull=True) | Q(team_id__in=scope.team_ids),
    )


def scoped_policies(scope: EffectiveScope) -> QuerySet[ModelPolicy]:
    return ModelPolicy.objects.filter(organization_id=scope.organization_id).filter(
        Q(team__isnull=True) | Q(team_id__in=scope.team_ids),
    )


def model_policy_response(policy: ModelPolicy) -> dict[str, Any]:
    return {
        'id': str(policy.id),
        'policy_id': str(policy.id),
        'organization_id': str(policy.organization_id),
        'team_id': str(policy.team_id) if policy.team_id else None,
        'project_id': str(policy.project_id) if policy.project_id else None,
        'secret_id': str(policy.secret_id),
        'name': policy.name,
        'scope': policy.scope,
        'task_type': policy.task_type,
        'provider': policy.provider,
        'model': policy.model,
        'version': policy.version,
        'active': policy.active,
        'fallback_enabled': policy.fallback_enabled,
    }


def error_response(error: ModelPolicyError) -> Response:
    return Response(
        {'code': error.code, 'detail': str(error)},
        status=ERROR_STATUS.get(error.code, status.HTTP_400_BAD_REQUEST),
    )
