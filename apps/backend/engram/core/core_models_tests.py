import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from engram.core.models import (
    Agent,
    AgentSession,
    AuditEvent,
    ContextBundle,
    ContextBundleItem,
    Memory,
    MemoryCandidate,
    MemoryVersion,
    Observation,
    ObservationSource,
    Organization,
    Project,
    ProjectTeam,
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


def create_second_scope() -> tuple[Organization, Team, Project, Agent, AgentSession]:
    organization = Organization.objects.create(name='Other', slug='other')
    team = Team.objects.create(organization=organization, name='Other Platform', slug='platform')
    project = Project.objects.create(organization=organization, name='Other Backend', slug='backend')
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
def test_core_scope_uniqueness_constraints() -> None:
    organization = Organization.objects.create(name='Engram', slug='engram')
    other_organization = Organization.objects.create(name='Other', slug='other')
    team = Team.objects.create(organization=organization, name='Platform', slug='platform')
    project = Project.objects.create(organization=organization, name='Backend', slug='backend')
    Agent.objects.create(organization=organization, runtime='codex', external_id='agent-1')

    Team.objects.create(organization=other_organization, name='Other Platform', slug='platform')
    Project.objects.create(organization=other_organization, name='Other Backend', slug='backend')
    Agent.objects.create(organization=other_organization, runtime='codex', external_id='agent-1')

    with pytest.raises(IntegrityError), transaction.atomic():
        Organization.objects.create(name='Duplicate', slug='engram')

    with pytest.raises(IntegrityError), transaction.atomic():
        Team.objects.create(organization=organization, name='Duplicate Platform', slug='platform')

    with pytest.raises(IntegrityError), transaction.atomic():
        Project.objects.create(organization=organization, name='Duplicate Backend', slug='backend')

    with pytest.raises(IntegrityError), transaction.atomic():
        Agent.objects.create(organization=organization, runtime='codex', external_id='agent-1')

    ProjectTeam.objects.create(organization=organization, project=project, team=team)

    with pytest.raises(IntegrityError), transaction.atomic():
        ProjectTeam.objects.create(organization=organization, project=project, team=team)


@pytest.mark.django_db
def test_project_team_rejects_cross_organization_scope_on_create() -> None:
    organization, _team, project, _agent, _session = create_scope()
    _other_organization, other_team, _other_project, _other_agent, _other_session = create_second_scope()

    with pytest.raises(ValidationError):
        ProjectTeam.objects.create(organization=organization, project=project, team=other_team)


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
def test_session_rejects_cross_scope_project_on_create() -> None:
    organization, team, _project, agent, _session = create_scope()
    _other_organization, _other_team, other_project, _other_agent, _other_session = create_second_scope()

    with pytest.raises(ValidationError):
        AgentSession.objects.create(
            organization=organization,
            project=other_project,
            team=team,
            agent=agent,
            external_session_id='cross-scope-session',
            runtime='codex',
        )


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
def test_raw_event_rejects_cross_scope_session_on_create() -> None:
    organization, team, project, agent, _session = create_scope()
    _other_organization, _other_team, _other_project, _other_agent, other_session = create_second_scope()

    with pytest.raises(ValidationError):
        RawEventEnvelope.objects.create(
            organization=organization,
            project=project,
            team=team,
            agent=agent,
            session=other_session,
            event_type='post_tool_use',
            client_event_id='cross-event-1',
            idempotency_key='cross-event-1-key',
            content_hash='cross-event-hash',
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
def test_observation_rejects_cross_scope_raw_event_on_create() -> None:
    organization, team, project, agent, session = create_scope()
    other_organization, other_team, other_project, other_agent, other_session = create_second_scope()
    other_raw_event = RawEventEnvelope.objects.create(
        organization=other_organization,
        project=other_project,
        team=other_team,
        agent=other_agent,
        session=other_session,
        event_type='post_tool_use',
        client_event_id='other-event-1',
        idempotency_key='other-event-1-key',
        content_hash='other-event-hash',
        runtime='codex',
        payload={'tool_name': 'bash'},
    )

    with pytest.raises(ValidationError):
        Observation.objects.create(
            organization=organization,
            project=project,
            team=team,
            agent=agent,
            session=session,
            raw_event=other_raw_event,
            observation_type='decision',
            title='Cross source event',
            content_hash='cross-observation-hash',
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
def test_observation_source_rejects_cross_scope_raw_event_on_create() -> None:
    organization, team, project, agent, session = create_scope()
    other_organization, other_team, other_project, other_agent, other_session = create_second_scope()
    observation = Observation.objects.create(
        organization=organization,
        project=project,
        team=team,
        agent=agent,
        session=session,
        observation_type='decision',
        title='Record source links',
        content_hash='source-scope-observation-hash',
    )
    other_raw_event = RawEventEnvelope.objects.create(
        organization=other_organization,
        project=other_project,
        team=other_team,
        agent=other_agent,
        session=other_session,
        event_type='post_tool_use',
        client_event_id='other-source-event-1',
        idempotency_key='other-source-event-1-key',
        content_hash='other-source-event-hash',
        runtime='codex',
        payload={'tool_name': 'bash'},
    )

    with pytest.raises(ValidationError):
        ObservationSource.objects.create(
            organization=organization,
            project=project,
            observation=observation,
            raw_event=other_raw_event,
            source_type='hook_event',
            source_id='other-source-event-1',
        )


@pytest.mark.django_db
def test_memory_candidate_rejects_cross_scope_source_observation_on_create() -> None:
    organization, team, project, _agent, _session = create_scope()
    other_organization, other_team, other_project, other_agent, other_session = create_second_scope()
    other_observation = Observation.objects.create(
        organization=other_organization,
        project=other_project,
        team=other_team,
        agent=other_agent,
        session=other_session,
        observation_type='decision',
        title='Other observation',
        content_hash='other-candidate-observation-hash',
    )

    with pytest.raises(ValidationError):
        MemoryCandidate.objects.create(
            organization=organization,
            project=project,
            team=team,
            source_observation=other_observation,
            title='Cross candidate',
            body='This candidate points at another project.',
            content_hash='cross-candidate-hash',
        )


@pytest.mark.django_db
def test_memory_version_rejects_cross_scope_memory_on_create() -> None:
    organization, _team, project, _agent, _session = create_scope()
    other_organization, other_team, other_project, _other_agent, _other_session = create_second_scope()
    other_memory = Memory.objects.create(
        organization=other_organization,
        project=other_project,
        team=other_team,
        title='Other memory',
        body='Other project memory.',
        visibility_scope='project',
    )

    with pytest.raises(ValidationError):
        MemoryVersion.objects.create(
            organization=organization,
            project=project,
            memory=other_memory,
            version=1,
            body='Cross memory version.',
            content_hash='cross-memory-version-hash',
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
def test_retrieval_document_rejects_cross_scope_memory_on_create() -> None:
    organization, team, project, _agent, _session = create_scope()
    _other_organization, other_team, other_project, _other_agent, _other_session = create_second_scope()
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
        content_hash='retrieval-scope-version-hash',
    )

    with pytest.raises(ValidationError):
        RetrievalDocument.objects.create(
            organization=organization,
            project=other_project,
            team=other_team,
            memory=memory,
            memory_version=version,
            visibility_scope='project',
            full_text='Do not use local SQLite as the runtime source of truth.',
        )


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
def test_context_bundle_rejects_cross_scope_session_on_create() -> None:
    organization, team, project, agent, _session = create_scope()
    _other_organization, _other_team, _other_project, _other_agent, other_session = create_second_scope()

    with pytest.raises(ValidationError):
        ContextBundle.objects.create(
            organization=organization,
            project=project,
            team=team,
            agent=agent,
            session=other_session,
            request_id='cross-context-request',
            purpose='session_start',
        )


@pytest.mark.django_db
def test_context_bundle_item_rejects_cross_scope_retrieval_document_on_create() -> None:
    organization, team, project, agent, session = create_scope()
    other_organization, other_team, other_project, _other_agent, _other_session = create_second_scope()
    memory = Memory.objects.create(
        organization=organization,
        project=project,
        team=team,
        title='Context bundles need citations',
        body='Every injected memory must include provenance.',
        visibility_scope='project',
    )
    MemoryVersion.objects.create(
        organization=organization,
        project=project,
        memory=memory,
        version=1,
        body=memory.body,
        content_hash='context-cross-version-hash',
    )
    other_memory = Memory.objects.create(
        organization=other_organization,
        project=other_project,
        team=other_team,
        title='Other memory',
        body='Other project memory.',
        visibility_scope='project',
    )
    other_version = MemoryVersion.objects.create(
        organization=other_organization,
        project=other_project,
        memory=other_memory,
        version=1,
        body=other_memory.body,
        content_hash='other-context-cross-version-hash',
    )
    other_retrieval_document = RetrievalDocument.objects.create(
        organization=other_organization,
        project=other_project,
        team=other_team,
        memory=other_memory,
        memory_version=other_version,
        visibility_scope='project',
        full_text='Other project memory.',
    )
    bundle = ContextBundle.objects.create(
        organization=organization,
        project=project,
        team=team,
        agent=agent,
        session=session,
        request_id='cross-item-context-request',
        purpose='session_start',
    )

    with pytest.raises(ValidationError):
        ContextBundleItem.objects.create(
            bundle=bundle,
            organization=organization,
            project=project,
            memory=memory,
            retrieval_document=other_retrieval_document,
            rank=1,
            citation='M1',
        )


@pytest.mark.django_db
def test_audit_event_rejects_cross_scope_project_on_create() -> None:
    organization, _team, _project, _agent, _session = create_scope()
    _other_organization, _other_team, other_project, _other_agent, _other_session = create_second_scope()

    with pytest.raises(ValidationError):
        AuditEvent.objects.create(
            organization=organization,
            project=other_project,
            event_type='MemoryRetrieved',
            actor_type='agent',
            result='allowed',
        )


@pytest.mark.django_db
def test_memory_kind_is_populated_from_metadata_on_create() -> None:
    organization, team, project, _agent, _session = create_scope()

    memory = Memory.objects.create(
        organization=organization,
        project=project,
        team=team,
        title='Digest memory',
        body='Daily digest body.',
        metadata={'kind': 'digest'},
    )

    assert memory.kind == 'digest'


@pytest.mark.django_db
def test_memory_kind_defaults_to_empty_string_when_metadata_has_no_kind() -> None:
    organization, team, project, _agent, _session = create_scope()

    memory = Memory.objects.create(
        organization=organization,
        project=project,
        team=team,
        title='Plain memory',
        body='No kind here.',
    )

    assert memory.kind == ''


def index_field_sets(model: type) -> set[tuple[str, ...]]:
    return {tuple(index.fields) for index in model._meta.indexes}


def test_observation_has_observed_at_created_at_composite_index() -> None:
    assert ('organization', 'project', 'observed_at', 'created_at') in index_field_sets(Observation)


def test_audit_event_has_organization_created_at_composite_index() -> None:
    assert ('organization', 'created_at') in index_field_sets(AuditEvent)


def test_audit_event_has_organization_project_created_at_composite_index() -> None:
    assert ('organization', 'project', 'created_at') in index_field_sets(AuditEvent)


def test_memory_has_status_updated_at_composite_index() -> None:
    assert ('organization', 'project', 'status', 'updated_at') in index_field_sets(Memory)


def test_memory_has_created_at_composite_index() -> None:
    assert ('organization', 'project', 'created_at') in index_field_sets(Memory)


def test_memory_has_kind_composite_index() -> None:
    assert ('organization', 'project', 'kind') in index_field_sets(Memory)


def test_context_bundle_has_created_at_composite_index() -> None:
    assert ('organization', 'project', 'created_at') in index_field_sets(ContextBundle)


def test_agent_session_has_updated_at_composite_index() -> None:
    assert ('organization', 'project', 'updated_at') in index_field_sets(AgentSession)


@pytest.mark.django_db
def test_retrieval_document_defaults_to_empty_embedding_vector() -> None:
    organization = Organization.objects.create(name='Org', slug='org-embedding')
    project = Project.objects.create(organization=organization, name='P', slug='p-embedding')
    memory = Memory.objects.create(organization=organization, project=project, title='t', body='b')
    version = MemoryVersion.objects.create(
        organization=organization,
        project=project,
        memory=memory,
        version=1,
        body='b',
        content_hash='hash-embedding-default',
    )
    document = RetrievalDocument.objects.create(
        organization=organization,
        project=project,
        memory=memory,
        memory_version=version,
        full_text='t',
    )

    assert document.embedding_vector == []
