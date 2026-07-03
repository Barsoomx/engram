from __future__ import annotations

import uuid
from dataclasses import dataclass

import structlog
from django.db import IntegrityError, transaction
from django.utils import timezone

from engram.access.services import EffectiveScope, ResolveApiKeyScope
from engram.core.models import (
    Agent,
    AgentSession,
    Observation,
    ObservationSource,
    Organization,
    Project,
    RawEventEnvelope,
    SessionStatus,
    Team,
)
from engram.core.redaction import RedactionResult, redact_value
from engram.core.repository import resolve_project_for_scope
from engram.memory.tasks import distill_session, process_observation_recorded

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class HookEventInput:
    raw_key: str
    project_id: uuid.UUID | None
    team_id: uuid.UUID | None
    agent_runtime: str
    agent_version: str
    agent_external_id: str
    session_id: str
    event_id: str
    idempotency_key: str
    event_type: str
    payload_schema_version: str
    sequence_number: int | None
    occurred_at: object | None
    content_hash: str
    request_id: str
    correlation_id: str
    trace_id: str
    repository_url: str
    repository_root: str
    branch: str
    cwd: str
    payload: dict[str, object]
    observation: dict[str, object]


@dataclass(frozen=True)
class HookIngestResult:
    request_id: str
    raw_event: RawEventEnvelope
    observation: Observation
    session: AgentSession
    duplicate: bool


