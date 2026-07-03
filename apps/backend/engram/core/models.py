from __future__ import annotations

import uuid
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models

try:
    from pgvector.django import HnswIndex, VectorField
except ImportError:
    HnswIndex = None
    VectorField = None


class TimestampedModel(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True

    def save(self, *args: object, **kwargs: object) -> None:
        self.full_clean(validate_unique=False, validate_constraints=False)

        super().save(*args, **kwargs)


class Runtime(models.TextChoices):
    CLAUDE_CODE = 'claude_code', 'Claude Code'
    CODEX = 'codex', 'Codex'
    UNKNOWN = 'unknown', 'Unknown'


class SessionStatus(models.TextChoices):
    ACTIVE = 'active', 'Active'
    ENDED = 'ended', 'Ended'
    ERRORED = 'errored', 'Errored'


class VisibilityScope(models.TextChoices):
    SESSION = 'session', 'Session'
    PROJECT = 'project', 'Project'
    TEAM = 'team', 'Team'
    ORGANIZATION = 'organization', 'Organization'


class CandidateStatus(models.TextChoices):
    PROPOSED = 'proposed', 'Proposed'
    PROMOTED = 'promoted', 'Promoted'
    REJECTED = 'rejected', 'Rejected'


class MemoryStatus(models.TextChoices):
    APPROVED = 'approved', 'Approved'
    ARCHIVED = 'archived', 'Archived'
    REFUTED = 'refuted', 'Refuted'
    CONFLICT = 'conflict', 'Conflict'


MEMORY_KINDS = ('decision', 'convention', 'gotcha', 'architecture', 'incident', 'digest')


def clamp_memory_kind(value: object) -> str:
    if value in MEMORY_KINDS and value != 'digest':
        return value

    return ''


class ContextBundleStatus(models.TextChoices):
    CREATED = 'created', 'Created'
    INJECTED = 'injected', 'Injected'
    SKIPPED = 'skipped', 'Skipped'


class AuditResult(models.TextChoices):
    ALLOWED = 'allowed', 'Allowed'
    DENIED = 'denied', 'Denied'
    RECORDED = 'recorded', 'Recorded'
    ERROR = 'error', 'Error'


def add_scope_error(errors: dict[str, list[str]], field: str, message: str) -> None:
    errors.setdefault(field, []).append(message)


def check_organization_scope(
    errors: dict[str, list[str]],
    field: str,
    related: object,
    organization_id: uuid.UUID | None,
) -> None:
    if related is not None and related.organization_id != organization_id:
        add_scope_error(errors, field, f'{field} organization must match record organization')


def check_project_scope(
    errors: dict[str, list[str]],
    field: str,
    related: object,
    organization_id: uuid.UUID | None,
    project_id: uuid.UUID | None,
) -> None:
    check_organization_scope(errors, field, related, organization_id)
    if related is not None and related.project_id != project_id:
        add_scope_error(errors, field, f'{field} project must match record project')


def check_project_organization(
    errors: dict[str, list[str]],
    field: str,
    project: object,
    organization_id: uuid.UUID | None,
) -> None:
    if project is not None and project.organization_id != organization_id:
        add_scope_error(errors, field, f'{field} organization must match record organization')


def raise_scope_errors(errors: dict[str, list[str]]) -> None:
    if errors:
        raise ValidationError(errors)


class OrganizationStatus(models.TextChoices):
    ACTIVE = 'active', 'Active'
    TRIALING = 'trialing', 'Trialing'
    PAST_DUE = 'past_due', 'Past due'
    SUSPENDED = 'suspended', 'Suspended'
    PENDING_DELETE = 'pending_delete', 'Pending delete'


class Organization(TimestampedModel):
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=120, unique=True)
    status = models.CharField(
        max_length=20,
        choices=OrganizationStatus.choices,
        default=OrganizationStatus.ACTIVE,
    )

    class Meta:
        ordering = ['slug']

    def __str__(self) -> str:
        return self.slug


