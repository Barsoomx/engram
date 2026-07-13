from __future__ import annotations

import datetime
import uuid
from typing import Any

import structlog
from django.db import transaction
from django.utils import timezone
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.status import HTTP_400_BAD_REQUEST, HTTP_404_NOT_FOUND
from rest_framework.views import APIView

from engram.console.exceptions import DigestNotFoundError
from engram.console.org_resolution import ActiveOrganizationPermission
from engram.console.permissions import RequireCapability
from engram.console.services import audit_admin_action
from engram.core.models import (
    Memory,
    Organization,
    Project,
    WorkflowSubjectType,
    WorkflowWork,
    WorkflowWorkType,
)
from engram.memory.digest_visibility import proven_digest_memory
from engram.memory.digest_work import create_digest_work_and_signal, freeze_weekly_digest_input
from engram.memory.services import (
    WEEKLY_DIGEST_WINDOW_DAYS,
    BuildWeeklyStructuredDigest,
    WeeklyDigestInput,
)
from engram.memory.tasks import generate_weekly_digest_work_v1
from engram.memory.workflow_work import CreateWorkflowWorkInput

logger = structlog.get_logger(__name__)

_DIGEST_KINDS = ('weekly_structured', 'daily_structured')


class WeeklyDigestView(APIView):
    def get_permissions(self) -> list[BasePermission]:
        return [
            IsAuthenticated(),
            ActiveOrganizationPermission(),
            RequireCapability('memories:read'),
        ]

    def get(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        organization = request.active_organization
        scope = request.effective_scope

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

        window_days = self._parse_window_days(request)

        try:
            weeks_back = max(0, int(request.query_params.get('weeks_back', '0')))
        except (TypeError, ValueError):
            weeks_back = 0

        team_id_raw = request.query_params.get('team_id')

        team_id: uuid.UUID | None = None

        if team_id_raw:
            try:
                team_id = uuid.UUID(str(team_id_raw))
            except ValueError:
                return Response(
                    {'detail': 'team_id must be a valid UUID'},
                    status=HTTP_400_BAD_REQUEST,
                )

        if project_id not in scope.project_ids:
            return Response({'detail': 'project not found'}, status=HTTP_404_NOT_FOUND)

        if team_id is not None and team_id not in scope.team_ids:
            return Response({'detail': 'team not found'}, status=HTTP_404_NOT_FOUND)

        if weeks_back > 0:
            return self._read_through_history(organization, project_id, window_days, team_id, weeks_back)

        project = Project.objects.filter(organization=organization, id=project_id).first()

        if project is None:
            return Response({'detail': 'project not found'}, status=HTTP_404_NOT_FOUND)

        return self._enqueue_current(organization, project, team_id, window_days)

    def _parse_window_days(self, request: Request) -> int:
        window_days_raw = request.query_params.get('window_days', str(WEEKLY_DIGEST_WINDOW_DAYS))

        try:
            return max(1, int(window_days_raw))
        except (TypeError, ValueError):
            return WEEKLY_DIGEST_WINDOW_DAYS

    def _enqueue_current(
        self,
        organization: Organization,
        project: Project,
        team_id: uuid.UUID | None,
        window_days: int,
    ) -> Response:
        window_start, window_end = _current_weekly_window()
        schedule_key = f'weekly:{project.id}:{window_start.date().isoformat()}:{team_id or ""}'

        with transaction.atomic():
            snapshot = freeze_weekly_digest_input(
                organization_id=organization.id,
                project_id=project.id,
                team_id=team_id,
                window_start=window_start,
                window_end=window_end,
                schedule_key=schedule_key,
            )
            data = CreateWorkflowWorkInput(
                organization_id=organization.id,
                project_id=project.id,
                work_type=WorkflowWorkType.WEEKLY_DIGEST,
                subject_type=WorkflowSubjectType.TEAM if team_id is not None else WorkflowSubjectType.PROJECT,
                subject_id=team_id if team_id is not None else project.id,
                input_snapshot=snapshot,
                occurrence_key=schedule_key,
            )
            work, _created = create_digest_work_and_signal(
                data=data,
                signal_task=generate_weekly_digest_work_v1,
            )

        proven = _proven_output_for_work(organization, project, work)

        if proven is not None:
            return Response(_built_response(proven))

        return Response(_not_built_response(window_start, window_end, window_days))

    def _read_through_history(
        self,
        organization: Organization,
        project_id: uuid.UUID,
        window_days: int,
        team_id: uuid.UUID | None,
        weeks_back: int,
    ) -> Response:
        digest_input = WeeklyDigestInput(
            organization_id=organization.id,
            project_id=project_id,
            window_days=window_days,
            team_id=team_id,
            weeks_back=weeks_back,
        )

        try:
            existing, window_start, window_end = BuildWeeklyStructuredDigest().find_existing(digest_input)
        except Project.DoesNotExist:
            return Response({'detail': 'project not found'}, status=HTTP_404_NOT_FOUND)

        if existing is None or not proven_digest_memory(existing.digest_memory):
            return Response(_not_built_response(window_start, window_end, window_days))

        return Response(_built_response(existing.digest_memory))


class DigestReviewView(APIView):
    def get_permissions(self) -> list[BasePermission]:
        return [
            IsAuthenticated(),
            ActiveOrganizationPermission(),
            RequireCapability('memories:review'),
        ]

    def post(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        organization = request.active_organization
        scope = request.effective_scope

        memory_id: uuid.UUID = kwargs['memory_id']

        digest_kind = request.data.get('digest_kind', 'weekly_structured')

        if digest_kind not in _DIGEST_KINDS:
            return Response(
                {'detail': f'digest_kind must be one of {_DIGEST_KINDS}'},
                status=HTTP_400_BAD_REQUEST,
            )

        memory = Memory.objects.filter(
            organization=organization,
            id=memory_id,
            kind='digest',
            metadata__digest_kind=digest_kind,
            project_id__in=scope.project_ids,
        ).first()

        if memory is None or not proven_digest_memory(memory):
            raise DigestNotFoundError('digest not found')

        metadata = memory.metadata if isinstance(memory.metadata, dict) else {}

        memory.metadata = {
            **metadata,
            'ready': True,
            'reviewed_at': timezone.now().isoformat(),
        }

        memory.save(update_fields=['metadata', 'updated_at'])

        audit_admin_action(
            organization=organization,
            actor_identity=request.user_identity,
            event_type='DigestReviewed',
            target_type='memory',
            target_id=str(memory.id),
            metadata={
                'digest_kind': digest_kind,
                'memory_id': str(memory.id),
            },
        )

        logger.info(
            'digest_reviewed',
            organization_id=str(organization.id),
            memory_id=str(memory.id),
            digest_kind=digest_kind,
        )

        return Response(
            {
                'memory_id': str(memory.id),
                'reviewed': True,
                'ready': True,
            }
        )


def _current_weekly_window() -> tuple[datetime.datetime, datetime.datetime]:
    today = timezone.now().date()
    current_monday = today - datetime.timedelta(days=today.isoweekday() - 1)
    tzinfo = timezone.get_current_timezone()
    window_end = datetime.datetime.combine(current_monday, datetime.time.min, tzinfo=tzinfo)
    window_start = datetime.datetime.combine(
        current_monday - datetime.timedelta(days=7),
        datetime.time.min,
        tzinfo=tzinfo,
    )

    return window_start, window_end


def _proven_output_for_work(
    organization: Organization,
    project: Project,
    work: WorkflowWork,
) -> Memory | None:
    memory = (
        Memory.objects.filter(
            organization=organization,
            project=project,
            kind='digest',
            metadata__digest_visibility__workflow_work_id=str(work.id),
        )
        .order_by('created_at')
        .first()
    )

    if memory is None or not proven_digest_memory(memory):
        return None

    return memory


def _built_response(memory: Memory) -> dict[str, object]:
    metadata = memory.metadata if isinstance(memory.metadata, dict) else {}
    memory_changes = metadata.get('memory_changes', {}) or {}

    return {
        'digest_memory_id': str(memory.id),
        'built': True,
        'window_start': metadata.get('window_start'),
        'window_end': metadata.get('window_end'),
        'window_days': metadata.get('window_days'),
        'counts': metadata.get('counts', {}) or {},
        'memory_changes': memory_changes,
        'changelog': _build_changelog(memory_changes),
        'ready': bool(metadata.get('ready', False)),
    }


def _not_built_response(
    window_start: datetime.datetime,
    window_end: datetime.datetime,
    window_days: int,
) -> dict[str, object]:
    return {
        'digest_memory_id': None,
        'built': False,
        'window_start': window_start.isoformat(),
        'window_end': window_end.isoformat(),
        'window_days': window_days,
        'counts': {},
        'memory_changes': {},
        'changelog': [],
        'ready': False,
    }


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

    changelog.sort(key=lambda entry: entry['at'])

    return changelog
