from __future__ import annotations

import uuid
from dataclasses import dataclass

from engram.access.services import ResolveApiKeyScope
from engram.core.models import Observation, Organization, Project
from engram.core.redaction import redact_value


@dataclass(frozen=True)
class ObservationListInput:
    raw_key: str
    project_id: uuid.UUID
    team_id: uuid.UUID | None
    limit: int
    request_id: str
    correlation_id: str


@dataclass(frozen=True)
class ObservationListResult:
    observations: tuple[Observation, ...]

    def to_response(self) -> dict[str, object]:
        return {
            'items': [self._observation_response(obs) for obs in self.observations],
            'warnings': [],
        }

    def _observation_response(self, observation: Observation) -> dict[str, object]:
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

        observations = tuple(queryset.order_by('-observed_at', '-created_at')[: data.limit])

        return ObservationListResult(observations=observations)