class OrganizationSettings(TimestampedModel):
    organization = models.OneToOneField(Organization, on_delete=models.CASCADE, related_name='settings')
    hybrid_retrieval_enabled = models.BooleanField(default=True)
    require_provenance = models.BooleanField(default=False)
    distillation_auto_approve_threshold = models.DecimalField(
        max_digits=4,
        decimal_places=3,
        null=True,
        blank=True,
    )
    curator_enabled = models.BooleanField(default=True)
    curator_llm_judge_enabled = models.BooleanField(default=False)
    lexical_fusion_enabled = models.BooleanField(default=False)
    lexical_recall_enabled = models.BooleanField(default=False)
    near_dup_threshold = models.DecimalField(
        max_digits=4,
        decimal_places=3,
        default=Decimal('0.850'),
    )

    def __str__(self) -> str:
        return f'settings:{self.organization_id}'


class Team(TimestampedModel):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='teams')
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=120)
    archived_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['organization', 'slug'], name='core_team_unique_slug_per_org'),
        ]
        ordering = ['organization_id', 'slug']

    def __str__(self) -> str:
        return f'{self.organization.slug}/{self.slug}'


class Project(TimestampedModel):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='projects')
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=120)
    repository_url = models.TextField(blank=True)
    repository_root = models.TextField(blank=True)
    default_branch = models.CharField(max_length=255, blank=True)
    archived_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['organization', 'slug'], name='core_project_unique_slug_per_org'),
        ]
        ordering = ['organization_id', 'slug']

    def __str__(self) -> str:
        return f'{self.organization.slug}/{self.slug}'


class ProjectTeam(TimestampedModel):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='project_team_links')
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='team_links')
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name='project_links')

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['project', 'team'], name='core_project_team_unique_pair'),
        ]

    def clean(self) -> None:
        errors: dict[str, list[str]] = {}
        if self.project_id:
            check_project_organization(errors, 'project', self.project, self.organization_id)
        if self.team_id:
            check_organization_scope(errors, 'team', self.team, self.organization_id)
        raise_scope_errors(errors)

    def __str__(self) -> str:
        return f'{self.project_id}:{self.team_id}'


class Agent(TimestampedModel):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='agents')
    runtime = models.CharField(max_length=40, choices=Runtime.choices, default=Runtime.UNKNOWN)
    external_id = models.CharField(max_length=255)
    display_name = models.CharField(max_length=255, blank=True)
    version = models.CharField(max_length=80, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'runtime', 'external_id'],
                name='core_agent_unique_external_id_per_runtime',
            ),
        ]
        ordering = ['organization_id', 'runtime', 'external_id']

    def __str__(self) -> str:
        return f'{self.runtime}:{self.external_id}'


class AgentSession(TimestampedModel):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='sessions')
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='sessions')
    team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name='sessions', null=True, blank=True)
    agent = models.ForeignKey(Agent, on_delete=models.PROTECT, related_name='sessions')
    external_session_id = models.CharField(max_length=255)
    content_session_id = models.CharField(max_length=255, blank=True)
    memory_session_id = models.CharField(max_length=255, blank=True)
    model_id = models.CharField(max_length=120, blank=True, default='')
    runtime = models.CharField(max_length=40, choices=Runtime.choices, default=Runtime.UNKNOWN)
    platform_source = models.CharField(max_length=80, blank=True)
    repository_url = models.TextField(blank=True)
    repository_root = models.TextField(blank=True)
    branch = models.CharField(max_length=255, blank=True)
    cwd = models.TextField(blank=True)
    status = models.CharField(max_length=40, choices=SessionStatus.choices, default=SessionStatus.ACTIVE)
    prompt_counter = models.PositiveIntegerField(default=0)
    metadata = models.JSONField(default=dict, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'project', 'external_session_id'],
                name='core_session_unique_external_id_per_project',
            ),
            models.UniqueConstraint(
                fields=['organization', 'project', 'content_session_id'],
                condition=~models.Q(content_session_id=''),
                name='core_session_unique_content_id_per_project',
            ),
        ]
        indexes = [
            models.Index(fields=['organization', 'project', 'status']),
            models.Index(
                fields=['organization', 'project', 'updated_at'],
                name='core_session_updated_idx',
            ),
        ]
        ordering = ['organization_id', 'project_id', 'external_session_id']

    def clean(self) -> None:
        errors: dict[str, list[str]] = {}
        if self.project_id:
            check_project_organization(errors, 'project', self.project, self.organization_id)
        if self.team_id:
            check_organization_scope(errors, 'team', self.team, self.organization_id)
        if self.agent_id:
            check_organization_scope(errors, 'agent', self.agent, self.organization_id)
        raise_scope_errors(errors)

    def __str__(self) -> str:
        return f'{self.runtime}:{self.external_session_id}'


