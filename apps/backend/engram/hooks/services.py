from __future__ import annotations

import uuid
from dataclasses import dataclass

import structlog
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from engram.access.services import AccessDeniedError, EffectiveScope, ResolveApiKeyScope
from engram.core.models import (
    Agent,
    AgentSession,
    Observation,
    ObservationSource,
    Organization,
    OrganizationSettings,
    Project,
    RawEventEnvelope,
    RawEventNormalizationDisposition,
    Runtime,
    SessionStatus,
    Team,
    WorkflowSubjectType,
    WorkflowWork,
    WorkflowWorkType,
)
from engram.core.redaction import RedactionResult, redact_value
from engram.core.repository import resolve_project_for_scope
from engram.memory.observation_work import (
    allocate_observation_sequence,
    lock_session_for_observation,
    session_has_observation_history,
)
from engram.memory.tasks import dispatch_work_task, distill_session, process_observation_work_v1
from engram.memory.workflow_work import CreateWorkflowWorkInput, create_work, observation_content_digest

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
        payload_result = redact_hook_value(data.payload)
        redacted_payload = payload_result.value if isinstance(payload_result.value, dict) else {}

        try:
            with transaction.atomic():
                duplicate = self._find_duplicate(organization, project, team, data)
                if duplicate is not None:
                    return self._handle_duplicate(
                        duplicate,
                        organization=organization,
                        project=project,
                        requested_team=team,
                    )

                with transaction.atomic():
                    agent = self._get_or_create_agent(organization, data)
                    session = self._get_or_create_session(organization, project, team, agent, data)
                    duplicate = self._find_duplicate(organization, project, team, data)
                    if duplicate is not None:
                        transaction.set_rollback(True)
                if duplicate is not None:
                    return self._handle_duplicate(
                        duplicate,
                        organization=organization,
                        project=project,
                        requested_team=team,
                    )

                team = session.team

                session_was_active = session.status == SessionStatus.ACTIVE
                existing_observation = Observation.objects.filter(
                    organization=organization,
                    project=project,
                    session=session,
                    content_hash=data.content_hash,
                ).first()
                observation_values = self._observation_values(data, redacted_payload)
                self._validate_observation_reuse(existing_observation, observation_values, data.event_type)
                sequence_number = (
                    existing_observation.session_sequence
                    if existing_observation is not None and existing_observation.session_sequence is not None
                    else allocate_observation_sequence(session)
                )

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
                    normalization_contract_version=1,
                    normalization_disposition=RawEventNormalizationDisposition.OBSERVATION,
                    normalization_reason=None,
                    sequence_number=sequence_number,
                    occurred_at=data.occurred_at,
                    payload=redacted_payload,
                    headers={},
                    request_id=data.request_id,
                    correlation_id=data.correlation_id,
                    trace_id=data.trace_id,
                    actor_type=scope.actor_type,
                    actor_id=scope.actor_id,
                    metadata=self._raw_event_metadata(
                        data,
                        payload_result,
                        policy=self._work_policy(organization),
                    ),
                )
                observation = self._get_or_create_observation(
                    organization,
                    project,
                    team,
                    agent,
                    session,
                    raw_event,
                    observation_values,
                    sequence_number=raw_event.sequence_number,
                )
                self._ensure_hook_source(
                    organization=organization,
                    project=project,
                    observation=observation,
                    raw_event=raw_event,
                    source_id=data.event_id,
                    event_type=data.event_type,
                    repair_missing_raw=False,
                )
                observation, trusted_event_type, policy = self._validate_typed_hook_evidence(
                    raw_event,
                    organization=organization,
                    project=project,
                    session=session,
                )
                observation_id = str(observation.id)
                if trusted_event_type == 'session_end':
                    session.status = SessionStatus.ENDED
                    session.ended_at = data.occurred_at or timezone.now()
                    session.save(update_fields=['status', 'ended_at', 'updated_at'])
                if trusted_event_type not in {'session_start', 'session_end'} and policy['realtime_candidates_enabled']:
                    work, created = self._create_observation_work(
                        organization=organization,
                        project=project,
                        observation=observation,
                        policy=policy,
                    )
                    if created:
                        dispatch_work_task(process_observation_work_v1, work.id)
                if trusted_event_type == 'session_end' and session_was_active:
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
            with transaction.atomic():
                duplicate = self._find_duplicate(organization, project, team, data)
                if duplicate is not None:
                    return self._handle_duplicate(
                        duplicate,
                        organization=organization,
                        project=project,
                        requested_team=team,
                    )
                self._raise_if_foreign_identity_collision(organization, project, team, data)

            raise

    def _handle_duplicate(
        self,
        raw_event: RawEventEnvelope,
        *,
        organization: Organization,
        project: Project,
        requested_team: Team | None,
    ) -> HookIngestResult:
        session = lock_session_for_observation(
            organization_id=organization.id,
            project_id=project.id,
            session_id=raw_event.session_id,
        )
        raw_event = RawEventEnvelope.objects.get(
            id=raw_event.id,
            organization=organization,
            project=project,
            session=session,
        )
        requested_team_id = requested_team.id if requested_team is not None else None
        if raw_event.team_id != session.team_id or raw_event.team_id != requested_team_id:
            raise AccessDeniedError('team_scope_denied', 'Hook duplicate is outside effective team scope')
        self._validate_duplicate_producer(raw_event)
        observation = self._repair_duplicate(
            raw_event,
            organization=organization,
            project=project,
            session=session,
        )

        return self._existing_result(raw_event, observation)

    def _repair_duplicate(
        self,
        raw_event: RawEventEnvelope,
        *,
        organization: Organization,
        project: Project,
        session: AgentSession,
    ) -> Observation:
        if raw_event.normalization_contract_version == 1:
            observation, trusted_event_type, policy = self._validate_typed_hook_evidence(
                raw_event,
                organization=organization,
                project=project,
                session=session,
            )
        else:
            normalization_state = (
                raw_event.normalization_contract_version,
                raw_event.normalization_disposition,
                raw_event.normalization_reason,
            )
            if normalization_state != (None, None, None):
                raise ValueError('malformed legacy hook normalization state')
            direct_observations = list(raw_event.observations.order_by('created_at')[:2])
            if len(direct_observations) != 1:
                raise ValueError('legacy hook evidence requires exactly one direct observation')
            observation = direct_observations[0]
            self._validate_hook_observation_scope(
                raw_event,
                observation,
                organization=organization,
                project=project,
                session=session,
            )

            trusted_event_type = self._trusted_duplicate_event_type(raw_event, observation)
            policy, policy_is_new = self._duplicate_work_policy(raw_event, organization)
            self._ensure_hook_source(
                organization=organization,
                project=project,
                observation=observation,
                raw_event=raw_event,
                source_id=raw_event.client_event_id,
                event_type=trusted_event_type,
            )
            if policy_is_new:
                raw_event.metadata = {**raw_event.metadata, 'work_policy_v1': policy}
                raw_event.save(update_fields=['metadata', 'updated_at'])
        if trusted_event_type in {'session_start', 'session_end'} or not policy['realtime_candidates_enabled']:
            return observation
        work, created = self._create_observation_work(
            organization=organization,
            project=project,
            observation=observation,
            policy=policy,
        )
        if created:
            dispatch_work_task(process_observation_work_v1, work.id)

        return observation

    def _validate_typed_hook_evidence(
        self,
        raw_event: RawEventEnvelope,
        *,
        organization: Organization,
        project: Project,
        session: AgentSession,
    ) -> tuple[Observation, str, dict[str, object]]:
        policy = self._typed_work_policy(raw_event)

        direct_sources = list(raw_event.observation_sources.select_related('observation').order_by('created_at')[:2])
        if len(direct_sources) != 1:
            raise ValueError('typed hook evidence requires exactly one direct hook source')
        source = direct_sources[0]
        observation = source.observation
        self._validate_hook_observation_scope(
            raw_event,
            observation,
            organization=organization,
            project=project,
            session=session,
        )

        trusted_event_type = self._trusted_duplicate_event_type(raw_event, observation)
        if (
            raw_event.source_adapter not in Runtime.values
            or source.organization_id != organization.id
            or source.project_id != project.id
            or source.raw_event_id != raw_event.id
            or source.source_type != 'hook_event'
            or source.source_id != raw_event.client_event_id
            or not isinstance(source.metadata, dict)
            or source.metadata.get('event_type') != trusted_event_type
        ):
            raise ValueError('malformed typed hook source')
        if Observation.objects.filter(raw_event_id=raw_event.id).exclude(id=observation.id).exists():
            raise ValueError('ambiguous typed hook direct observation')

        return observation, trusted_event_type, policy

    def _validate_hook_observation_scope(
        self,
        raw_event: RawEventEnvelope,
        observation: Observation,
        *,
        organization: Organization,
        project: Project,
        session: AgentSession,
    ) -> None:
        expected_scope = (
            organization.id,
            project.id,
            session.id,
            session.team_id,
            session.agent_id,
        )
        if (
            raw_event.organization_id,
            raw_event.project_id,
            raw_event.session_id,
            raw_event.team_id,
            raw_event.agent_id,
        ) != expected_scope or (
            observation.organization_id,
            observation.project_id,
            observation.session_id,
            observation.team_id,
            observation.agent_id,
        ) != expected_scope:
            raise ValueError('mismatched hook observation scope')
        if raw_event.content_hash != observation.content_hash:
            raise ValueError('mismatched hook observation content')

    def _typed_work_policy(self, raw_event: RawEventEnvelope) -> dict[str, object]:
        if (
            raw_event.normalization_contract_version != 1
            or raw_event.normalization_disposition != RawEventNormalizationDisposition.OBSERVATION
            or raw_event.normalization_reason is not None
        ):
            raise ValueError('malformed typed hook normalization disposition')
        metadata = raw_event.metadata
        if not isinstance(metadata, dict) or 'work_policy_v1' not in metadata:
            raise ValueError('missing work_policy_v1')
        policy = metadata['work_policy_v1']
        if not self._valid_work_policy(policy) or policy['legacy_policy_fallback'] is not False:
            raise ValueError('malformed work_policy_v1')

        return policy

    def _trusted_duplicate_event_type(self, raw_event: RawEventEnvelope, observation: Observation) -> str:
        observation_metadata = observation.source_metadata
        trusted_event_type = observation_metadata.get('event_type') if isinstance(observation_metadata, dict) else None
        if not isinstance(trusted_event_type, str) or not trusted_event_type:
            raise ValueError('malformed persisted observation event type')
        if not isinstance(raw_event.event_type, str) or raw_event.event_type != trusted_event_type:
            raise ValueError('mismatched persisted observation event type')

        return trusted_event_type

    def _duplicate_work_policy(
        self,
        raw_event: RawEventEnvelope,
        organization: Organization,
    ) -> tuple[dict[str, object], bool]:
        metadata = raw_event.metadata
        if not isinstance(metadata, dict):
            raise ValueError('malformed hook duplicate metadata')
        if 'work_policy_v1' not in metadata:
            if raw_event.normalization_contract_version is not None:
                raise ValueError('missing work_policy_v1')

            return self._work_policy(organization, legacy_policy_fallback=True), True

        policy = metadata['work_policy_v1']
        if not self._valid_work_policy(policy):
            raise ValueError('malformed work_policy_v1')

        return policy, False

    def _work_policy(self, organization: Organization, *, legacy_policy_fallback: bool = False) -> dict[str, object]:
        return {
            'schema': 'hook_work_policy/v1',
            'realtime_candidates_enabled': self._realtime_candidates_enabled(organization),
            'legacy_policy_fallback': legacy_policy_fallback,
        }

    def _valid_work_policy(self, value: object) -> bool:
        return (
            isinstance(value, dict)
            and set(value) == {'schema', 'realtime_candidates_enabled', 'legacy_policy_fallback'}
            and value['schema'] == 'hook_work_policy/v1'
            and type(value['realtime_candidates_enabled']) is bool
            and type(value['legacy_policy_fallback']) is bool
        )

    def _create_observation_work(
        self,
        *,
        organization: Organization,
        project: Project,
        observation: Observation,
        policy: dict[str, object],
    ) -> tuple[WorkflowWork, bool]:
        return create_work(
            CreateWorkflowWorkInput(
                organization_id=organization.id,
                project_id=project.id,
                work_type=WorkflowWorkType.OBSERVATION_PROCESSING,
                subject_type=WorkflowSubjectType.OBSERVATION,
                subject_id=observation.id,
                input_snapshot={
                    'schema': 'observation_processing_input/v1',
                    'observation_id': str(observation.id),
                    'observation_digest': observation_content_digest(observation),
                    'policy': policy,
                },
            ),
        )

    def _ensure_hook_source(
        self,
        *,
        organization: Organization,
        project: Project,
        observation: Observation,
        raw_event: RawEventEnvelope,
        source_id: str,
        event_type: str,
        repair_missing_raw: bool = True,
    ) -> ObservationSource:
        source, created = ObservationSource.objects.get_or_create(
            organization=organization,
            project=project,
            observation=observation,
            source_type='hook_event',
            source_id=source_id,
            defaults={
                'raw_event': raw_event,
                'citation': source_id,
                'metadata': {'event_type': event_type},
            },
        )
        if repair_missing_raw and not created and source.raw_event_id is None:
            source.raw_event = raw_event
            source.save(update_fields=['raw_event', 'updated_at'])
        elif not created and source.raw_event_id not in {None, raw_event.id}:
            raise ValueError('hook source is bound to a different raw event')

        return source

    def _realtime_candidates_enabled(self, organization: Organization) -> bool:
        return bool(
            OrganizationSettings.objects.filter(organization=organization)
            .values_list('realtime_candidates_enabled', flat=True)
            .first(),
        )

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

    def _raw_event_metadata(
        self,
        data: HookEventInput,
        payload_result: RedactionResult,
        *,
        policy: dict[str, object],
    ) -> dict[str, object]:
        metadata: dict[str, object] = {
            'repository_url': data.repository_url,
            'repository_root': data.repository_root,
            'branch': data.branch,
            'cwd': data.cwd,
        }
        if payload_result.redacted:
            metadata['redaction'] = {'payload': True}

        metadata['work_policy_v1'] = policy

        return metadata

    def _find_duplicate(
        self,
        organization: Organization,
        project: Project,
        team: Team | None,
        data: HookEventInput,
    ) -> RawEventEnvelope | None:
        team_id = team.id if team is not None else None
        duplicate = RawEventEnvelope.objects.filter(
            organization=organization,
            project=project,
            team_id=team_id,
            idempotency_key=data.idempotency_key,
        ).first()
        if duplicate is not None:
            return duplicate

        session = AgentSession.objects.filter(
            organization=organization,
            project=project,
            team_id=team_id,
            external_session_id=data.session_id,
        ).first()
        if session is None:
            return None

        return RawEventEnvelope.objects.filter(
            organization=organization,
            project=project,
            team_id=team_id,
            session=session,
            client_event_id=data.event_id,
        ).first()

    def _raise_if_foreign_identity_collision(
        self,
        organization: Organization,
        project: Project,
        team: Team | None,
        data: HookEventInput,
    ) -> None:
        if team is None:
            raw_outside_team = Q(team_id__isnull=False)
            session_outside_team = Q(session__team_id__isnull=False)
        else:
            raw_outside_team = Q(team_id__isnull=True) | ~Q(team_id=team.id)
            session_outside_team = Q(session__team_id__isnull=True) | ~Q(session__team_id=team.id)
        foreign_identity = Q(idempotency_key=data.idempotency_key) & raw_outside_team
        foreign_identity |= Q(
            session__external_session_id=data.session_id,
            client_event_id=data.event_id,
        ) & (raw_outside_team | session_outside_team)
        if (
            RawEventEnvelope.objects.filter(
                organization=organization,
                project=project,
            )
            .filter(foreign_identity)
            .exists()
        ):
            raise AccessDeniedError(
                'hook_identity_collision',
                'Hook identity collision is not accessible',
            ) from None

    def _validate_duplicate_producer(self, raw_event: RawEventEnvelope) -> None:
        if (
            raw_event.source_adapter == 'claude_mem'
            or raw_event.observation_sources.exclude(source_type='hook_event').exists()
        ):
            raise ValueError('hook duplicate is owned by another producer')

    def _existing_result(
        self,
        raw_event: RawEventEnvelope,
        observation: Observation,
    ) -> HookIngestResult:
        if raw_event.content_hash != observation.content_hash:
            raise ValueError('hook duplicate observation content mismatch')

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
                'observation_sequence_cursor': 0,
            },
        )
        if transaction.get_connection().in_atomic_block:
            session = lock_session_for_observation(
                organization_id=organization.id,
                project_id=project.id,
                session_id=session.id,
            )
        selected_team = team
        if not created:
            selected_team = self._existing_session_team(session, team)
            identity_conflict = (
                session.agent_id != agent.id
                or session.runtime != data.agent_runtime
                or (bool(session.platform_source) and session.platform_source != data.agent_runtime)
            )
            if identity_conflict and session_has_observation_history(session_id=session.id):
                raise AccessDeniedError(
                    'hook_identity_collision',
                    'Hook identity collision is not accessible',
                )
        update_fields = []
        for field, value in (
            ('team', selected_team),
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

    def _existing_session_team(self, session: AgentSession, requested_team: Team | None) -> Team | None:
        if session.team_id is None:
            if requested_team is not None and session_has_observation_history(session_id=session.id):
                raise AccessDeniedError(
                    'hook_identity_collision',
                    'Hook identity collision is not accessible',
                )

            return requested_team
        if requested_team is None:
            raise AccessDeniedError('team_scope_denied', 'Hook session is outside effective team scope')
        if session.team_id != requested_team.id:
            raise AccessDeniedError(
                'hook_identity_collision',
                'Hook identity collision is not accessible',
            )

        return session.team

    def _get_or_create_observation(
        self,
        organization: Organization,
        project: Project,
        team: Team | None,
        agent: Agent,
        session: AgentSession,
        raw_event: RawEventEnvelope,
        values: dict[str, object],
        sequence_number: int,
    ) -> Observation:
        observation, created = Observation.objects.get_or_create(
            organization=organization,
            project=project,
            session=session,
            content_hash=raw_event.content_hash,
            defaults={
                'team': team,
                'agent': agent,
                'raw_event': raw_event,
                **values,
                'session_sequence': sequence_number,
            },
        )
        if created:
            return observation
        update_fields = []
        if observation.raw_event_id is None:
            observation.raw_event = raw_event
            update_fields.append('raw_event')
        if observation.session_sequence is None:
            observation.session_sequence = sequence_number
            update_fields.append('session_sequence')
        if update_fields:
            update_fields.append('updated_at')
            observation.save(update_fields=update_fields)

        return observation

    def _observation_values(self, data: HookEventInput, payload: dict[str, object]) -> dict[str, object]:
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

        return {
            'observation_type': observation_type,
            'title': str(redacted_title.value)[:255],
            'body': str(redacted_body.value),
            'files_read': list(redacted_files_read.value),
            'files_modified': list(redacted_files_modified.value),
            'redaction_metadata': {'redacted': True} if redacted else {},
            'source_metadata': {'event_type': data.event_type},
            'observed_at': data.occurred_at,
        }

    def _validate_observation_reuse(
        self,
        observation: Observation | None,
        values: dict[str, object],
        event_type: str,
    ) -> None:
        if observation is None:
            return

        canonical_fields = (
            'observation_type',
            'title',
            'subtitle',
            'body',
            'facts',
            'narrative',
            'concepts',
            'files_read',
            'files_modified',
        )
        defaults: dict[str, object] = {
            'subtitle': '',
            'facts': [],
            'narrative': '',
            'concepts': [],
        }
        if any(getattr(observation, field) != values.get(field, defaults.get(field)) for field in canonical_fields):
            raise ValueError('content hash collision with different canonical redacted content')

        persisted_event_type = observation.source_metadata.get('event_type')
        lifecycle_types = {'session_start', 'session_end'}
        if not isinstance(persisted_event_type, str) or (
            (persisted_event_type in lifecycle_types) != (event_type in lifecycle_types)
        ):
            raise ValueError('content hash collision across trusted lifecycle class')
        if persisted_event_type != event_type:
            raise ValueError('content hash collision across trusted event type')

    def _fallback_title(self, data: HookEventInput, payload: dict[str, object] | None = None) -> str:
        tool_name = (payload or data.payload).get('tool_name')
        if isinstance(tool_name, str) and tool_name:
            return f'{data.event_type}: {tool_name}'

        return data.event_type


def redact_hook_value(value: object) -> RedactionResult:
    return redact_value(value)
