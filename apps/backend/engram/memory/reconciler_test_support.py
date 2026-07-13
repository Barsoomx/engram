from __future__ import annotations

import uuid

from django.utils import timezone

from engram.core.models import AgentSession, Observation, Organization, Project, WorkflowWork
from engram.memory.candidate_work_reconciler import CandidateDecisionWorkInput
from engram.memory.session_lifecycle import EndSession

Scope = tuple[Organization, Project, AgentSession]


class StubBuilder:
    def __init__(
        self,
        inputs: dict[uuid.UUID, CandidateDecisionWorkInput],
        works_by_manifest: dict[str, WorkflowWork | None],
    ) -> None:
        self._inputs = inputs
        self._works_by_manifest = works_by_manifest

    def expected_input(self, *, candidate_id: uuid.UUID) -> CandidateDecisionWorkInput:
        return self._inputs[candidate_id]

    def exact_work(self, *, value: CandidateDecisionWorkInput) -> WorkflowWork | None:
        return self._works_by_manifest.get(value.evidence_manifest_hash)


def ended_session_work(scope: Scope, *, sequence: int = 1) -> WorkflowWork:
    organization, project, session = scope
    Observation.objects.create(
        organization=organization,
        project=project,
        team=session.team,
        agent=session.agent,
        session=session,
        observation_type='tool_use',
        title=f'observation {sequence}',
        content_hash=f'content-{session.id}-{sequence}',
        session_sequence=sequence,
        source_metadata={'event_type': 'post_tool_use'},
    )
    result = EndSession().execute(
        organization_id=organization.id,
        project_id=project.id,
        session_id=session.id,
        ended_at=timezone.now(),
        source='explicit',
    )

    return WorkflowWork.objects.get(id=result.work_id)