class RawEventEnvelope(TimestampedModel):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='raw_events')
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='raw_events')
    team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name='raw_events', null=True, blank=True)
    agent = models.ForeignKey(Agent, on_delete=models.PROTECT, related_name='raw_events')
    session = models.ForeignKey(AgentSession, on_delete=models.CASCADE, related_name='raw_events')
    event_type = models.CharField(max_length=120)
    source_adapter = models.CharField(max_length=80, blank=True)
    client_event_id = models.CharField(max_length=255)
    idempotency_key = models.CharField(max_length=255)
    content_hash = models.CharField(max_length=128)
    runtime = models.CharField(max_length=40, choices=Runtime.choices, default=Runtime.UNKNOWN)
    payload_schema_version = models.CharField(max_length=40, default='v1')
    sequence_number = models.BigIntegerField(null=True, blank=True)
    occurred_at = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)
    payload = models.JSONField(default=dict)
    headers = models.JSONField(default=dict, blank=True)
    request_id = models.CharField(max_length=255, blank=True)
    correlation_id = models.CharField(max_length=255, blank=True)
    trace_id = models.CharField(max_length=255, blank=True)
    actor_type = models.CharField(max_length=80, blank=True)
    actor_id = models.CharField(max_length=255, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'project', 'session', 'client_event_id'],
                name='core_raw_event_unique_client_event_per_session',
            ),
            models.UniqueConstraint(
                fields=['organization', 'project', 'idempotency_key'],
                name='core_raw_event_unique_idempotency_key_per_project',
            ),
        ]
        indexes = [
            models.Index(fields=['organization', 'project', 'event_type']),
            models.Index(fields=['organization', 'project', 'content_hash']),
        ]
        ordering = ['organization_id', 'project_id', 'received_at']

    def clean(self) -> None:
        errors: dict[str, list[str]] = {}
        if self.project_id:
            check_project_organization(errors, 'project', self.project, self.organization_id)
        if self.team_id:
            check_organization_scope(errors, 'team', self.team, self.organization_id)
        if self.agent_id:
            check_organization_scope(errors, 'agent', self.agent, self.organization_id)
        if self.session_id:
            check_project_scope(errors, 'session', self.session, self.organization_id, self.project_id)
        raise_scope_errors(errors)

    def __str__(self) -> str:
        return f'{self.event_type}:{self.client_event_id}'


