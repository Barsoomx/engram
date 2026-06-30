from __future__ import annotations

import uuid
from typing import Any

from django.db import transaction
from django.db.models import Q
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.status import HTTP_400_BAD_REQUEST, HTTP_403_FORBIDDEN, HTTP_404_NOT_FOUND
from rest_framework.views import APIView

from engram.console.model_presets import ALL_TASK_TYPES, PRESET_BY_KEY, PRESETS
from engram.console.org_resolution import ActiveOrganizationPermission
from engram.console.permissions import RequireCapability
from engram.console.serializers.model_setup import ApplyPresetSerializer, ModelSetupStatusQuerySerializer
from engram.core.models import Organization
from engram.model_policy.models import ModelPolicy, ProviderSecret
from engram.model_policy.services import (
    CreateModelPolicy,
    CreateProviderSecret,
    DisableModelPolicy,
    DisableModelPolicyInput,
    ModelPolicyInput,
    ProviderSecretInput,
)


class ModelSetupStatusView(APIView):
    def get_permissions(self) -> list[BasePermission]:
        return [
            IsAuthenticated(),
            ActiveOrganizationPermission(),
            RequireCapability('model_policy:read'),
        ]

    def get(self, request: Request) -> Response:
        serializer = ModelSetupStatusQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        project_id: uuid.UUID | None = data.get('project_id')
        team_id: uuid.UUID | None = data.get('team_id')
        org = request.active_organization

        return Response(_build_status(org, project_id, team_id))


class ModelPresetsView(APIView):
    def get_permissions(self) -> list[BasePermission]:
        return [
            IsAuthenticated(),
            ActiveOrganizationPermission(),
            RequireCapability('model_policy:read'),
        ]

    def get(self, request: Request) -> Response:
        return Response({'presets': PRESETS})


class ApplyPresetView(APIView):
    def get_permissions(self) -> list[BasePermission]:
        return [
            IsAuthenticated(),
            ActiveOrganizationPermission(),
            RequireCapability('model_policy:*'),
        ]

    def post(self, request: Request) -> Response:
        caps = set(getattr(request.effective_scope, 'capabilities', ()))
        if 'secrets:*' not in caps:
            return Response(
                {'code': 'missing_capability', 'detail': 'secrets:* capability required'},
                status=HTTP_403_FORBIDDEN,
            )

        serializer = ApplyPresetSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        preset_key: str = data['preset_key']
        preset = PRESET_BY_KEY.get(preset_key)
        if preset is None:
            return Response(
                {'code': 'preset_not_found', 'detail': f'preset {preset_key!r} not found'},
                status=HTTP_404_NOT_FOUND,
            )

        provider_keys: dict[str, str] = data['provider_keys']
        providers_needed: list[str] = preset['providers_needed']
        missing = [slot for slot in providers_needed if slot not in provider_keys]
        if missing:
            return Response(
                {
                    'code': 'missing_provider_key',
                    'detail': f'missing provider keys: {missing}',
                },
                status=HTTP_400_BAD_REQUEST,
            )

        project_id: uuid.UUID = data['project_id']
        team_id: uuid.UUID | None = data['team_id']
        scope: str = data['scope']
        request_id: str = data['request_id']
        org = request.active_organization
        actor_id: str = request.effective_scope.actor_id

        secret_scope = 'team' if scope == 'team' else 'organization'
        secret_team_id = team_id if scope == 'team' else None
        allowed_team_ids: tuple[uuid.UUID, ...] = (team_id,) if team_id else ()

        created_secret_ids: list[str] = []
        created_policy_ids: list[str] = []

        key_slot_provider: dict[str, str] = {}
        for tm in preset['task_models']:
            slot: str = tm['key_slot']
            if slot not in key_slot_provider:
                key_slot_provider[slot] = tm['provider']

        with transaction.atomic():
            slot_to_secret: dict[str, ProviderSecret] = {}
            for slot in providers_needed:
                provider = key_slot_provider[slot]
                secret = CreateProviderSecret().execute(
                    ProviderSecretInput(
                        organization_id=org.id,
                        project_id=project_id,
                        team_id=secret_team_id,
                        name=f'{preset_key}:{slot}',
                        provider=provider,
                        scope=secret_scope,
                        raw_secret=provider_keys[slot],
                        request_id=request_id,
                        actor_id=actor_id,
                        allowed_team_ids=() if secret_scope == 'organization' else allowed_team_ids,
                    ),
                )
                slot_to_secret[slot] = secret
                created_secret_ids.append(str(secret.id))

            for tm in preset['task_models']:
                task_type: str = tm['task_type']

                existing = list(
                    _active_policies_in_scope(org, task_type, scope, project_id, team_id),
                )
                for policy in existing:
                    DisableModelPolicy().execute(
                        DisableModelPolicyInput(
                            organization_id=org.id,
                            project_id=project_id,
                            team_id=team_id,
                            policy_id=policy.id,
                            request_id=request_id,
                            actor_id=actor_id,
                            allowed_team_ids=allowed_team_ids,
                        ),
                    )

                secret = slot_to_secret[tm['key_slot']]
                policy = CreateModelPolicy().execute(
                    ModelPolicyInput(
                        organization_id=org.id,
                        project_id=project_id,
                        team_id=team_id,
                        name=f'{preset_key}:{task_type}',
                        scope=scope,
                        task_type=task_type,
                        provider=tm['provider'],
                        model=tm['model'],
                        secret_id=secret.id,
                        request_id=request_id,
                        actor_id=actor_id,
                        scope_team_id=None,
                        base_url=tm['base_url'],
                    ),
                )
                created_policy_ids.append(str(policy.id))

        return Response(
            {
                'created_secret_ids': created_secret_ids,
                'created_policy_ids': created_policy_ids,
                'status': _build_status(org, project_id, team_id),
            }
        )


