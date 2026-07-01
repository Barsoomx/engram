from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from engram.access.services import EffectiveScope
from engram.core.models import Observation, Organization, Project
from engram.core.redaction import redact_value
from engram.observations.filters import ObservationFilterSet


@dataclass(frozen=True)
class ObservationListInput:
    scope: EffectiveScope
    project_id: uuid.UUID
    team_id: uuid.UUID | None
    limit: int
    offset: int
    observation_type: str | None = None
    session_id: uuid.UUID | None = None
    since: datetime | None = None
    until: datetime | None = None
    correlation_id: str | None = None


@dataclass(frozen=True)
class ObservationDetailInput:
    scope: EffectiveScope
    project_id: uuid.UUID
    team_id: uuid.UUID | None
    observation_id: uuid.UUID


def observation_response(observation: Observation) -> dict[str, object]:
    return {
        'observation_id': str(observation.id),
        'session_id': str(observation.session_id),
        'team_id': str(observation.team_id) if observation.team_id else None,
        'observation_type': observation.observation_type,
        'title': str(redact_value(observation.title).value),
        'subtitle': str(redact_value(observation.subtitle).value),
        'body': str(redact_value(observation.body).value),
        'facts': redact_value(observation.facts).value,
        'narrative': str(redact_value(observation.narrative).value),
        'concepts': redact_value(observation.concepts).value,
        'files_read': redact_value(observation.files_read).value,
        'files_modified': redact_value(observation.files_modified).value,
        'prompt_number': observation.prompt_number,
        'content_hash': observation.content_hash,
        'generation_key': observation.generation_key,
        'generated_model': observation.generated_model,
        'redaction_metadata': redact_value(observation.redaction_metadata).value,
        'source_metadata': redact_value(observation.source_metadata).value,
        'observed_at': observation.observed_at.isoformat() if observation.observed_at else None,
        'created_at': observation.created_at.isoformat(),
        'updated_at': observation.updated_at.isoformat(),
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
        organization = Organization.objects.get(id=data.scope.organization_id)
        project = Project.objects.get(organization=organization, id=data.project_id)
        queryset = Observation.objects.filter(organization=organization, project=project)
        filter_data = {
            'team_id': data.team_id,
            'observation_type': data.observation_type,
            'session_id': data.session_id,
            'since': data.since,
            'until': data.until,
            'correlation_id': data.correlation_id,
        }
        queryset = ObservationFilterSet(data=filter_data, queryset=queryset).qs

        offset = data.offset
        limit = data.limit
        observations = tuple(queryset.order_by('-observed_at', '-created_at')[offset : offset + limit])

        return ObservationListResult(observations=observations)


class GetObservation:
    def execute(self, data: ObservationDetailInput) -> Observation:
        organization = Organization.objects.get(id=data.scope.organization_id)
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