class Observation(TimestampedModel):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='observations')
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='observations')
    team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name='observations', null=True, blank=True)
    agent = models.ForeignKey(Agent, on_delete=models.PROTECT, related_name='observations')
    session = models.ForeignKey(AgentSession, on_delete=models.CASCADE, related_name='observations')
    raw_event = models.ForeignKey(
        RawEventEnvelope,
        on_delete=models.SET_NULL,
        related_name='observations',
        null=True,
        blank=True,
    )
    observation_type = models.CharField(max_length=80)
    title = models.CharField(max_length=255)
    subtitle = models.CharField(max_length=255, blank=True)
    body = models.TextField(blank=True)
    facts = models.JSONField(default=list, blank=True)
    narrative = models.TextField(blank=True)
    concepts = models.JSONField(default=list, blank=True)
    files_read = models.JSONField(default=list, blank=True)
    files_modified = models.JSONField(default=list, blank=True)
    prompt_number = models.PositiveIntegerField(null=True, blank=True)
    content_hash = models.CharField(max_length=128)
    generation_key = models.CharField(max_length=255, blank=True)
    generated_model = models.CharField(max_length=120, blank=True)
    redaction_metadata = models.JSONField(default=dict, blank=True)
    source_metadata = models.JSONField(default=dict, blank=True)
    observed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'project', 'session', 'content_hash'],
                name='core_observation_unique_content_hash_per_session',
            ),
        ]
        indexes = [
            models.Index(fields=['organization', 'project', 'observation_type']),
            models.Index(fields=['organization', 'project', 'content_hash']),
            models.Index(
                fields=['organization', 'project', 'observed_at', 'created_at'],
                name='core_observation_created_idx',
            ),
        ]
        ordering = ['organization_id', 'project_id', 'created_at']

    def clean(self) -> None:
        errors: dict[str, list[str]] = {}
        if self.project_id:
            check_project_organization(errors, 'project', self.project, self.organization_id)
        if self.team_id:
            check_organization_scope(errors, 'team', self.team, self.organization_id)
        if self.agent_id:
            check_organization_scope(errors, 'agent', self.agent, self.organization_id)
        if self.session_id:
            check_project_scope(errors, 'session', self.session, self.organization_id, self.project_id)
        if self.raw_event_id:
            check_project_scope(errors, 'raw_event', self.raw_event, self.organization_id, self.project_id)
        raise_scope_errors(errors)

    def __str__(self) -> str:
        return self.title


class ObservationSource(TimestampedModel):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='observation_sources')
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='observation_sources')
    observation = models.ForeignKey(Observation, on_delete=models.CASCADE, related_name='sources')
    raw_event = models.ForeignKey(
        RawEventEnvelope,
        on_delete=models.SET_NULL,
        related_name='observation_sources',
        null=True,
        blank=True,
    )
    source_type = models.CharField(max_length=80)
    source_id = models.CharField(max_length=255)
    citation = models.CharField(max_length=80, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['observation', 'source_type', 'source_id'],
                name='core_observation_source_unique_source',
            ),
        ]
        indexes = [
            models.Index(fields=['organization', 'project', 'source_type']),
        ]
        ordering = ['organization_id', 'project_id', 'source_type', 'source_id']

    def clean(self) -> None:
        errors: dict[str, list[str]] = {}
        if self.project_id:
            check_project_organization(errors, 'project', self.project, self.organization_id)
        if self.observation_id:
            check_project_scope(errors, 'observation', self.observation, self.organization_id, self.project_id)
        if self.raw_event_id:
            check_project_scope(errors, 'raw_event', self.raw_event, self.organization_id, self.project_id)
        raise_scope_errors(errors)

    def __str__(self) -> str:
        return f'{self.source_type}:{self.source_id}'


