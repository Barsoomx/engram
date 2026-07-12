import uuid

import pytest
from django.apps import apps as django_apps
from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction
from django.utils import timezone

from engram.core.models import (
    MEMORY_KINDS,
    Agent,
    AgentSession,
    AuditEvent,
    ContextBundle,
    ContextBundleItem,
    Memory,
    MemoryCandidate,
    MemoryReviewExample,
    MemoryVersion,
    Observation,
    ObservationSource,
    Organization,
    Project,
    ProjectTeam,
    RawEventEnvelope,
    RetrievalDocument,
    Team,
    WorkflowRun,
    clamp_memory_kind,
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
        normalization_contract_version=0,
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
            normalization_contract_version=0,
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
            normalization_contract_version=0,
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
        session_sequence=1,
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
            session_sequence=2,
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
        normalization_contract_version=0,
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
            session_sequence=1,
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
        normalization_contract_version=0,
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
        session_sequence=1,
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
        session_sequence=1,
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
        normalization_contract_version=0,
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
        session_sequence=1,
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
def test_memory_review_example_rejects_cross_scope_project_on_create() -> None:
    organization, team, project, _agent, _session = create_scope()
    other_organization, _other_team, other_project, _other_agent, _other_session = create_second_scope()

    with pytest.raises(ValidationError):
        MemoryReviewExample.objects.create(
            organization=organization,
            project=other_project,
            team=team,
            item_type='memory_candidate',
            item_id='cand-1',
            action='approve',
        )

    with pytest.raises(ValidationError):
        MemoryReviewExample.objects.create(
            organization=other_organization,
            project=project,
            item_type='memory_candidate',
            item_id='cand-1',
            action='approve',
        )


@pytest.mark.django_db
def test_memory_review_example_rejects_cross_scope_team_on_create() -> None:
    organization, _team, project, _agent, _session = create_scope()
    _other_organization, other_team, _other_project, _other_agent, _other_session = create_second_scope()

    with pytest.raises(ValidationError):
        MemoryReviewExample.objects.create(
            organization=organization,
            project=project,
            team=other_team,
            item_type='memory_candidate',
            item_id='cand-1',
            action='approve',
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


@pytest.mark.django_db
def test_memory_kind_update_field_save_persists_kind_column() -> None:
    organization, team, project, _agent, _session = create_scope()

    memory = Memory.objects.create(
        organization=organization,
        project=project,
        team=team,
        title='Digest memory',
        body='Daily digest body.',
        metadata={'kind': 'digest'},
    )

    memory.metadata = {'kind': 'summary'}
    memory.save(update_fields=['metadata'])

    refreshed = Memory.objects.get(pk=memory.pk)
    assert refreshed.kind == 'summary'


@pytest.mark.django_db
def test_memory_kind_update_field_save_clears_kind_column() -> None:
    organization, team, project, _agent, _session = create_scope()

    memory = Memory.objects.create(
        organization=organization,
        project=project,
        team=team,
        title='Digest memory',
        body='Daily digest body.',
        metadata={'kind': 'digest'},
    )

    memory.metadata = {}
    memory.save(update_fields=['metadata'])

    refreshed = Memory.objects.get(pk=memory.pk)
    assert refreshed.kind == ''


@pytest.mark.django_db
def test_memory_kind_full_save_mirrors_changed_metadata_kind() -> None:
    organization, team, project, _agent, _session = create_scope()

    memory = Memory.objects.create(
        organization=organization,
        project=project,
        team=team,
        title='Digest memory',
        body='Daily digest body.',
        metadata={'kind': 'digest'},
    )

    memory.metadata = {'kind': 'summary'}
    memory.save()

    refreshed = Memory.objects.get(pk=memory.pk)
    assert refreshed.kind == 'summary'


def test_clamp_memory_kind_accepts_non_digest_vocabulary_values() -> None:
    for kind in MEMORY_KINDS:
        if kind == 'digest':
            continue
        assert clamp_memory_kind(kind) == kind


def test_clamp_memory_kind_rejects_digest() -> None:
    assert clamp_memory_kind('digest') == ''


def test_clamp_memory_kind_rejects_unknown_value() -> None:
    assert clamp_memory_kind('random') == ''


def test_clamp_memory_kind_rejects_none_and_empty_string() -> None:
    assert clamp_memory_kind(None) == ''
    assert clamp_memory_kind('') == ''


@pytest.mark.django_db
def test_memory_candidate_kind_defaults_to_empty_string() -> None:
    organization, team, project, _agent, _session = create_scope()

    candidate = MemoryCandidate.objects.create(
        organization=organization,
        project=project,
        team=team,
        title='Candidate without kind',
        body='Body text.',
        content_hash='candidate-kind-default-hash',
    )

    assert candidate.kind == ''


@pytest.mark.django_db
def test_memory_candidate_kind_persists_explicit_value() -> None:
    organization, team, project, _agent, _session = create_scope()

    candidate = MemoryCandidate.objects.create(
        organization=organization,
        project=project,
        team=team,
        title='Candidate with kind',
        body='Body text.',
        content_hash='candidate-kind-explicit-hash',
        kind='gotcha',
    )

    assert candidate.kind == 'gotcha'


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


def test_memory_review_example_has_org_project_created_at_composite_index() -> None:
    assert ('organization', 'project', 'created_at') in index_field_sets(MemoryReviewExample)


def test_memory_review_example_has_org_action_composite_index() -> None:
    assert ('organization', 'action') in index_field_sets(MemoryReviewExample)


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


C11Scope = tuple[Organization, Team, Project, Agent, AgentSession]


def get_workflow_work_model() -> type[models.Model]:
    return django_apps.get_model('core', 'WorkflowWork')


def create_c11_raw_event(
    scope: C11Scope,
    *,
    suffix: str,
    contract_version: int | None,
    disposition: str | None,
    reason: str | None,
) -> RawEventEnvelope:
    organization, team, project, agent, session = scope

    return RawEventEnvelope.objects.create(
        organization=organization,
        project=project,
        team=team,
        agent=agent,
        session=session,
        event_type='post_tool_use',
        client_event_id=f'c11-event-{suffix}',
        idempotency_key=f'c11-key-{suffix}',
        content_hash=f'c11-hash-{suffix}',
        runtime='codex',
        payload={'tool_name': 'bash'},
        normalization_contract_version=contract_version,
        normalization_disposition=disposition,
        normalization_reason=reason,
    )


def create_c11_observation(scope: C11Scope, *, suffix: str) -> Observation:
    organization, team, project, agent, session = scope
    session_sequence = Observation.objects.filter(session=session).count() + 1

    return Observation.objects.create(
        organization=organization,
        project=project,
        team=team,
        agent=agent,
        session=session,
        observation_type='decision',
        title=f'C1.1 observation {suffix}',
        content_hash=f'c11-observation-{suffix}',
        session_sequence=session_sequence,
    )


def create_c11_work(
    scope: C11Scope,
    *,
    subject_id: uuid.UUID,
    input_fingerprint: str = 'a' * 64,
    **overrides: object,
) -> models.Model:
    organization, team, project, _agent, _session = scope
    fields: dict[str, object] = {
        'organization': organization,
        'project': project,
        'team': team,
        'work_type': 'observation_processing',
        'subject_type': 'observation',
        'subject_id': subject_id,
        'contract_version': 1,
        'occurrence_key': '',
        'input_fingerprint': input_fingerprint,
        'input_snapshot': {'schema': 'test_input/v1'},
        'disposition': 'required',
        'resolution_reason': '',
        'resolved_at': None,
    }
    fields.update(overrides)

    return get_workflow_work_model().objects.create(**fields)


def assert_c11_save_rejected(work: models.Model, **changes: object) -> None:
    for field, value in changes.items():
        setattr(work, field, value)

    with pytest.raises(ValidationError):
        work.save()

    work.refresh_from_db()


@pytest.mark.parametrize(
    ('contract_version', 'disposition', 'reason'),
    [
        (0, None, None),
        (1, 'observation', None),
        (1, 'no_op', 'evidence_only'),
    ],
    ids=['legacy', 'v1-observation', 'v1-no-op'],
)
@pytest.mark.django_db
def test_raw_event_normalization_expand_accepts_declared_combinations(
    contract_version: int | None,
    disposition: str | None,
    reason: str | None,
) -> None:
    raw_event = create_c11_raw_event(
        create_scope(),
        suffix='valid',
        contract_version=contract_version,
        disposition=disposition,
        reason=reason,
    )

    assert (
        raw_event.normalization_contract_version,
        raw_event.normalization_disposition,
        raw_event.normalization_reason,
    ) == (contract_version, disposition, reason)


@pytest.mark.parametrize(
    ('contract_version', 'disposition', 'reason'),
    [
        (None, 'observation', None),
        (None, None, 'evidence_only'),
        (1, None, None),
        (1, 'observation', 'evidence_only'),
        (1, 'no_op', None),
        (1, 'no_op', 'unknown'),
        (1, 'unknown', None),
        (0, 'observation', None),
        (2, 'observation', None),
    ],
)
@pytest.mark.django_db
def test_raw_event_normalization_expand_rejects_partial_or_unknown_combinations(
    contract_version: int | None,
    disposition: str | None,
    reason: str | None,
) -> None:
    raw_event = create_c11_raw_event(
        create_scope(),
        suffix='invalid',
        contract_version=0,
        disposition=None,
        reason=None,
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        RawEventEnvelope.objects.filter(id=raw_event.id).update(
            normalization_contract_version=contract_version,
            normalization_disposition=disposition,
            normalization_reason=reason,
        )

    raw_event.refresh_from_db()
    assert (
        raw_event.normalization_contract_version,
        raw_event.normalization_disposition,
        raw_event.normalization_reason,
    ) == (0, None, None)


@pytest.mark.django_db
def test_sequence_expand_fields_are_nullable_but_checked_when_present() -> None:
    scope = create_scope()
    _organization, _team, _project, _agent, session = scope
    first = create_c11_observation(scope, suffix='sequence-1')
    second = create_c11_observation(scope, suffix='sequence-2')

    Observation.objects.filter(id=first.id).update(session_sequence=1)

    with pytest.raises(IntegrityError), transaction.atomic():
        Observation.objects.filter(id=second.id).update(session_sequence=1)

    with pytest.raises(IntegrityError), transaction.atomic():
        Observation.objects.filter(id=first.id).update(session_sequence=0)

    AgentSession.objects.filter(id=session.id).update(end_work_contract_version=1)

    with pytest.raises(IntegrityError), transaction.atomic():
        AgentSession.objects.filter(id=session.id).update(end_work_contract_version=2)

    cursor_field = AgentSession._meta.get_field('observation_sequence_cursor')
    end_contract_field = AgentSession._meta.get_field('end_work_contract_version')
    sequence_field = Observation._meta.get_field('session_sequence')

    assert cursor_field.null is False
    assert cursor_field.default is models.NOT_PROVIDED
    assert end_contract_field.default == 0
    assert sequence_field.null is False
    assert sequence_field.blank is False
    assert (
        'organization',
        'project',
        'normalization_contract_version',
        'normalization_disposition',
    ) in index_field_sets(RawEventEnvelope)
    assert ('organization', 'project', 'status', 'end_work_contract_version') in index_field_sets(AgentSession)
    assert ('organization', 'project', 'session', 'session_sequence') in index_field_sets(Observation)


@pytest.mark.django_db
def test_end_work_contract_version_has_both_defaults_and_rejects_null() -> None:
    _organization, _team, _project, _agent, session = create_scope()
    field = AgentSession._meta.get_field('end_work_contract_version')

    assert field.default == 0
    assert field.db_default == 0
    assert field.null is False

    with pytest.raises(IntegrityError), transaction.atomic():
        AgentSession.objects.filter(id=session.id).update(end_work_contract_version=None)


@pytest.mark.django_db
def test_workflow_work_full_identity_and_digest_occurrence_are_unique() -> None:
    scope = create_scope()
    _organization, _team, project, _agent, _session = scope
    observation = create_c11_observation(scope, suffix='identity')
    create_c11_work(scope, subject_id=observation.id)

    with pytest.raises(IntegrityError), transaction.atomic():
        create_c11_work(scope, subject_id=observation.id)

    create_c11_work(scope, subject_id=observation.id, input_fingerprint='b' * 64)
    create_c11_work(scope, subject_id=observation.id, contract_version=2)

    create_c11_work(
        scope,
        subject_id=project.id,
        team=None,
        work_type='daily_digest',
        subject_type='project',
        occurrence_key='daily:2026-07-10',
        input_fingerprint='c' * 64,
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        create_c11_work(
            scope,
            subject_id=project.id,
            team=None,
            work_type='daily_digest',
            subject_type='project',
            occurrence_key='daily:2026-07-10',
            input_fingerprint='d' * 64,
        )

    work_model = get_workflow_work_model()
    identity_constraint = next(
        constraint
        for constraint in work_model._meta.constraints
        if isinstance(constraint, models.UniqueConstraint)
        and constraint.condition is None
        and 'input_fingerprint' in constraint.fields
    )

    assert tuple(identity_constraint.fields) == (
        'organization',
        'project',
        'work_type',
        'subject_type',
        'subject_id',
        'contract_version',
        'input_fingerprint',
    )
    assert 'team' not in identity_constraint.fields
    assert {
        ('organization', 'project', 'disposition'),
        ('organization', 'project', 'work_type', 'disposition'),
        ('organization', 'project', 'subject_type', 'subject_id'),
        ('organization', 'project', 'work_type', 'occurrence_key'),
    } <= index_field_sets(work_model)


@pytest.mark.parametrize(
    'mutation',
    [
        {'contract_version': 0},
        {'input_fingerprint': 'A' * 64},
        {'input_fingerprint': 'g' * 64},
        {'input_fingerprint': 'a' * 63},
        {'subject_type': 'project'},
        {'occurrence_key': 'unexpected'},
    ],
)
@pytest.mark.django_db
def test_workflow_work_rejects_invalid_identity_values(mutation: dict[str, object]) -> None:
    scope = create_scope()
    observation = create_c11_observation(scope, suffix='invalid-identity')
    work = create_c11_work(scope, subject_id=observation.id)

    with pytest.raises(IntegrityError), transaction.atomic():
        get_workflow_work_model().objects.filter(id=work.id).update(**mutation)


@pytest.mark.django_db
def test_workflow_work_database_checks_each_work_subject_branch() -> None:
    scope = create_scope()
    organization, team, project, _agent, session = scope
    observation = create_c11_observation(scope, suffix='subject-pairs')
    observation_work = create_c11_work(scope, subject_id=observation.id)
    session_work = create_c11_work(
        scope,
        subject_id=session.id,
        work_type='session_distillation',
        subject_type='agent_session',
        input_fingerprint='b' * 64,
    )
    daily_work = create_c11_work(
        scope,
        subject_id=project.id,
        team=None,
        work_type='daily_digest',
        subject_type='project',
        occurrence_key='daily:2026-07-10',
        input_fingerprint='c' * 64,
    )
    weekly_project_work = create_c11_work(
        scope,
        subject_id=project.id,
        team=None,
        work_type='weekly_digest',
        subject_type='project',
        occurrence_key='weekly:2026-W28',
        input_fingerprint='d' * 64,
    )
    selected_team = Team.objects.create(organization=organization, name='Selected', slug='selected')
    weekly_team_work = create_c11_work(
        scope,
        subject_id=selected_team.id,
        team=selected_team,
        work_type='weekly_digest',
        subject_type='team',
        occurrence_key='weekly:2026-W28:selected',
        input_fingerprint='e' * 64,
    )
    work_model = get_workflow_work_model()
    _foreign_organization, _foreign_team, foreign_project, _foreign_agent, _foreign_session = create_second_scope()

    for work, mutation in (
        (observation_work, {'subject_type': 'agent_session'}),
        (observation_work, {'occurrence_key': 'unexpected'}),
        (session_work, {'subject_type': 'observation'}),
        (session_work, {'occurrence_key': 'unexpected'}),
        (daily_work, {'subject_id': foreign_project.id}),
        (daily_work, {'team_id': team.id}),
        (daily_work, {'occurrence_key': ''}),
        (weekly_project_work, {'subject_id': foreign_project.id}),
        (weekly_project_work, {'team_id': team.id}),
        (weekly_project_work, {'occurrence_key': ''}),
        (weekly_team_work, {'subject_id': team.id}),
        (weekly_team_work, {'occurrence_key': ''}),
    ):
        with pytest.raises(IntegrityError), transaction.atomic():
            work_model.objects.filter(id=work.id).update(**mutation)


@pytest.mark.django_db
def test_workflow_work_clean_rejects_cross_organization_project_or_team() -> None:
    scope = create_scope()
    _organization, _team, _project, _agent, _session = scope
    observation = create_c11_observation(scope, suffix='scope-clean')
    _foreign_organization, foreign_team, foreign_project, _foreign_agent, _foreign_session = create_second_scope()

    with pytest.raises(ValidationError):
        create_c11_work(
            scope,
            subject_id=foreign_project.id,
            project=foreign_project,
            team=None,
            work_type='daily_digest',
            subject_type='project',
            occurrence_key='daily:2026-07-10',
        )

    with pytest.raises(ValidationError):
        create_c11_work(scope, subject_id=observation.id, team=foreign_team)


@pytest.mark.parametrize(
    ('disposition', 'reason', 'resolved'),
    [
        ('required', '', False),
        ('complete', 'succeeded', True),
        ('complete', 'no_signal', True),
        ('no_op', 'no_input', True),
    ],
)
@pytest.mark.django_db
def test_workflow_work_accepts_valid_terminal_combinations(
    disposition: str,
    reason: str,
    resolved: bool,
) -> None:
    scope = create_scope()
    observation = create_c11_observation(scope, suffix='valid-terminal')
    work = create_c11_work(
        scope,
        subject_id=observation.id,
        disposition=disposition,
        resolution_reason=reason,
        resolved_at=timezone.now() if resolved else None,
    )

    assert work.disposition == disposition


@pytest.mark.parametrize(
    ('disposition', 'reason', 'resolved'),
    [
        ('required', 'succeeded', False),
        ('required', '', True),
        ('complete', '', False),
        ('complete', 'succeeded', False),
        ('complete', 'no_input', True),
        ('no_op', 'no_input', False),
        ('no_op', 'succeeded', True),
    ],
)
@pytest.mark.django_db
def test_workflow_work_rejects_invalid_terminal_combinations(
    disposition: str,
    reason: str,
    resolved: bool,
) -> None:
    scope = create_scope()
    observation = create_c11_observation(scope, suffix='invalid-terminal')
    work = create_c11_work(scope, subject_id=observation.id)

    with pytest.raises(IntegrityError), transaction.atomic():
        get_workflow_work_model().objects.filter(id=work.id).update(
            disposition=disposition,
            resolution_reason=reason,
            resolved_at=timezone.now() if resolved else None,
        )


@pytest.mark.django_db
def test_workflow_work_identity_snapshot_team_and_terminal_state_are_one_way() -> None:
    scope = create_scope()
    organization, team, project, _agent, _session = scope
    first_observation = create_c11_observation(scope, suffix='immutable-first')
    second_observation = create_c11_observation(scope, suffix='immutable-second')
    work = create_c11_work(scope, subject_id=first_observation.id)

    for changes in (
        {'subject_id': second_observation.id},
        {'contract_version': 2},
        {'input_fingerprint': 'b' * 64},
        {'input_snapshot': {'schema': 'changed/v1'}},
    ):
        assert_c11_save_rejected(work, **changes)

    digest = create_c11_work(
        scope,
        subject_id=project.id,
        team=None,
        work_type='weekly_digest',
        subject_type='project',
        occurrence_key='weekly:2026-W28',
        input_fingerprint='c' * 64,
    )
    assert_c11_save_rejected(digest, work_type='daily_digest')
    assert_c11_save_rejected(digest, occurrence_key='weekly:2026-W29')
    other_project = Project.objects.create(organization=organization, name='Other project', slug='immutable-project')
    assert_c11_save_rejected(digest, project=other_project, subject_id=other_project.id)
    assert_c11_save_rejected(digest, subject_type='team', subject_id=team.id, team=team)

    team_work = create_c11_work(
        scope,
        subject_id=team.id,
        team=team,
        work_type='weekly_digest',
        subject_type='team',
        occurrence_key='weekly:2026-W28:team',
        input_fingerprint='d' * 64,
    )
    foreign_organization, foreign_team, foreign_project, _foreign_agent, _foreign_session = create_second_scope()
    assert_c11_save_rejected(
        team_work,
        organization=foreign_organization,
        project=foreign_project,
        team=foreign_team,
        subject_id=foreign_team.id,
    )

    work.disposition = 'complete'
    work.resolution_reason = 'succeeded'
    work.resolved_at = timezone.now()
    work.save()
    work.refresh_from_db()

    assert work.disposition == 'complete'

    assert_c11_save_rejected(work, disposition='required', resolution_reason='', resolved_at=None)


@pytest.mark.django_db
def test_workflow_run_link_requires_matching_scope_team_and_type() -> None:
    scope = create_scope()
    organization, team, project, _agent, _session = scope
    observation = create_c11_observation(scope, suffix='linked-run')
    work = create_c11_work(scope, subject_id=observation.id)
    linked = WorkflowRun.objects.create(
        organization=organization,
        project=project,
        team=team,
        work=work,
        run_type='observation_processing',
        status='queued',
    )
    legacy = WorkflowRun.objects.create(
        organization=organization,
        project=project,
        team=team,
        run_type='session_distillation',
        status='succeeded',
    )
    other_team = Team.objects.create(organization=organization, name='Run team', slug='run-team')
    other_project = Project.objects.create(organization=organization, name='Run project', slug='run-project')
    foreign_organization, foreign_team, foreign_project, _foreign_agent, _foreign_session = create_second_scope()

    mismatches = (
        {'organization': organization, 'project': project, 'team': team, 'run_type': 'session_distillation'},
        {'organization': organization, 'project': project, 'team': other_team, 'run_type': 'observation_processing'},
        {'organization': organization, 'project': other_project, 'team': team, 'run_type': 'observation_processing'},
        {
            'organization': foreign_organization,
            'project': foreign_project,
            'team': foreign_team,
            'run_type': 'observation_processing',
        },
    )
    for mismatch in mismatches:
        with pytest.raises(ValidationError):
            WorkflowRun.objects.create(work=work, status='queued', **mismatch)

    work_field = WorkflowRun._meta.get_field('work')

    assert linked.work_id == work.id
    assert work.attempts.get() == linked
    assert legacy.work_id is None
    assert work_field.null is True
    assert work_field.blank is True
    assert work_field.default is models.NOT_PROVIDED
    assert work_field.remote_field.on_delete is models.PROTECT
    assert work_field.remote_field.related_name == 'attempts'
