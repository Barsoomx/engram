import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError

from engram.core.models import (
    Agent,
    AgentSession,
    ContextBundle,
    ContextBundleItem,
    Memory,
    MemoryVersion,
    Observation,
    ObservationSource,
    Organization,
    OutboxEvent,
    Project,
    RawEventEnvelope,
    RetrievalDocument,
    Team,
)


def create_scope() -> tuple[Organization, Team, Project, Agent, AgentSession]:
    organization = Organization.objects.create(name='Engram', slug='engram')
    team = Team.objects.create(organization=organization, name='Platform', slug='platform')
    project = Project.objects.create(organization=organization, name='Backend', slug='backend')
    agent = Agent.objects.create(organization=organization, runtime='codex', external_id='agent-1')
    session = AgentSession.objects.create(
        organization=organization,
        project=project,
        team=team,
        agent=agent,
        external_session_id='session-1',
        runtime='codex',
    )

    return organization, team, project, agent, session


@pytest.mark.django_db
def test_sessions_are_unique_inside_project_scope() -> None:
    organization, team, first_project, agent, _session = create_scope()
    second_project = Project.objects.create(organization=organization, name='CLI', slug='cli')

    second_session = AgentSession.objects.create(
        organization=organization,
        project=second_project,
        team=team,
        agent=agent,
        external_session_id='session-1',
        runtime='codex',
    )

    assert second_session.external_session_id == 'session-1'
    assert second_session.project_id != first_project.id

    with pytest.raises(IntegrityError):
        AgentSession.objects.create(
            organization=organization,
            project=first_project,
            team=team,
            agent=agent,
            external_session_id='session-1',
            runtime='codex',
        )


@pytest.mark.django_db
def test_sessions_preserve_upstream_content_and_memory_session_ids() -> None:
    organization = Organization.objects.create(name='Engram', slug='engram')
    project = Project.objects.create(organization=organization, name='Backend', slug='backend')
    agent = Agent.objects.create(organization=organization, runtime='claude_code', external_id='agent-1')

    session = AgentSession.objects.create(
        organization=organization,
        project=project,
        agent=agent,
        external_session_id='hook-session-1',
        content_session_id='content-session-1',
        memory_session_id='memory-session-1',
        platform_source='claude_code',
        runtime='claude_code',
    )

    assert session.content_session_id == 'content-session-1'
    assert session.memory_session_id == 'memory-session-1'
    assert session.platform_source == 'claude_code'


@pytest.mark.django_db
def test_raw_event_duplicate_replay_is_scoped_to_session_event_id() -> None:
    organization, team, project, agent, session = create_scope()

    RawEventEnvelope.objects.create(
        organization=organization,
        project=project,
        team=team,
        agent=agent,
        session=session,
        event_type='post_tool_use',
        client_event_id='event-1',
        idempotency_key='event-1-key',
        content_hash='hash-1',
        runtime='codex',
        payload={'tool_name': 'bash'},
    )

    with pytest.raises(IntegrityError):
        RawEventEnvelope.objects.create(
            organization=organization,
            project=project,
            team=team,
            agent=agent,
            session=session,
            event_type='post_tool_use',
            client_event_id='event-1',
            idempotency_key='event-1-replay-key',
            content_hash='hash-1',
            runtime='codex',
            payload={'tool_name': 'bash'},
        )


@pytest.mark.django_db
def test_observations_dedupe_by_session_and_content_hash() -> None:
    organization, team, project, agent, session = create_scope()

    Observation.objects.create(
        organization=organization,
        project=project,
        team=team,
        agent=agent,
        session=session,
        observation_type='decision',
        title='Use server-side memory',
        content_hash='observation-hash',
    )

    with pytest.raises(IntegrityError):
        Observation.objects.create(
            organization=organization,
            project=project,
            team=team,
            agent=agent,
            session=session,
            observation_type='decision',
            title='Use server-side memory',
            content_hash='observation-hash',
        )