class MemoryCandidate(TimestampedModel):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='memory_candidates')
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='memory_candidates')
    team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name='memory_candidates', null=True, blank=True)
    source_observation = models.ForeignKey(
        Observation,
        on_delete=models.SET_NULL,
        related_name='memory_candidates',
        null=True,
        blank=True,
    )
    promoted_memory = models.ForeignKey(
        'Memory',
        on_delete=models.SET_NULL,
        related_name='source_candidates',
        null=True,
        blank=True,
    )
    title = models.CharField(max_length=255)
    body = models.TextField()
    status = models.CharField(max_length=40, choices=CandidateStatus.choices, default=CandidateStatus.PROPOSED)
    visibility_scope = models.CharField(
        max_length=40,
        choices=VisibilityScope.choices,
        default=VisibilityScope.PROJECT,
    )
    evidence = models.JSONField(default=list, blank=True)
    content_hash = models.CharField(max_length=128)
    confidence = models.DecimalField(max_digits=4, decimal_places=3, null=True, blank=True)
    kind = models.CharField(max_length=40, blank=True, default='')

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'project', 'content_hash'],
                name='core_memory_candidate_unique_content_hash_per_project',
            ),
        ]
        indexes = [
            models.Index(fields=['organization', 'project', 'status']),
        ]
        ordering = ['organization_id', 'project_id', 'created_at']

    def clean(self) -> None:
        errors: dict[str, list[str]] = {}
        if self.project_id:
            check_project_organization(errors, 'project', self.project, self.organization_id)
        if self.team_id:
            check_organization_scope(errors, 'team', self.team, self.organization_id)
        if self.source_observation_id:
            check_project_scope(
                errors,
                'source_observation',
                self.source_observation,
                self.organization_id,
                self.project_id,
            )
        if self.promoted_memory_id:
            check_project_scope(errors, 'promoted_memory', self.promoted_memory, self.organization_id, self.project_id)
        raise_scope_errors(errors)

    def __str__(self) -> str:
        return self.title


class Memory(TimestampedModel):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='memories')
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='memories')
    team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name='memories', null=True, blank=True)
    title = models.CharField(max_length=255)
    body = models.TextField()
    status = models.CharField(max_length=40, choices=MemoryStatus.choices, default=MemoryStatus.APPROVED)
    visibility_scope = models.CharField(
        max_length=40,
        choices=VisibilityScope.choices,
        default=VisibilityScope.PROJECT,
    )
    current_version = models.PositiveIntegerField(default=1)
    confidence = models.DecimalField(max_digits=4, decimal_places=3, null=True, blank=True)
    stale = models.BooleanField(default=False)
    refuted = models.BooleanField(default=False)
    metadata = models.JSONField(default=dict, blank=True)
    kind = models.CharField(max_length=40, blank=True, default='')

    class Meta:
        indexes = [
            models.Index(fields=['organization', 'project', 'status']),
            models.Index(fields=['organization', 'project', 'visibility_scope']),
            models.Index(
                fields=['organization', 'project', 'status', 'updated_at'],
                name='core_memory_status_updated_idx',
            ),
            models.Index(
                fields=['organization', 'project', 'created_at'],
                name='core_memory_created_idx',
            ),
            models.Index(
                fields=['organization', 'project', 'kind'],
                name='core_memory_kind_idx',
            ),
        ]
        ordering = ['organization_id', 'project_id', 'title']

    def clean(self) -> None:
        errors: dict[str, list[str]] = {}
        if self.project_id:
            check_project_organization(errors, 'project', self.project, self.organization_id)
        if self.team_id:
            check_organization_scope(errors, 'team', self.team, self.organization_id)
        raise_scope_errors(errors)

    def save(self, *args: object, **kwargs: object) -> None:
        self.kind = self.metadata.get('kind', '') if isinstance(self.metadata, dict) else ''

        update_fields = kwargs.get('update_fields')
        if update_fields is not None and 'metadata' in update_fields and 'kind' not in update_fields:
            kwargs['update_fields'] = (*update_fields, 'kind')

        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return self.title


class MemoryVersion(TimestampedModel):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='memory_versions')
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='memory_versions')
    memory = models.ForeignKey(Memory, on_delete=models.CASCADE, related_name='versions')
    source_observation = models.ForeignKey(
        Observation,
        on_delete=models.SET_NULL,
        related_name='memory_versions',
        null=True,
        blank=True,
    )
    version = models.PositiveIntegerField()
    body = models.TextField()
    content_hash = models.CharField(max_length=128)
    source_metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['memory', 'version'], name='core_memory_version_unique_version'),
        ]
        indexes = [
            models.Index(fields=['organization', 'project', 'content_hash']),
        ]
        ordering = ['memory_id', 'version']

    def clean(self) -> None:
        errors: dict[str, list[str]] = {}
        if self.project_id:
            check_project_organization(errors, 'project', self.project, self.organization_id)
        if self.memory_id:
            check_project_scope(errors, 'memory', self.memory, self.organization_id, self.project_id)
        if self.source_observation_id:
            check_project_scope(
                errors,
                'source_observation',
                self.source_observation,
                self.organization_id,
                self.project_id,
            )
        raise_scope_errors(errors)

    def __str__(self) -> str:
        return f'{self.memory_id}:v{self.version}'


