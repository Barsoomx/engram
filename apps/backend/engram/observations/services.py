from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from engram.access.services import ResolveApiKeyScope
from engram.core.models import Observation, Organization, Project
from engram.core.redaction import redact_value


@dataclass(frozen=True)
class ObservationListInput:
    raw_key: str
    project_id: uuid.UUID
    team_id: uuid.UUID | None
    limit: int
    offset: int
    request_id: str
    correlation_id: str
    observation_type: str | None = None
    session_id: uuid.UUID | None = None
    since: datetime | None = None
    until: datetime | None = None


@dataclass(frozen=True)
class ObservationDetailInput:
    raw_key: str
    project_id: uuid.UUID
    team_id: uuid.UUID | None
    observation_id: uuid.UUID
    request_id: str


def observation_response(observation: Observation) -> dict[str, object]:
    return {
        'observation_id': str(observation.id),
        'session_id': str(observation.session_id),
        'observation_type': observation.observation_type,
        'title': str(redact_value(observation.title).value),
        'body': str(redact_value(observation.body).value),
        'files_read': redact_value(observation.files_read).value,
        'files_modified': redact_value(observation.files_modified).value,
        'observed_at': observation.observed_at.isoformat() if observation.observed_at else None,
    }


@dataclass(frozen=True)
class ObservationListResult:
    observations: tuple[Observation, ...]

    def to_response(self) -> dict[str, object]:
        return {
            'items': [observation_response(obs) for obs in self.observations],
            'warnings': [],
        }


class ListObservations:
    def execute(self, data: ObservationListInput) -> ObservationListResult:
        scope = ResolveApiKeyScope().execute(
            raw_key=data.raw_key,
            required_capability='observations:read',
            requested_project_id=data.project_id,
            requested_team_id=data.team_id,
            request_id=data.request_id,
            correlation_id=data.correlation_id,
            target_type='observation_list',
            target_id='list',
        )
        organization = Organization.objects.get(id=scope.organization_id)
        project = Project.objects.get(organization=organization, id=data.project_id)
        queryset = Observation.objects.filter(organization=organization, project=project)
        if data.team_id is not None:
            queryset = queryset.filter(team_id=data.team_id)
        if data.observation_type:
            queryset = queryset.filter(observation_type=data.observation_type)
        if data.session_id is not None:
            queryset = queryset.filter(session_id=data.session_id)
        if data.since is not None:
            queryset = queryset.filter(created_at__gte=data.since)
        if data.until is not None:
            queryset = queryset.filter(created_at__lt=data.until)

        offset = data.offset
        limit = data.limit
        observations = tuple(queryset.order_by('-observed_at', '-created_at')[offset : offset + limit])

        return ObservationListResult(observations=observations)


class GetObservation:
    def execute(self, data: ObservationDetailInput) -> Observation:
        scope = ResolveApiKeyScope().execute(
            raw_key=data.raw_key,
            required_capability='observations:read',
            requested_project_id=data.project_id,
            requested_team_id=data.team_id,
            request_id=data.request_id,
            correlation_id='',
            target_type='observation',
            target_id=str(data.observation_id),
        )
        organization = Organization.objects.get(id=scope.organization_id)
        project = Project.objects.get(organization=organization, id=data.project_id)
        queryset = Observation.objects.filter(organization=organization, project=project, id=data.observation_id)
        if data.team_id is not None:
            queryset = queryset.filter(team_id=data.team_id)

        observation = queryset.first()
        if observation is None:
            raise ObservationNotFoundError('observation_not_found', 'Observation was not found')

        return observation


class ObservationNotFoundError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
