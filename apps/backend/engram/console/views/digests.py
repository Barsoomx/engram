from __future__ import annotations

import uuid
from typing import Any

from django.utils import timezone
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.status import HTTP_400_BAD_REQUEST, HTTP_404_NOT_FOUND
from rest_framework.views import APIView

from engram.console.org_resolution import ActiveOrganizationPermission
from engram.console.permissions import RequireCapability
from engram.console.services import audit_admin_action
from engram.core.models import Memory, Project
from engram.memory.services import (
    WEEKLY_DIGEST_WINDOW_DAYS,
    BuildWeeklyStructuredDigest,
    WeeklyDigestInput,
)


class WeeklyDigestView(APIView):
    def get_permissions(self) -> list[BasePermission]:
        return [
            IsAuthenticated(),
            ActiveOrganizationPermission(),
            RequireCapability('memories:read'),
        ]

    def get(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        organization = request.active_organization

        project_id_raw = request.query_params.get('project_id')

        if not project_id_raw:
            return Response(
                {'detail': 'project_id is required'},
                status=HTTP_400_BAD_REQUEST,
            )

        try:
            project_id = uuid.UUID(str(project_id_raw))
        except ValueError:
            return Response(
                {'detail': 'project_id must be a valid UUID'},
                status=HTTP_400_BAD_REQUEST,
            )

        window_days_raw = request.query_params.get('window_days', str(WEEKLY_DIGEST_WINDOW_DAYS))

        try:
            window_days = max(1, int(window_days_raw))
        except (TypeError, ValueError):
            window_days = WEEKLY_DIGEST_WINDOW_DAYS

        try:
            result = BuildWeeklyStructuredDigest().execute(
                WeeklyDigestInput(
                    organization_id=organization.id,
                    project_id=project_id,
                    window_days=window_days,
                ),
            )
        except Project.DoesNotExist:
            return Response({'detail': 'project not found'}, status=HTTP_404_NOT_FOUND)

        metadata = result.digest_memory.metadata if isinstance(result.digest_memory.metadata, dict) else {}

        changelog = _build_changelog(result.memory_changes)

        return Response(
            {
                'window_start': metadata.get('window_start'),
                'window_end': metadata.get('window_end'),
                'window_days': metadata.get('window_days'),
                'counts': result.counts,
                'memory_changes': result.memory_changes,
                'changelog': changelog,
                'ready': result.ready,
            }
        )


class DigestReviewView(APIView):
    def get_permissions(self) -> list[BasePermission]:
        return [
            IsAuthenticated(),
            ActiveOrganizationPermission(),
            RequireCapability('memories:review'),
        ]

    def post(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        organization = request.active_organization

        memory_id: uuid.UUID = kwargs['memory_id']

        memory = Memory.objects.filter(
            organization=organization,
            id=memory_id,
            metadata__kind='digest',
            metadata__digest_kind='weekly_structured',
        ).first()

        if memory is None:
            return Response({'detail': 'digest not found'}, status=HTTP_404_NOT_FOUND)

        metadata = memory.metadata if isinstance(memory.metadata, dict) else {}

        updated_metadata = {
            **metadata,
            'ready': True,
            'reviewed_at': timezone.now().isoformat(),
        }

        memory.metadata = updated_metadata

        memory.save(update_fields=['metadata', 'updated_at'])

        audit_admin_action(
            organization=organization,
            actor_identity=request.user_identity,
            event_type='DigestReviewed',
            target_type='memory',
            target_id=str(memory.id),
            metadata={
                'digest_kind': 'weekly_structured',
                'memory_id': str(memory.id),
            },
        )

        return Response(
            {
                'memory_id': str(memory.id),
                'reviewed': True,
                'ready': True,
            }
        )


def _build_changelog(memory_changes: dict) -> list[dict]:
    changelog: list[dict] = []

    for bucket, items in memory_changes.items():
        for item in items:
            changelog.append(
                {
                    'id': item['id'],
                    'title': item['title'],
                    'bucket': bucket,
                    'at': item['at'],
                }
            )

    changelog.sort(key=lambda x: x['at'])

    return changelog