class RetrievalDocument(TimestampedModel):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='retrieval_documents')
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='retrieval_documents')
    team = models.ForeignKey(
        Team,
        on_delete=models.PROTECT,
        related_name='retrieval_documents',
        null=True,
        blank=True,
    )
    memory = models.ForeignKey(Memory, on_delete=models.CASCADE, related_name='retrieval_documents')
    memory_version = models.OneToOneField(
        MemoryVersion,
        on_delete=models.CASCADE,
        related_name='retrieval_document',
    )
    visibility_scope = models.CharField(
        max_length=40,
        choices=VisibilityScope.choices,
        default=VisibilityScope.PROJECT,
    )
    source_observation_ids = models.JSONField(default=list, blank=True)
    file_paths = models.JSONField(default=list, blank=True)
    symbols = models.JSONField(default=list, blank=True)
    exact_terms = models.JSONField(default=list, blank=True)
    full_text = models.TextField()
    embedding_reference = models.CharField(max_length=255, blank=True)
    embedding_vector = models.JSONField(default=list, blank=True)
    embedding_pgvector = VectorField(dimensions=1536, null=True, blank=True) if VectorField is not None else None
    stale = models.BooleanField(default=False)
    refuted = models.BooleanField(default=False)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['organization', 'project', 'visibility_scope']),
            models.Index(fields=['organization', 'project', 'stale', 'refuted']),
            *(
                [
                    HnswIndex(
                        name='core_retdoc_emb_hnsw',
                        fields=['embedding_pgvector'],
                        opclasses=['vector_cosine_ops'],
                        m=16,
                        ef_construction=64,
                    ),
                ]
                if VectorField is not None
                else []
            ),
        ]
        ordering = ['organization_id', 'project_id', 'memory_id']

    def clean_fields(self, exclude: object = None) -> None:
        excluded = set(exclude or ())
        if VectorField is not None:
            excluded.add('embedding_pgvector')

        super().clean_fields(exclude=excluded)

    def clean(self) -> None:
        errors: dict[str, list[str]] = {}
        if self.project_id:
            check_project_organization(errors, 'project', self.project, self.organization_id)
        if self.team_id:
            check_organization_scope(errors, 'team', self.team, self.organization_id)
        if self.memory_id:
            check_project_scope(errors, 'memory', self.memory, self.organization_id, self.project_id)
        if self.memory_version_id:
            check_project_scope(errors, 'memory_version', self.memory_version, self.organization_id, self.project_id)
        if self.memory_version_id and self.memory_id and self.memory_version.memory_id != self.memory_id:
            add_scope_error(errors, 'memory_version', 'memory version must belong to retrieval document memory')
        raise_scope_errors(errors)

    def __str__(self) -> str:
        return f'{self.memory_id}:{self.memory_version_id}'