class IngestHookEvent:
    def execute(self, data: HookEventInput) -> HookIngestResult:
        scope = ResolveApiKeyScope().execute(
            raw_key=data.raw_key,
            required_capability='observations:write',
            requested_project_id=data.project_id,
            requested_team_id=data.team_id,
            request_id=data.request_id,
            correlation_id=data.correlation_id,
            target_type='hook_event',
            target_id=data.event_id,
        )
        organization = Organization.objects.get(id=scope.organization_id)
        project = resolve_project_for_scope(
            scope=scope,
            project_id=data.project_id,
            repository_url=data.repository_url,
            allow_create=True,
            repository_root=data.repository_root,
            request_id=data.request_id,
            correlation_id=data.correlation_id,
        )
        team = self._resolve_team(organization, data.team_id, scope)
        duplicate = self._find_duplicate(organization, project, data)
        if duplicate is not None:
            return self._existing_result(duplicate)
        payload_result = redact_hook_value(data.payload)
        redacted_payload = payload_result.value if isinstance(payload_result.value, dict) else {}

        try:
            with transaction.atomic():
                agent = self._get_or_create_agent(organization, data)
                session = self._get_or_create_session(organization, project, team, agent, data)
                session_was_active = session.status == SessionStatus.ACTIVE
                if data.event_type == 'session_end':
                    session.status = SessionStatus.ENDED
                    session.ended_at = data.occurred_at or timezone.now()
                    session.save(update_fields=['status', 'ended_at', 'updated_at'])

                raw_event = RawEventEnvelope.objects.create(
                    organization=organization,
                    project=project,
                    team=team,
                    agent=agent,
                    session=session,
                    event_type=data.event_type,
                    source_adapter=data.agent_runtime,
                    client_event_id=data.event_id,
                    idempotency_key=data.idempotency_key,
                    content_hash=data.content_hash,
                    runtime=data.agent_runtime,
                    payload_schema_version=data.payload_schema_version,
                    sequence_number=data.sequence_number,
                    occurred_at=data.occurred_at,
                    payload=redacted_payload,
                    headers={},
                    request_id=data.request_id,
                    correlation_id=data.correlation_id,
                    trace_id=data.trace_id,
                    actor_type=scope.actor_type,
                    actor_id=scope.actor_id,
                    metadata=self._raw_event_metadata(data, payload_result),
                )
                observation = self._get_or_create_observation(
                    organization,
                    project,
                    team,
                    agent,
                    session,
                    raw_event,
                    data,
                    redacted_payload,
                )
                ObservationSource.objects.get_or_create(
                    organization=organization,
                    project=project,
                    observation=observation,
                    raw_event=raw_event,
                    source_type='hook_event',
                    source_id=data.event_id,
                    defaults={'citation': data.event_id, 'metadata': {'event_type': data.event_type}},
                )
                observation_id = str(observation.id)
                transaction.on_commit(lambda: process_observation_recorded.delay(observation_id))
                if data.event_type == 'session_end' and session_was_active:
                    session_id = str(session.id)
                    transaction.on_commit(lambda: distill_session.delay(session_id))

                logger.info(
                    'hook_event_ingested',
                    organization_id=str(organization.id),
                    project_id=str(project.id),
                    session_id=str(session.id),
                    event_type=data.event_type,
                    observation_id=observation_id,
                    duplicate=False,
                )

                return HookIngestResult(
                    request_id=data.request_id,
                    raw_event=raw_event,
                    observation=observation,
                    session=session,
                    duplicate=False,
                )
        except IntegrityError:
            duplicate = self._find_duplicate(organization, project, data)
            if duplicate is not None:
                return self._existing_result(duplicate)

            raise

    def _resolve_team(
        self,
        organization: Organization,
        team_id: uuid.UUID | None,
        scope: EffectiveScope,
    ) -> Team | None:
        selected_team_id = team_id
        if selected_team_id is None and len(scope.team_ids) == 1:
            selected_team_id = scope.team_ids[0]
        if selected_team_id is None:
            return None

        return Team.objects.get(organization=organization, id=selected_team_id)

    def _raw_event_metadata(self, data: HookEventInput, payload_result: RedactionResult) -> dict[str, object]:
        metadata: dict[str, object] = {
            'repository_url': data.repository_url,
            'repository_root': data.repository_root,
            'branch': data.branch,
            'cwd': data.cwd,
        }
        if payload_result.redacted:
            metadata['redaction'] = {'payload': True}

        return metadata

    def _find_duplicate(
        self,
        organization: Organization,
        project: Project,
        data: HookEventInput,
    ) -> RawEventEnvelope | None:
        duplicate = RawEventEnvelope.objects.filter(
            organization=organization,
            project=project,
            idempotency_key=data.idempotency_key,
        ).first()
        if duplicate is not None:
            return duplicate

        session = AgentSession.objects.filter(
            organization=organization,
            project=project,
            external_session_id=data.session_id,
        ).first()
        if session is None:
            return None

        return RawEventEnvelope.objects.filter(
            organization=organization,
            project=project,
            session=session,
            client_event_id=data.event_id,
        ).first()

    def _existing_result(self, raw_event: RawEventEnvelope) -> HookIngestResult:
        observation = raw_event.observations.order_by('created_at').first()
        if observation is None:
            observation = Observation.objects.get(
                organization=raw_event.organization,
                project=raw_event.project,
                session=raw_event.session,
                content_hash=raw_event.content_hash,
            )

        return HookIngestResult(
            request_id=raw_event.request_id,
            raw_event=raw_event,
            observation=observation,
            session=raw_event.session,
            duplicate=True,
        )

    def _get_or_create_agent(self, organization: Organization, data: HookEventInput) -> Agent:
        external_id = data.agent_external_id or f'{data.agent_runtime}:default'
        agent, _created = Agent.objects.get_or_create(
            organization=organization,
            runtime=data.agent_runtime,
            external_id=external_id,
            defaults={'version': data.agent_version, 'display_name': external_id},
        )
        if data.agent_version and agent.version != data.agent_version:
            agent.version = data.agent_version
            agent.save(update_fields=['version', 'updated_at'])

        return agent

    def _get_or_create_session(
        self,
        organization: Organization,
        project: Project,
        team: Team | None,
        agent: Agent,
        data: HookEventInput,
    ) -> AgentSession:
        model_id = data.payload.get('model_id') if data.event_type == 'session_start' else None
        session, created = AgentSession.objects.get_or_create(
            organization=organization,
            project=project,
            external_session_id=data.session_id,
            defaults={
                'team': team,
                'agent': agent,
                'runtime': data.agent_runtime,
                'platform_source': data.agent_runtime,
                'repository_url': data.repository_url,
                'repository_root': data.repository_root,
                'branch': data.branch,
                'cwd': data.cwd,
                'started_at': data.occurred_at or timezone.now(),
                'model_id': model_id or '',
            },
        )
        update_fields = []
        for field, value in (
            ('team', team),
            ('agent', agent),
            ('runtime', data.agent_runtime),
            ('platform_source', data.agent_runtime),
            ('repository_url', data.repository_url),
            ('repository_root', data.repository_root),
            ('branch', data.branch),
            ('cwd', data.cwd),
            *((('model_id', model_id),) if isinstance(model_id, str) and model_id else ()),
        ):
            if getattr(session, field) != value:
                setattr(session, field, value)
                update_fields.append(field)
        if not created and session.status == SessionStatus.ENDED and data.event_type != 'session_end':
            session.status = SessionStatus.ACTIVE
            session.ended_at = None
            update_fields.append('status')
            update_fields.append('ended_at')
        if update_fields:
            update_fields.append('updated_at')
            session.save(update_fields=update_fields)

        return session

    def _get_or_create_observation(
        self,
        organization: Organization,
        project: Project,
        team: Team | None,
        agent: Agent,
        session: AgentSession,
        raw_event: RawEventEnvelope,
        data: HookEventInput,
        payload: dict[str, object],
    ) -> Observation:
        observation_data = data.observation
        observation_type = str(observation_data.get('type') or data.event_type)
        title = str(observation_data.get('title') or self._fallback_title(data, payload))
        redacted_title = redact_hook_value(title)
        redacted_body = redact_hook_value(str(observation_data.get('body') or ''))
        redacted_files_read = redact_hook_value(list(observation_data.get('files_read') or []))
        redacted_files_modified = redact_hook_value(list(observation_data.get('files_modified') or []))
        redacted = (
            redacted_title.redacted
            or redacted_body.redacted
            or redacted_files_read.redacted
            or redacted_files_modified.redacted
        )
        observation, created = Observation.objects.get_or_create(
            organization=organization,
            project=project,
            session=session,
            content_hash=data.content_hash,
            defaults={
                'team': team,
                'agent': agent,
                'raw_event': raw_event,
                'observation_type': observation_type,
                'title': str(redacted_title.value)[:255],
                'body': str(redacted_body.value),
                'files_read': list(redacted_files_read.value),
                'files_modified': list(redacted_files_modified.value),
                'redaction_metadata': {'redacted': True} if redacted else {},
                'source_metadata': {'event_type': data.event_type},
                'observed_at': data.occurred_at,
            },
        )
        if created:
            return observation
        if observation.raw_event_id is None:
            observation.raw_event = raw_event
            observation.save(update_fields=['raw_event', 'updated_at'])

        return observation

    def _fallback_title(self, data: HookEventInput, payload: dict[str, object] | None = None) -> str:
        tool_name = (payload or data.payload).get('tool_name')
        if isinstance(tool_name, str) and tool_name:
            return f'{data.event_type}: {tool_name}'

        return data.event_type


def redact_hook_value(value: object) -> RedactionResult:
    return redact_value(value)