@pytest.mark.django_db
def test_observation_sources_preserve_provenance_with_scoped_uniqueness() -> None:
    organization, team, project, agent, session = create_scope()
    raw_event = RawEventEnvelope.objects.create(
        organization=organization,
        project=project,
        team=team,
        agent=agent,
        session=session,
        event_type='post_tool_use',
        client_event_id='event-source-1',
        idempotency_key='event-source-1-key',
        content_hash='event-source-hash',
        runtime='codex',
        payload={'tool_name': 'bash'},
    )
    observation = Observation.objects.create(
        organization=organization,
        project=project,
        team=team,
        agent=agent,
        session=session,
        raw_event=raw_event,
        observation_type='decision',
        title='Record source links',
        content_hash='source-observation-hash',
    )

    source = ObservationSource.objects.create(
        organization=organization,
        project=project,
        observation=observation,
        raw_event=raw_event,
        source_type='hook_event',
        source_id='event-source-1',
        citation='E1',
        metadata={'tool_name': 'bash'},
    )

    assert source.citation == 'E1'
    assert source.metadata == {'tool_name': 'bash'}

    with pytest.raises(IntegrityError):
        ObservationSource.objects.create(
            organization=organization,
            project=project,
            observation=observation,
            raw_event=raw_event,
            source_type='hook_event',
            source_id='event-source-1',
            citation='E1-replay',
        )


@pytest.mark.django_db
def test_retrieval_document_scope_must_match_memory_version_scope() -> None:
    organization, team, project, _agent, _session = create_scope()
    other_project = Project.objects.create(organization=organization, name='CLI', slug='cli')
    memory = Memory.objects.create(
        organization=organization,
        project=project,
        team=team,
        title='Server memory is authoritative',
        body='Do not use local SQLite as the runtime source of truth.',
        visibility_scope='project',
    )
    version = MemoryVersion.objects.create(
        organization=organization,
        project=project,
        memory=memory,
        version=1,
        body=memory.body,
        content_hash='memory-version-hash',
    )
    retrieval_document = RetrievalDocument(
        organization=organization,
        project=other_project,
        team=team,
        memory=memory,
        memory_version=version,
        visibility_scope='project',
        full_text='Do not use local SQLite as the runtime source of truth.',
    )

    with pytest.raises(ValidationError):
        retrieval_document.full_clean()


@pytest.mark.django_db
def test_context_bundle_items_store_citations_and_scope_evidence() -> None:
    organization, team, project, agent, session = create_scope()
    memory = Memory.objects.create(
        organization=organization,
        project=project,
        team=team,
        title='Context bundles need citations',
        body='Every injected memory must include provenance.',
        visibility_scope='project',
    )
    version = MemoryVersion.objects.create(
        organization=organization,
        project=project,
        memory=memory,
        version=1,
        body=memory.body,
        content_hash='citation-version-hash',
    )
    retrieval_document = RetrievalDocument.objects.create(
        organization=organization,
        project=project,
        team=team,
        memory=memory,
        memory_version=version,
        visibility_scope='project',
        full_text='Every injected memory must include provenance.',
    )
    bundle = ContextBundle.objects.create(
        organization=organization,
        project=project,
        team=team,
        agent=agent,
        session=session,
        request_id='context-request-1',
        purpose='session_start',
        rendered_text='Memory: Every injected memory must include provenance. [M1]',
        authorization_scope={'project_id': str(project.id), 'capabilities': ['memories:read']},
        selected_count=1,
    )
    item = ContextBundleItem.objects.create(
        bundle=bundle,
        organization=organization,
        project=project,
        memory=memory,
        retrieval_document=retrieval_document,
        rank=1,
        citation='M1',
        inclusion_reason='matched project memory',
        scope_evidence={'visibility_scope': 'project'},
    )

    assert item.citation == 'M1'
    assert item.scope_evidence == {'visibility_scope': 'project'}


@pytest.mark.django_db
def test_outbox_idempotency_is_unique_per_event_type() -> None:
    organization, team, project, _agent, _session = create_scope()

    OutboxEvent.objects.create(
        organization=organization,
        project=project,
        team=team,
        aggregate_type='observation',
        aggregate_id='observation-1',
        event_type='ObservationRecorded',
        idempotency_key='observation-1',
        payload={'observation_id': 'observation-1'},
    )
    OutboxEvent.objects.create(
        organization=organization,
        project=project,
        team=team,
        aggregate_type='observation',
        aggregate_id='observation-1',
        event_type='RetrievalDocumentRefreshRequested',
        idempotency_key='observation-1',
        payload={'observation_id': 'observation-1'},
    )

    with pytest.raises(IntegrityError):
        OutboxEvent.objects.create(
            organization=organization,
            project=project,
            team=team,
            aggregate_type='observation',
            aggregate_id='observation-1',
            event_type='ObservationRecorded',
            idempotency_key='observation-1',
            payload={'observation_id': 'observation-1'},
        )
