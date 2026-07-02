from __future__ import annotations

from decimal import Decimal, InvalidOperation

import structlog
from django.db import transaction
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.status import HTTP_400_BAD_REQUEST
from rest_framework.views import APIView

from engram.console.exceptions import EmbeddingFieldsRequiredError, EmbeddingSecretNotFoundError
from engram.console.org_resolution import ActiveOrganizationPermission
from engram.console.permissions import RequireCapability
from engram.console.services import audit_admin_action
from engram.core.models import (
    ContextBundle,
    ContextBundleItem,
    Memory,
    MemoryCandidate,
    OrganizationSettings,
    RetrievalDocument,
)
from engram.model_policy.models import ModelPolicy, PolicyScope, ProviderSecret

logger = structlog.get_logger(__name__)

_BOOLEAN_SETTINGS_FIELDS = (
    'hybrid_retrieval_enabled',
    'require_provenance',
    'lexical_recall_enabled',
    'lexical_fusion_enabled',
    'curator_llm_judge_enabled',
)


def _parse_unit_threshold(raw: object) -> Decimal:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError('threshold must be a number') from exc

    if value < 0 or value > 1:
        raise ValueError('threshold must be between 0 and 1')

    return value


def _serialize_retrieval_settings(settings: OrganizationSettings) -> dict:
    return {
        'hybrid_retrieval_enabled': settings.hybrid_retrieval_enabled,
        'require_provenance': settings.require_provenance,
        'lexical_recall_enabled': settings.lexical_recall_enabled,
        'lexical_fusion_enabled': settings.lexical_fusion_enabled,
        'curator_llm_judge_enabled': settings.curator_llm_judge_enabled,
        'near_dup_threshold': settings.near_dup_threshold,
        'distillation_auto_approve_threshold': settings.distillation_auto_approve_threshold,
    }


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

        return Response(_serialize_retrieval_settings(settings))

    def put(self, request: Request) -> Response:
        settings, _ = OrganizationSettings.objects.get_or_create(
            organization=request.active_organization,
        )
        update_fields = ['updated_at']

        for field in _BOOLEAN_SETTINGS_FIELDS:
            if field in request.data:
                setattr(settings, field, bool(request.data[field]))
                update_fields.append(field)

        if 'near_dup_threshold' in request.data:
            try:
                settings.near_dup_threshold = _parse_unit_threshold(request.data['near_dup_threshold'])
            except ValueError as exc:
                return Response({'error': str(exc)}, status=HTTP_400_BAD_REQUEST)

            update_fields.append('near_dup_threshold')

        if 'distillation_auto_approve_threshold' in request.data:
            raw_threshold = request.data['distillation_auto_approve_threshold']
            if raw_threshold is None:
                settings.distillation_auto_approve_threshold = None
            else:
                try:
                    settings.distillation_auto_approve_threshold = _parse_unit_threshold(raw_threshold)
                except ValueError as exc:
                    return Response({'error': str(exc)}, status=HTTP_400_BAD_REQUEST)

            update_fields.append('distillation_auto_approve_threshold')

        settings.save(update_fields=update_fields)

        audit_admin_action(
            organization=request.active_organization,
            actor_identity=request.user_identity,
            event_type='RetrievalSettingsUpdated',
            target_type='organization_settings',
            target_id=str(settings.id),
        )

        return Response(_serialize_retrieval_settings(settings))


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
            raise EmbeddingFieldsRequiredError('provider, model, and secret_id are required')

        try:
            secret = ProviderSecret.objects.get(
                organization=request.active_organization,
                id=secret_id,
                active=True,
            )
        except ProviderSecret.DoesNotExist:
            raise EmbeddingSecretNotFoundError('provider secret not found') from None

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

        logger.info(
            'embedding_settings_updated',
            organization_id=str(request.active_organization.id),
            provider=provider,
            model=model,
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

        with transaction.atomic():
            memory_count = Memory.objects.filter(organization=organization).count()
            candidate_count = MemoryCandidate.objects.filter(organization=organization).count()
            doc_count = RetrievalDocument.objects.filter(organization=organization).count()
            bundle_item_count = ContextBundleItem.objects.filter(organization=organization).count()
            bundle_count = ContextBundle.objects.filter(organization=organization).count()

            ContextBundleItem.objects.filter(organization=organization).delete()
            ContextBundle.objects.filter(organization=organization).delete()
            MemoryCandidate.objects.filter(organization=organization).delete()
            Memory.objects.filter(organization=organization).delete()

            audit_admin_action(
                organization=organization,
                actor_identity=request.user_identity,
                event_type='OrganizationMemoryPurged',
                target_type='organization',
                target_id=str(organization.id),
                metadata={
                    'memory_count': memory_count,
                    'memory_candidate_count': candidate_count,
                    'retrieval_document_count': doc_count,
                    'context_bundle_count': bundle_count,
                    'context_bundle_item_count': bundle_item_count,
                },
            )

        logger.info(
            'organization_memory_purged',
            organization_id=str(organization.id),
            memory_count=memory_count,
            memory_candidate_count=candidate_count,
            retrieval_document_count=doc_count,
            context_bundle_count=bundle_count,
            context_bundle_item_count=bundle_item_count,
        )

        return Response(
            {
                'deleted': {
                    'memories': memory_count,
                    'memory_candidates': candidate_count,
                    'retrieval_documents': doc_count,
                    'context_bundles': bundle_count,
                    'context_bundle_items': bundle_item_count,
                }
            }
        )