class ContextBundle(TimestampedModel):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='context_bundles')
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='context_bundles')
    team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name='context_bundles', null=True, blank=True)
    agent = models.ForeignKey(Agent, on_delete=models.PROTECT, related_name='context_bundles')
    session = models.ForeignKey(AgentSession, on_delete=models.CASCADE, related_name='context_bundles')
    request_id = models.CharField(max_length=255)
    purpose = models.CharField(max_length=80)
    query_text = models.TextField(blank=True)
    rendered_text = models.TextField(blank=True)
    authorization_scope = models.JSONField(default=dict, blank=True)
    token_budget = models.PositiveIntegerField(null=True, blank=True)
    selected_count = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=40, choices=ContextBundleStatus.choices, default=ContextBundleStatus.CREATED)
    metadata = models.JSONField(default=dict, blank=True)
    retrieval_latency_ms = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'project', 'request_id'],
                name='core_context_bundle_unique_request_per_project',
            ),
        ]
        indexes = [
            models.Index(fields=['organization', 'project', 'purpose']),
            models.Index(
                fields=['organization', 'project', 'created_at'],
                name='core_ctxbundle_created_idx',
            ),
        ]
        ordering = ['organization_id', 'project_id', 'created_at']

    def clean(self) -> None:
        errors: dict[str, list[str]] = {}
        if self.project_id:
            check_project_organization(errors, 'project', self.project, self.organization_id)
        if self.team_id:
            check_organization_scope(errors, 'team', self.team, self.organization_id)
        if self.agent_id:
            check_organization_scope(errors, 'agent', self.agent, self.organization_id)
        if self.session_id:
            check_project_scope(errors, 'session', self.session, self.organization_id, self.project_id)
        raise_scope_errors(errors)

    def __str__(self) -> str:
        return self.request_id


class ContextBundleItem(TimestampedModel):
    bundle = models.ForeignKey(ContextBundle, on_delete=models.CASCADE, related_name='items')
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='context_bundle_items')
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='context_bundle_items')
    memory = models.ForeignKey(Memory, on_delete=models.PROTECT, related_name='context_bundle_items')
    retrieval_document = models.ForeignKey(
        RetrievalDocument,
        on_delete=models.PROTECT,
        related_name='context_bundle_items',
    )
    rank = models.PositiveIntegerField()
    citation = models.CharField(max_length=80)
    inclusion_reason = models.TextField(blank=True)
    scope_evidence = models.JSONField(default=dict, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['bundle', 'rank'], name='core_context_bundle_item_unique_rank'),
            models.UniqueConstraint(fields=['bundle', 'memory'], name='core_context_bundle_item_unique_memory'),
        ]
        ordering = ['bundle_id', 'rank']

    def clean(self) -> None:
        errors: dict[str, list[str]] = {}
        if self.project_id:
            check_project_organization(errors, 'project', self.project, self.organization_id)
        if self.bundle_id:
            check_project_scope(errors, 'bundle', self.bundle, self.organization_id, self.project_id)
        if self.memory_id:
            check_project_scope(errors, 'memory', self.memory, self.organization_id, self.project_id)
        if self.retrieval_document_id:
            check_project_scope(
                errors,
                'retrieval_document',
                self.retrieval_document,
                self.organization_id,
                self.project_id,
            )
        raise_scope_errors(errors)

    def __str__(self) -> str:
        return f'{self.bundle_id}:{self.citation}'


class AuditEvent(TimestampedModel):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='audit_events')
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='audit_events', null=True, blank=True)
    team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name='audit_events', null=True, blank=True)
    event_type = models.CharField(max_length=120)
    actor_type = models.CharField(max_length=80)
    actor_id = models.CharField(max_length=255, blank=True)
    target_type = models.CharField(max_length=80, blank=True)
    target_id = models.CharField(max_length=255, blank=True)
    capability = models.CharField(max_length=120, blank=True)
    result = models.CharField(max_length=40, choices=AuditResult.choices, default=AuditResult.RECORDED)
    request_id = models.CharField(max_length=255, blank=True)
    correlation_id = models.CharField(max_length=255, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['organization', 'project', 'event_type']),
            models.Index(fields=['organization', 'result']),
            models.Index(
                fields=['organization', 'created_at'],
                name='core_audit_org_created_idx',
            ),
            models.Index(
                fields=['organization', 'project', 'created_at'],
                name='core_audit_proj_created_idx',
            ),
        ]
        ordering = ['organization_id', 'created_at']

    def clean(self) -> None:
        errors: dict[str, list[str]] = {}
        if self.project_id:
            check_project_organization(errors, 'project', self.project, self.organization_id)
        if self.team_id:
            check_organization_scope(errors, 'team', self.team, self.organization_id)
        raise_scope_errors(errors)

    def __str__(self) -> str:
        return f'{self.event_type}:{self.result}'


