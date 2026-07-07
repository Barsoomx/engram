from __future__ import annotations

from rest_framework.request import Request

from engram.access.services import EffectiveScope
from engram.core.models import Organization, Team
from engram.core.repository import resolve_project_for_scope
from engram.imports.models import ImportJob
from engram.imports.serializers import MAX_REQUEST_BYTES


def resolve_import_organization(scope: EffectiveScope) -> Organization:
    return Organization.objects.get(id=scope.organization_id)


def resolve_team_for_scope(scope: EffectiveScope, organization: Organization) -> Team | None:
    if len(scope.team_ids) != 1:
        return None

    return Team.objects.filter(organization=organization, id=scope.team_ids[0]).first()


def authorize_job_project(scope: EffectiveScope, job: ImportJob, request_id: str = '') -> None:
    resolve_project_for_scope(scope=scope, project_id=job.project_id, repository_url='', request_id=request_id)


def request_too_large(request: Request) -> bool:
    declared = int(request.META.get('CONTENT_LENGTH') or 0)

    return declared > MAX_REQUEST_BYTES
