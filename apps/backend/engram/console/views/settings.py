from __future__ import annotations

from django.db import transaction
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.status import HTTP_400_BAD_REQUEST
from rest_framework.views import APIView

from engram.console.org_resolution import ActiveOrganizationPermission
from engram.console.permissions import RequireCapability
from engram.console.services import audit_admin_action
from engram.core.models import (
    AuditEvent,
    ContextBundle,
    ContextBundleItem,
    Memory,
    MemoryCandidate,
    OrganizationSettings,
    RetrievalDocument,
)
from engram.model_policy.models import ModelPolicy, PolicyScope, ProviderSecret


class RetrievalSettingsView(APIView):
    def get_permissions(self) -> list[BasePermission]:
        return [
            IsAuthenticated(),
            ActiveOrganizationPermission(),
            RequireCapability('organizations:admin'),
        ]

    def get(self, request: Request) -> Response:
        settings, _ = OrganizationSettings.objects.get_or_create(
            organization=request.active_organization,
        )

        return Response(
            {
                'hybrid_retrieval_enabled': settings.hybrid_retrieval_enabled,
                'require_provenance': settings.require_provenance,
            }
        )

    def put(self, request: Request) -> Response:
        settings, _ = OrganizationSettings.objects.get_or_create(
            organization=request.active_organization,
        )
        update_fields = ['updated_at']
        if 'hybrid_retrieval_enabled' in request.data:
            settings.hybrid_retrieval_enabled = bool(request.data['hybrid_retrieval_enabled'])
            update_fields.append('hybrid_retrieval_enabled')
        if 'require_provenance' in request.data:
            settings.require_provenance = bool(request.data['require_provenance'])
            update_fields.append('require_provenance')
        settings.save(update_fields=update_fields)

        audit_admin_action(
            organization=request.active_organization,
            actor_identity=request.user_identity,
            event_type='RetrievalSettingsUpdated',
            target_type='organization_settings',
            target_id=str(settings.id),
        )

        return Response(
            {
                'hybrid_retrieval_enabled': settings.hybrid_retrieval_enabled,
                'require_provenance': settings.require_provenance,
            }
        )


class EmbeddingSettingsView(APIView):
    def get_permissions(self) -> list[BasePermission]:
        return [
            IsAuthenticated(),
            ActiveOrganizationPermission(),
            RequireCapability('organizations:admin'),
        ]

    def get(self, request: Request) -> Response:
        policy = (
            ModelPolicy.objects.filter(
                organization=request.active_organization,
                task_type='embedding',
                scope=PolicyScope.ORGANIZATION,
                active=True,
            )
            .order_by('-updated_at', '-created_at')
            .first()
        )

        if policy is None:
            return Response({'provider': None, 'model': None})

        return Response({'provider': policy.provider, 'model': policy.model})

    def put(self, request: Request) -> Response:
        provider = request.data.get('provider')
        model = request.data.get('model')
        secret_id = request.data.get('secret_id')

        if not provider or not model or not secret_id:
            return Response(
                {'error': 'provider, model, and secret_id are required'},
                status=HTTP_400_BAD_REQUEST,
            )

        try:
            secret = ProviderSecret.objects.get(
                organization=request.active_organization,
                id=secret_id,
                active=True,
            )
        except ProviderSecret.DoesNotExist:
            return Response({'error': 'provider secret not found'}, status=HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            ModelPolicy.objects.filter(
                organization=request.active_organization,
                task_type='embedding',
                scope=PolicyScope.ORGANIZATION,
            ).update(active=False)

            policy = ModelPolicy.objects.create(
                organization=request.active_organization,
                task_type='embedding',
                scope=PolicyScope.ORGANIZATION,
                provider=provider,
                model=model,
                secret=secret,
                name=f'org-embedding-{provider}',
                active=True,
            )

        audit_admin_action(
            organization=request.active_organization,
            actor_identity=request.user_identity,
            event_type='EmbeddingSettingsUpdated',
            target_type='model_policy',
            target_id=str(policy.id),
            metadata={'provider': provider, 'model': model},
        )

        return Response({'provider': policy.provider, 'model': policy.model})


class PurgeOrganizationMemoryView(APIView):
    def get_permissions(self) -> list[BasePermission]:
        return [
            IsAuthenticated(),
            ActiveOrganizationPermission(),
            RequireCapability('memories:admin'),
        ]

    def post(self, request: Request) -> Response:
        organization = request.active_organization
        confirmation = request.data.get('confirmation', '')

        if confirmation != organization.slug:
            return Response(
                {'error': 'confirmation must equal the organization slug'},
                status=HTTP_400_BAD_REQUEST,
            )

        memory_count = Memory.objects.filter(organization=organization).count()
        candidate_count = MemoryCandidate.objects.filter(organization=organization).count()
        doc_count = RetrievalDocument.objects.filter(organization=organization).count()

        AuditEvent.objects.create(
            organization=organization,
            event_type='OrganizationMemoryPurged',
            actor_type='user',
            actor_id=str(request.user_identity.id),
            target_type='organization',
            target_id=str(organization.id),
            metadata={
                'memory_count': memory_count,
                'memory_candidate_count': candidate_count,
                'retrieval_document_count': doc_count,
            },
        )

        with transaction.atomic():
            ContextBundleItem.objects.filter(organization=organization).delete()
            ContextBundle.objects.filter(organization=organization).delete()
            MemoryCandidate.objects.filter(organization=organization).delete()
            Memory.objects.filter(organization=organization).delete()

        return Response(
            {
                'deleted': {
                    'memories': memory_count,
                    'memory_candidates': candidate_count,
                    'retrieval_documents': doc_count,
                }
            }
        )