class LinkType(models.TextChoices):
    FILE = 'file', 'File'
    SYMBOL = 'symbol', 'Symbol'
    COMMIT = 'commit', 'Commit'
    ISSUE = 'issue', 'Issue'
    NARROWED_BY = 'narrowed_by', 'Narrowed by'
    SUPERSEDED_BY = 'superseded_by', 'Superseded by'
    CONFLICTS_WITH = 'conflicts_with', 'Conflicts With'


class MemoryLink(TimestampedModel):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='memory_links')
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='memory_links')
    memory = models.ForeignKey(Memory, on_delete=models.CASCADE, related_name='links')
    link_type = models.CharField(max_length=40, choices=LinkType.choices)
    target = models.CharField(max_length=1024)
    label = models.CharField(max_length=255, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['memory', 'link_type', 'target'],
                name='core_memory_link_unique_target',
            ),
        ]
        indexes = [
            models.Index(fields=['organization', 'project', 'link_type']),
        ]
        ordering = ['organization_id', 'project_id', 'memory_id', 'link_type', 'target']

    def clean(self) -> None:
        errors: dict[str, list[str]] = {}
        if self.project_id:
            check_project_organization(errors, 'project', self.project, self.organization_id)
        if self.memory_id:
            check_project_scope(errors, 'memory', self.memory, self.organization_id, self.project_id)
        raise_scope_errors(errors)

    def __str__(self) -> str:
        return f'{self.link_type}:{self.target}'


class WorkflowRunType(models.TextChoices):
    DAILY_DIGEST = 'daily_digest', 'Daily Digest'
    OBSERVATION_PROCESSING = 'observation_processing', 'Observation Processing'
    SESSION_DISTILLATION = 'session_distillation', 'Session Distillation'
    WEEKLY_DIGEST = 'weekly_digest', 'Weekly Digest'


class WorkflowRunStatus(models.TextChoices):
    QUEUED = 'queued', 'Queued'
    RUNNING = 'running', 'Running'
    SUCCEEDED = 'succeeded', 'Succeeded'
    FAILED = 'failed', 'Failed'


class WorkflowRun(TimestampedModel):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='workflow_runs')
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='workflow_runs')
    team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name='workflow_runs', null=True, blank=True)
    run_type = models.CharField(max_length=40, choices=WorkflowRunType.choices)
    status = models.CharField(
        max_length=40,
        choices=WorkflowRunStatus.choices,
        default=WorkflowRunStatus.QUEUED,
    )
    input_snapshot = models.JSONField(default=dict, blank=True)
    provider_call_ids = models.JSONField(default=list, blank=True)
    result_memory = models.ForeignKey(
        Memory,
        on_delete=models.SET_NULL,
        related_name='workflow_runs',
        null=True,
        blank=True,
    )
    escalation = models.BooleanField(default=False)
    failure_reason = models.CharField(max_length=1024, blank=True)
    request_id = models.CharField(max_length=255, blank=True)
    correlation_id = models.CharField(max_length=255, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    rerun_of = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        related_name='reruns',
        null=True,
        blank=True,
    )

    class Meta:
        indexes = [
            models.Index(fields=['organization', 'status']),
            models.Index(fields=['organization', 'created_at']),
        ]
        ordering = ['organization_id', '-created_at']

    def clean(self) -> None:
        errors: dict[str, list[str]] = {}
        if self.project_id:
            check_project_organization(errors, 'project', self.project, self.organization_id)
        if self.team_id:
            check_organization_scope(errors, 'team', self.team, self.organization_id)
        if self.result_memory_id:
            check_project_scope(
                errors,
                'result_memory',
                self.result_memory,
                self.organization_id,
                self.project_id,
            )
        raise_scope_errors(errors)

    def __str__(self) -> str:
        return f'{self.run_type}:{self.status}:{self.id}'