def _active_policies_in_scope(
    org: Organization,
    task_type: str,
    scope: str,
    project_id: uuid.UUID | None,
    team_id: uuid.UUID | None,
) -> Any:
    qs = ModelPolicy.objects.filter(
        organization=org,
        task_type=task_type,
        active=True,
    )
    if scope == 'organization':
        qs = qs.filter(scope='organization')
    elif scope == 'project' and project_id:
        qs = qs.filter(project_id=project_id)
    elif scope == 'team' and team_id:
        qs = qs.filter(scope='team', team_id=team_id)

    return qs


def _build_status(
    org: Organization,
    project_id: uuid.UUID | None,
    team_id: uuid.UUID | None,
) -> dict[str, Any]:
    task_type_statuses = []
    for tt in ALL_TASK_TYPES:
        qs = ModelPolicy.objects.select_related('secret').filter(
            organization=org,
            task_type=tt,
            active=True,
            secret__active=True,
        )
        if project_id:
            if team_id:
                qs = qs.filter(
                    Q(project_id=project_id) | Q(team_id=team_id) | Q(scope='organization'),
                )
            else:
                qs = qs.filter(Q(project_id=project_id) | Q(scope='organization'))

        policy = qs.order_by('-updated_at', '-created_at').first()
        task_type_statuses.append(
            {
                'task_type': tt,
                'configured': policy is not None,
                'policy_id': str(policy.id) if policy else None,
                'provider': policy.provider if policy else None,
                'model': policy.model if policy else None,
                'secret_active': policy.secret.active if policy else None,
            }
        )

    secrets_qs = ProviderSecret.objects.filter(organization=org)
    if team_id:
        secrets_qs = secrets_qs.filter(Q(team_id=team_id) | Q(scope='organization'))

    secrets = [
        {'id': str(s.id), 'name': s.name, 'provider': s.provider, 'active': s.active}
        for s in secrets_qs.order_by('-created_at')
    ]

    ready = all(tt['configured'] for tt in task_type_statuses)

    return {
        'ready': ready,
        'task_types': task_type_statuses,
        'secrets': secrets,
    }
