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


class RawEventNormalizationDisposition(models.TextChoices):
    OBSERVATION = 'observation', 'Observation'
    NO_OP = 'no_op', 'No-op'


class RawEventNormalizationReason(models.TextChoices):
    EVIDENCE_ONLY = 'evidence_only', 'Evidence only'


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


def check_window_scope(
    errors: dict[str, list[str]],
    window: object,
    organization_id: uuid.UUID | None,
    project_id: uuid.UUID | None,
    team_id: uuid.UUID | None,
) -> None:
    if window is None:
        return

    if window.organization_id != organization_id:
        add_scope_error(errors, 'window', 'window organization must match record organization')
    if window.project_id != project_id:
        add_scope_error(errors, 'window', 'window project must match record project')
    if window.team_id != team_id:
        add_scope_error(errors, 'window', 'window team must match record team')

    return


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
    realtime_candidates_enabled = models.BooleanField(default=False)
    confidence_decay_enabled = models.BooleanField(default=True)
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
    observation_sequence_cursor = models.PositiveBigIntegerField(db_default=0)
    end_work_contract_version = models.PositiveSmallIntegerField(
        default=0,
        db_default=0,
    )
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
            models.CheckConstraint(
                condition=models.Q(end_work_contract_version__in=(0, 1)),
                name='core_session_end_contract_ck',
            ),
        ]
        indexes = [
            models.Index(fields=['organization', 'project', 'status']),
            models.Index(
                fields=['organization', 'project', 'updated_at'],
                name='core_session_updated_idx',
            ),
            models.Index(
                fields=['organization', 'project', 'status', 'end_work_contract_version'],
                name='core_session_end_work_idx',
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
    normalization_contract_version = models.PositiveSmallIntegerField()
    normalization_disposition = models.CharField(
        max_length=20,
        choices=RawEventNormalizationDisposition.choices,
        null=True,
        blank=True,
    )
    normalization_reason = models.CharField(
        max_length=40,
        choices=RawEventNormalizationReason.choices,
        null=True,
        blank=True,
    )
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
            models.CheckConstraint(
                condition=(
                    models.Q(
                        normalization_contract_version=0,
                        normalization_disposition__isnull=True,
                        normalization_reason__isnull=True,
                    )
                    | models.Q(
                        normalization_contract_version=1,
                        normalization_disposition__isnull=False,
                        normalization_disposition='observation',
                        normalization_reason__isnull=True,
                    )
                    | models.Q(
                        normalization_contract_version=1,
                        normalization_disposition__isnull=False,
                        normalization_disposition='no_op',
                        normalization_reason__isnull=False,
                        normalization_reason='evidence_only',
                    )
                ),
                name='core_raw_norm_final_valid',
            ),
        ]
        indexes = [
            models.Index(fields=['organization', 'project', 'event_type']),
            models.Index(fields=['organization', 'project', 'content_hash']),
            models.Index(
                fields=[
                    'organization',
                    'project',
                    'normalization_contract_version',
                    'normalization_disposition',
                ],
                name='core_raw_norm_disposition_idx',
            ),
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
    session_sequence = models.PositiveBigIntegerField()
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
            models.UniqueConstraint(
                fields=['session', 'session_sequence'],
                condition=models.Q(session_sequence__isnull=False),
                name='core_obs_session_seq_uniq',
            ),
            models.CheckConstraint(
                condition=models.Q(session_sequence__gt=0),
                name='core_obs_session_seq_pos',
            ),
        ]
        indexes = [
            models.Index(fields=['organization', 'project', 'observation_type']),
            models.Index(fields=['organization', 'project', 'content_hash']),
            models.Index(
                fields=['organization', 'project', 'observed_at', 'created_at'],
                name='core_observation_created_idx',
            ),
            models.Index(
                fields=['organization', 'project', 'session', 'session_sequence'],
                name='core_obs_scope_session_seq_idx',
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
    decision_work_contract_version = models.PositiveSmallIntegerField(default=0, db_default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'project', 'content_hash'],
                name='core_memory_candidate_unique_content_hash_per_project',
            ),
            models.CheckConstraint(
                condition=models.Q(decision_work_contract_version__in=(0, 1)),
                name='core_memory_candidate_decision_ver_ck',
            ),
        ]
        indexes = [
            models.Index(fields=['organization', 'project', 'status']),
            models.Index(
                fields=['organization', 'project', 'status', 'decision_work_contract_version'],
                name='core_cand_decision_ver_idx',
            ),
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


def retrieval_embedding_deferred_fields() -> tuple[str, ...]:
    if VectorField is None:
        return ('embedding_vector',)

    return ('embedding_vector', 'embedding_pgvector')


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


class MemoryReviewExample(TimestampedModel):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='memory_review_examples')
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='memory_review_examples')
    team = models.ForeignKey(
        Team,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='memory_review_examples',
    )
    item_type = models.CharField(max_length=40)
    item_id = models.CharField(max_length=64)
    action = models.CharField(max_length=40)
    snapshot = models.JSONField(default=dict)
    curator_context = models.JSONField(default=dict, blank=True)
    reason = models.TextField(blank=True)
    actor_id = models.CharField(max_length=255, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['organization', 'project', 'created_at']),
            models.Index(fields=['organization', 'action']),
        ]
        ordering = ['organization_id', 'project_id', '-created_at']

    def clean(self) -> None:
        errors: dict[str, list[str]] = {}
        if self.project_id:
            check_project_organization(errors, 'project', self.project, self.organization_id)
        if self.team_id:
            check_organization_scope(errors, 'team', self.team, self.organization_id)
        raise_scope_errors(errors)

    def __str__(self) -> str:
        return f'{self.item_type}:{self.item_id}:{self.action}'


class WorkflowWorkType(models.TextChoices):
    OBSERVATION_PROCESSING = 'observation_processing', 'Observation Processing'
    SESSION_DISTILLATION = 'session_distillation', 'Session Distillation'
    DAILY_DIGEST = 'daily_digest', 'Daily Digest'
    WEEKLY_DIGEST = 'weekly_digest', 'Weekly Digest'
    CANDIDATE_DECISION = 'candidate_decision', 'Candidate Decision'


class WorkflowSubjectType(models.TextChoices):
    OBSERVATION = 'observation', 'Observation'
    AGENT_SESSION = 'agent_session', 'Agent Session'
    PROJECT = 'project', 'Project'
    TEAM = 'team', 'Team'
    MEMORY_CANDIDATE = 'memory_candidate', 'Memory Candidate'


class WorkflowWorkDisposition(models.TextChoices):
    REQUIRED = 'required', 'Required'
    COMPLETE = 'complete', 'Complete'
    NO_OP = 'no_op', 'No-op'


class WorkflowWorkResolutionReason(models.TextChoices):
    SUCCEEDED = 'succeeded', 'Succeeded'
    NO_SIGNAL = 'no_signal', 'No signal'
    NO_INPUT = 'no_input', 'No input'


class WorkflowWorkExecutionState(models.TextChoices):
    READY = 'ready', 'Ready'
    LEASED = 'leased', 'Leased'
    RETRY_WAIT = 'retry_wait', 'Retry wait'
    BLOCKED = 'blocked', 'Blocked'
    TERMINAL_FAILURE = 'terminal_failure', 'Terminal failure'
    SETTLED = 'settled', 'Settled'


class WorkflowWork(TimestampedModel):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='workflow_works')
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='workflow_works')
    team = models.ForeignKey(
        Team,
        on_delete=models.PROTECT,
        related_name='workflow_works',
        null=True,
        blank=True,
    )
    work_type = models.CharField(max_length=40, choices=WorkflowWorkType.choices)
    subject_type = models.CharField(max_length=40, choices=WorkflowSubjectType.choices)
    subject_id = models.UUIDField()
    contract_version = models.PositiveSmallIntegerField(default=1)
    occurrence_key = models.CharField(max_length=255, blank=True)
    input_fingerprint = models.CharField(max_length=64)
    input_snapshot = models.JSONField()
    disposition = models.CharField(
        max_length=20,
        choices=WorkflowWorkDisposition.choices,
        default=WorkflowWorkDisposition.REQUIRED,
    )
    resolution_reason = models.CharField(
        max_length=40,
        choices=WorkflowWorkResolutionReason.choices,
        blank=True,
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    execution_state = models.CharField(
        max_length=24,
        choices=WorkflowWorkExecutionState.choices,
        default=WorkflowWorkExecutionState.READY,
        db_default=WorkflowWorkExecutionState.READY,
    )
    fencing_token = models.PositiveBigIntegerField(default=0, db_default=0)
    lease_owner = models.CharField(max_length=255, blank=True, db_default='')
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    next_retry_at = models.DateTimeField(null=True, blank=True)
    failure_streak = models.PositiveIntegerField(default=0, db_default=0)
    blocked_configuration_fingerprint = models.CharField(max_length=64, blank=True, db_default='')

    _IMMUTABLE_FIELDS = (
        ('organization_id', 'organization'),
        ('project_id', 'project'),
        ('team_id', 'team'),
        ('work_type', 'work_type'),
        ('subject_type', 'subject_type'),
        ('subject_id', 'subject_id'),
        ('contract_version', 'contract_version'),
        ('occurrence_key', 'occurrence_key'),
        ('input_fingerprint', 'input_fingerprint'),
        ('input_snapshot', 'input_snapshot'),
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=[
                    'organization',
                    'project',
                    'work_type',
                    'subject_type',
                    'subject_id',
                    'contract_version',
                    'input_fingerprint',
                ],
                name='core_work_identity_uniq',
            ),
            models.UniqueConstraint(
                fields=[
                    'organization',
                    'project',
                    'work_type',
                    'subject_type',
                    'subject_id',
                    'contract_version',
                    'occurrence_key',
                ],
                condition=~models.Q(occurrence_key=''),
                name='core_work_occurrence_uniq',
            ),
            models.CheckConstraint(
                condition=models.Q(contract_version__gt=0),
                name='core_work_contract_ver_pos',
            ),
            models.CheckConstraint(
                condition=models.Q(input_fingerprint__regex=r'^[0-9a-f]{64}$'),
                name='core_work_fingerprint_hex',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        work_type=WorkflowWorkType.OBSERVATION_PROCESSING,
                        subject_type=WorkflowSubjectType.OBSERVATION,
                        occurrence_key='',
                    )
                    | models.Q(
                        work_type=WorkflowWorkType.SESSION_DISTILLATION,
                        subject_type=WorkflowSubjectType.AGENT_SESSION,
                        occurrence_key='',
                    )
                    | (
                        models.Q(
                            work_type=WorkflowWorkType.DAILY_DIGEST,
                            subject_type=WorkflowSubjectType.PROJECT,
                            subject_id=models.F('project_id'),
                            team__isnull=True,
                        )
                        & ~models.Q(occurrence_key='')
                    )
                    | (
                        models.Q(
                            work_type=WorkflowWorkType.WEEKLY_DIGEST,
                            subject_type=WorkflowSubjectType.PROJECT,
                            subject_id=models.F('project_id'),
                            team__isnull=True,
                        )
                        & ~models.Q(occurrence_key='')
                    )
                    | (
                        models.Q(
                            work_type=WorkflowWorkType.WEEKLY_DIGEST,
                            subject_type=WorkflowSubjectType.TEAM,
                            subject_id=models.F('team_id'),
                            team__isnull=False,
                        )
                        & ~models.Q(occurrence_key='')
                    )
                    | models.Q(
                        work_type=WorkflowWorkType.CANDIDATE_DECISION,
                        subject_type=WorkflowSubjectType.MEMORY_CANDIDATE,
                        occurrence_key='',
                    )
                ),
                name='core_work_subject_scope_ck',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        disposition=WorkflowWorkDisposition.REQUIRED,
                        resolution_reason='',
                        resolved_at__isnull=True,
                    )
                    | models.Q(
                        disposition=WorkflowWorkDisposition.COMPLETE,
                        resolution_reason__in=(
                            WorkflowWorkResolutionReason.SUCCEEDED,
                            WorkflowWorkResolutionReason.NO_SIGNAL,
                        ),
                        resolved_at__isnull=False,
                    )
                    | models.Q(
                        disposition=WorkflowWorkDisposition.NO_OP,
                        resolution_reason=WorkflowWorkResolutionReason.NO_INPUT,
                        resolved_at__isnull=False,
                    )
                ),
                name='core_work_terminal_state_ck',
            ),
            models.CheckConstraint(
                condition=(
                    (
                        models.Q(execution_state=WorkflowWorkExecutionState.LEASED)
                        & ~models.Q(lease_owner='')
                        & models.Q(heartbeat_at__isnull=False)
                        & models.Q(lease_expires_at__isnull=False)
                        & models.Q(lease_expires_at__gt=models.F('heartbeat_at'))
                        & models.Q(next_retry_at__isnull=True)
                        & models.Q(blocked_configuration_fingerprint='')
                    )
                    | (
                        models.Q(execution_state=WorkflowWorkExecutionState.RETRY_WAIT)
                        & models.Q(lease_owner='')
                        & models.Q(heartbeat_at__isnull=True)
                        & models.Q(lease_expires_at__isnull=True)
                        & models.Q(next_retry_at__isnull=False)
                        & models.Q(blocked_configuration_fingerprint='')
                    )
                    | (
                        models.Q(execution_state=WorkflowWorkExecutionState.BLOCKED)
                        & models.Q(lease_owner='')
                        & models.Q(heartbeat_at__isnull=True)
                        & models.Q(lease_expires_at__isnull=True)
                        & models.Q(next_retry_at__isnull=True)
                        & models.Q(blocked_configuration_fingerprint__regex=r'^[0-9a-f]{64}$')
                    )
                    | (
                        models.Q(
                            execution_state__in=(
                                WorkflowWorkExecutionState.READY,
                                WorkflowWorkExecutionState.TERMINAL_FAILURE,
                                WorkflowWorkExecutionState.SETTLED,
                            )
                        )
                        & models.Q(lease_owner='')
                        & models.Q(heartbeat_at__isnull=True)
                        & models.Q(lease_expires_at__isnull=True)
                        & models.Q(next_retry_at__isnull=True)
                        & models.Q(blocked_configuration_fingerprint='')
                    )
                ),
                name='core_work_execution_shape_ck',
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(execution_state=WorkflowWorkExecutionState.SETTLED)
                    | ~models.Q(disposition=WorkflowWorkDisposition.REQUIRED)
                ),
                name='core_work_settled_disposition_ck',
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(execution_state=WorkflowWorkExecutionState.TERMINAL_FAILURE)
                    | models.Q(disposition=WorkflowWorkDisposition.REQUIRED)
                ),
                name='core_work_terminal_disposition_ck',
            ),
            models.CheckConstraint(
                condition=models.Q(fencing_token__gte=0),
                name='core_work_fencing_token_nonneg',
            ),
            models.CheckConstraint(
                condition=models.Q(failure_streak__gte=0),
                name='core_work_failure_streak_nonneg',
            ),
        ]
        indexes = [
            models.Index(
                fields=['organization', 'project', 'disposition'],
                name='core_work_scope_disp_idx',
            ),
            models.Index(
                fields=['organization', 'project', 'work_type', 'disposition'],
                name='core_work_type_disp_idx',
            ),
            models.Index(
                fields=['organization', 'project', 'subject_type', 'subject_id'],
                name='core_work_subject_idx',
            ),
            models.Index(
                fields=['organization', 'project', 'work_type', 'occurrence_key'],
                name='core_work_occurrence_idx',
            ),
            models.Index(
                fields=['organization', 'project', 'execution_state', 'next_retry_at'],
                name='core_work_exec_retry_idx',
            ),
            models.Index(
                fields=['organization', 'project', 'work_type', 'execution_state'],
                name='core_work_type_exec_idx',
            ),
            models.Index(
                fields=['execution_state', 'lease_expires_at'],
                name='core_work_exec_lease_idx',
            ),
        ]

    def clean(self) -> None:
        errors: dict[str, list[str]] = {}
        if self.project_id:
            check_project_organization(errors, 'project', self.project, self.organization_id)
        if self.team_id:
            check_organization_scope(errors, 'team', self.team, self.organization_id)

        persisted = enforce_immutable_fields(self, self._IMMUTABLE_FIELDS, errors, extra_fields=('disposition',))
        if (
            persisted is not None
            and persisted['disposition'] != WorkflowWorkDisposition.REQUIRED
            and self.disposition == WorkflowWorkDisposition.REQUIRED
        ):
            add_scope_error(errors, 'disposition', 'terminal work cannot return to required')

        raise_scope_errors(errors)

    def __str__(self) -> str:
        return f'{self.work_type}:{self.subject_type}:{self.subject_id}'


class WorkflowRunType(models.TextChoices):
    DAILY_DIGEST = 'daily_digest', 'Daily Digest'
    OBSERVATION_PROCESSING = 'observation_processing', 'Observation Processing'
    SESSION_DISTILLATION = 'session_distillation', 'Session Distillation'
    WEEKLY_DIGEST = 'weekly_digest', 'Weekly Digest'
    CANDIDATE_DECISION = 'candidate_decision', 'Candidate Decision'


class WorkflowRunStatus(models.TextChoices):
    QUEUED = 'queued', 'Queued'
    RUNNING = 'running', 'Running'
    SUCCEEDED = 'succeeded', 'Succeeded'
    FAILED = 'failed', 'Failed'


class WorkflowRunOrigin(models.TextChoices):
    LEGACY = 'legacy', 'Legacy'
    AUTOMATIC = 'automatic', 'Automatic'
    RECONCILIATION = 'reconciliation', 'Reconciliation'
    MANUAL = 'manual', 'Manual'


class WorkflowRunFailureClass(models.TextChoices):
    WORKER_LOST = 'worker_lost', 'Worker lost'
    INFRASTRUCTURE_TRANSIENT = 'infrastructure_transient', 'Infrastructure transient'
    PROVIDER_TRANSIENT = 'provider_transient', 'Provider transient'
    CONFIGURATION = 'configuration', 'Configuration'
    INVALID_INPUT = 'invalid_input', 'Invalid input'
    UNEXPECTED = 'unexpected', 'Unexpected'


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
    work = models.ForeignKey(
        WorkflowWork,
        on_delete=models.PROTECT,
        related_name='attempts',
        null=True,
        blank=True,
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
    execution_contract_version = models.PositiveSmallIntegerField(default=0, db_default=0)
    origin = models.CharField(
        max_length=24,
        choices=WorkflowRunOrigin.choices,
        default=WorkflowRunOrigin.LEGACY,
        db_default=WorkflowRunOrigin.LEGACY,
    )
    fencing_token = models.PositiveBigIntegerField(null=True, blank=True)
    lease_owner = models.CharField(max_length=255, blank=True, db_default='')
    dispatched_at = models.DateTimeField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    failure_class = models.CharField(
        max_length=32,
        choices=WorkflowRunFailureClass.choices,
        blank=True,
        db_default='',
    )
    failure_code = models.CharField(max_length=128, blank=True, db_default='')
    configuration_fingerprint = models.CharField(max_length=64, blank=True, db_default='')

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['project', 'run_type'],
                condition=models.Q(
                    run_type='daily_digest',
                    status__in=('queued', 'running'),
                ),
                name='core_workflowrun_uniq_active_daily_digest',
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(execution_contract_version=1)
                    | (
                        models.Q(
                            status=WorkflowRunStatus.QUEUED,
                            fencing_token__isnull=True,
                            lease_owner='',
                            started_at__isnull=True,
                            finished_at__isnull=True,
                            heartbeat_at__isnull=True,
                            lease_expires_at__isnull=True,
                            failure_class='',
                            failure_code='',
                            dispatched_at__isnull=False,
                        )
                        | (
                            models.Q(
                                status=WorkflowRunStatus.RUNNING,
                                fencing_token__gt=0,
                                fencing_token__isnull=False,
                                started_at__isnull=False,
                                heartbeat_at__isnull=False,
                                lease_expires_at__isnull=False,
                                finished_at__isnull=True,
                                failure_class='',
                                failure_code='',
                            )
                            & ~models.Q(lease_owner='')
                        )
                        | (
                            models.Q(
                                status=WorkflowRunStatus.SUCCEEDED,
                                fencing_token__gt=0,
                                fencing_token__isnull=False,
                                started_at__isnull=False,
                                finished_at__isnull=False,
                                failure_class='',
                                failure_code='',
                            )
                            & ~models.Q(lease_owner='')
                        )
                        | (
                            models.Q(
                                status=WorkflowRunStatus.FAILED,
                                fencing_token__gt=0,
                                fencing_token__isnull=False,
                                started_at__isnull=False,
                                finished_at__isnull=False,
                            )
                            & ~models.Q(lease_owner='')
                            & ~models.Q(failure_class='')
                            & ~models.Q(failure_code='')
                        )
                    )
                ),
                name='core_run_v1_status_shape_ck',
            ),
            models.CheckConstraint(
                condition=(
                    (
                        models.Q(configuration_fingerprint='')
                        & ~models.Q(failure_class=WorkflowRunFailureClass.CONFIGURATION)
                    )
                    | models.Q(
                        failure_class=WorkflowRunFailureClass.CONFIGURATION,
                        configuration_fingerprint__regex=r'^[0-9a-f]{64}$',
                    )
                ),
                name='core_run_config_fingerprint_ck',
            ),
            models.UniqueConstraint(
                fields=['work', 'fencing_token'],
                condition=models.Q(
                    execution_contract_version=1,
                    fencing_token__isnull=False,
                    work__isnull=False,
                ),
                name='core_run_v1_work_token_uniq',
            ),
            models.UniqueConstraint(
                fields=['work'],
                condition=models.Q(
                    execution_contract_version=1,
                    status=WorkflowRunStatus.RUNNING,
                    work__isnull=False,
                ),
                name='core_run_v1_one_running_uniq',
            ),
        ]
        indexes = [
            models.Index(fields=['organization', 'status']),
            models.Index(fields=['organization', 'created_at']),
            models.Index(
                fields=['work', 'status', 'created_at'],
                name='core_run_work_status_time_idx',
            ),
            models.Index(
                fields=['work', 'fencing_token'],
                name='core_run_work_token_idx',
            ),
            models.Index(
                fields=['organization', 'project', 'failure_class', 'finished_at'],
                name='core_run_scope_failclass_idx',
            ),
        ]
        ordering = ['organization_id', '-created_at']

    def clean(self) -> None:
        errors: dict[str, list[str]] = {}
        if self.project_id:
            check_project_organization(errors, 'project', self.project, self.organization_id)
        if self.team_id:
            check_organization_scope(errors, 'team', self.team, self.organization_id)
        if self.work_id:
            check_project_scope(errors, 'work', self.work, self.organization_id, self.project_id)
            if self.work.team_id != self.team_id:
                add_scope_error(errors, 'work', 'work team must match workflow run team')
            if self.work.work_type != self.run_type:
                add_scope_error(errors, 'work', 'work type must match workflow run type')
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


def enforce_immutable_fields(
    instance: models.Model,
    fields: tuple[tuple[str, str], ...],
    errors: dict[str, list[str]],
    *,
    extra_fields: tuple[str, ...] = (),
) -> dict[str, object] | None:
    if instance._state.adding:
        return None

    value_fields = [value_field for value_field, _ in fields]
    persisted = type(instance).objects.filter(pk=instance.pk).values(*value_fields, *extra_fields).first()
    if persisted is None:
        return None

    for value_field, error_field in fields:
        if getattr(instance, value_field) != persisted[value_field]:
            add_scope_error(errors, error_field, f'{error_field} is immutable')

    return persisted


class DistillationStageKind(models.TextChoices):
    EXTRACT = 'extract', 'Extract'
    REDUCE = 'reduce', 'Reduce'


class DistillationStageStatus(models.TextChoices):
    REQUIRED = 'required', 'Required'
    COMPLETE = 'complete', 'Complete'


class DistillationStagePolicyRole(models.TextChoices):
    PRIMARY = 'primary', 'Primary'
    FALLBACK = 'fallback', 'Fallback'


class DistillationCoverageOutcome(models.TextChoices):
    SIGNAL = 'signal', 'Signal'
    NO_SIGNAL = 'no_signal', 'No signal'


class DistillationWindow(TimestampedModel):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='distillation_windows')
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='distillation_windows')
    team = models.ForeignKey(
        Team,
        on_delete=models.PROTECT,
        related_name='distillation_windows',
        null=True,
        blank=True,
    )
    work = models.OneToOneField(WorkflowWork, on_delete=models.PROTECT, related_name='distillation_window')
    session = models.ForeignKey(AgentSession, on_delete=models.PROTECT, related_name='distillation_windows')
    contract_version = models.PositiveSmallIntegerField()
    lower_sequence_exclusive = models.PositiveBigIntegerField()
    upper_sequence_inclusive = models.PositiveBigIntegerField()
    observation_count = models.PositiveIntegerField()
    input_hash = models.CharField(max_length=64)
    chunk_char_budget = models.PositiveIntegerField()
    reduction_target = models.PositiveIntegerField()
    chunk_contract_version = models.PositiveSmallIntegerField()

    _IMMUTABLE_FIELDS = (
        ('organization_id', 'organization'),
        ('project_id', 'project'),
        ('team_id', 'team'),
        ('work_id', 'work'),
        ('session_id', 'session'),
        ('contract_version', 'contract_version'),
        ('lower_sequence_exclusive', 'lower_sequence_exclusive'),
        ('upper_sequence_inclusive', 'upper_sequence_inclusive'),
        ('observation_count', 'observation_count'),
        ('input_hash', 'input_hash'),
        ('chunk_char_budget', 'chunk_char_budget'),
        ('reduction_target', 'reduction_target'),
        ('chunk_contract_version', 'chunk_contract_version'),
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'project', 'session', 'input_hash'],
                name='core_distill_window_scope_hash_uniq',
            ),
            models.CheckConstraint(
                condition=models.Q(lower_sequence_exclusive__gte=0)
                & models.Q(upper_sequence_inclusive__gt=models.F('lower_sequence_exclusive')),
                name='core_distill_window_bounds_ck',
            ),
            models.CheckConstraint(
                condition=models.Q(observation_count__gt=0),
                name='core_distill_window_obs_count_pos',
            ),
            models.CheckConstraint(
                condition=models.Q(chunk_char_budget__gt=0),
                name='core_distill_window_budget_pos',
            ),
            models.CheckConstraint(
                condition=models.Q(reduction_target__gt=0),
                name='core_distill_window_reduction_target_pos',
            ),
            models.CheckConstraint(
                condition=models.Q(input_hash__regex=r'^[0-9a-f]{64}$'),
                name='core_distill_window_input_hash_hex',
            ),
            models.CheckConstraint(
                condition=models.Q(contract_version=1),
                name='core_distill_window_contract_ck',
            ),
            models.CheckConstraint(
                condition=models.Q(chunk_contract_version=1),
                name='core_distill_window_chunk_contract_ck',
            ),
        ]
        indexes = [
            models.Index(
                fields=['organization', 'project', 'session', 'upper_sequence_inclusive'],
                name='core_distill_window_scope_idx',
            ),
        ]
        ordering = ['organization_id', 'project_id', 'created_at']

    def clean(self) -> None:
        errors: dict[str, list[str]] = {}
        if self.project_id:
            check_project_organization(errors, 'project', self.project, self.organization_id)
        if self.team_id:
            check_organization_scope(errors, 'team', self.team, self.organization_id)
        if self.session_id:
            check_project_scope(errors, 'session', self.session, self.organization_id, self.project_id)
        if self.work_id:
            check_project_scope(errors, 'work', self.work, self.organization_id, self.project_id)
            if self.session_id and self.work.subject_id != self.session_id:
                add_scope_error(errors, 'work', 'work subject must match window session')
            if self.work.team_id != self.team_id:
                add_scope_error(errors, 'work', 'work team must match window team')
        if self.session_id and self.session.team_id != self.team_id:
            add_scope_error(errors, 'session', 'session team must match window team')
        enforce_immutable_fields(self, self._IMMUTABLE_FIELDS, errors)
        raise_scope_errors(errors)

    def __str__(self) -> str:
        return f'window:{self.work_id}'


class DistillationChunk(TimestampedModel):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='distillation_chunks')
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='distillation_chunks')
    team = models.ForeignKey(
        Team,
        on_delete=models.PROTECT,
        related_name='distillation_chunks',
        null=True,
        blank=True,
    )
    window = models.ForeignKey(DistillationWindow, on_delete=models.PROTECT, related_name='chunks')
    ordinal = models.PositiveIntegerField()
    first_sequence = models.PositiveBigIntegerField()
    last_sequence = models.PositiveBigIntegerField()
    observation_count = models.PositiveIntegerField()
    input_manifest = models.JSONField()
    input_hash = models.CharField(max_length=64)

    _IMMUTABLE_FIELDS = (
        ('organization_id', 'organization'),
        ('project_id', 'project'),
        ('team_id', 'team'),
        ('window_id', 'window'),
        ('ordinal', 'ordinal'),
        ('first_sequence', 'first_sequence'),
        ('last_sequence', 'last_sequence'),
        ('observation_count', 'observation_count'),
        ('input_manifest', 'input_manifest'),
        ('input_hash', 'input_hash'),
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['window', 'ordinal'],
                name='core_distill_chunk_window_ordinal_uniq',
            ),
            models.UniqueConstraint(
                fields=['window', 'input_hash'],
                name='core_distill_chunk_window_hash_uniq',
            ),
            models.CheckConstraint(
                condition=models.Q(first_sequence__gt=0) & models.Q(last_sequence__gte=models.F('first_sequence')),
                name='core_distill_chunk_sequence_bounds_ck',
            ),
            models.CheckConstraint(
                condition=models.Q(observation_count__gt=0),
                name='core_distill_chunk_obs_count_pos',
            ),
            models.CheckConstraint(
                condition=models.Q(input_hash__regex=r'^[0-9a-f]{64}$'),
                name='core_distill_chunk_input_hash_hex',
            ),
        ]
        indexes = [
            models.Index(
                fields=['organization', 'project', 'window', 'ordinal'],
                name='core_distill_chunk_scope_idx',
            ),
        ]
        ordering = ['window_id', 'ordinal']

    def clean(self) -> None:
        errors: dict[str, list[str]] = {}
        if self.project_id:
            check_project_organization(errors, 'project', self.project, self.organization_id)
        if self.team_id:
            check_organization_scope(errors, 'team', self.team, self.organization_id)
        if self.window_id:
            check_window_scope(errors, self.window, self.organization_id, self.project_id, self.team_id)
        enforce_immutable_fields(self, self._IMMUTABLE_FIELDS, errors)
        raise_scope_errors(errors)

    def __str__(self) -> str:
        return f'chunk:{self.window_id}:{self.ordinal}'


class DistillationStage(TimestampedModel):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='distillation_stages')
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='distillation_stages')
    team = models.ForeignKey(
        Team,
        on_delete=models.PROTECT,
        related_name='distillation_stages',
        null=True,
        blank=True,
    )
    window = models.ForeignKey(DistillationWindow, on_delete=models.PROTECT, related_name='stages')
    chunk = models.ForeignKey(
        DistillationChunk,
        on_delete=models.PROTECT,
        related_name='stages',
        null=True,
        blank=True,
    )
    stage_kind = models.CharField(max_length=16, choices=DistillationStageKind.choices)
    level = models.PositiveSmallIntegerField()
    ordinal = models.PositiveIntegerField()
    target_key = models.CharField(max_length=64)
    stage_key = models.CharField(max_length=64)
    input_hash = models.CharField(max_length=64)
    input_manifest = models.JSONField()
    prompt_contract = models.CharField(max_length=80)
    policy = models.ForeignKey('model_policy.ModelPolicy', on_delete=models.PROTECT, related_name='distillation_stages')
    policy_version = models.PositiveIntegerField()
    policy_role = models.CharField(max_length=16, choices=DistillationStagePolicyRole.choices)
    status = models.CharField(max_length=16, choices=DistillationStageStatus.choices)
    attempt_count = models.PositiveIntegerField(default=0)
    last_failure_class = models.CharField(max_length=80, blank=True, default='')
    last_failure_at = models.DateTimeField(null=True, blank=True)
    accepted_provider_call = models.OneToOneField(
        'model_policy.ProviderCallRecord',
        on_delete=models.PROTECT,
        related_name='accepted_distillation_stage',
        null=True,
        blank=True,
    )
    response_hash = models.CharField(max_length=64, blank=True, default='')
    response_size = models.PositiveIntegerField(null=True, blank=True)
    output_snapshot = models.JSONField(null=True, blank=True)
    output_hash = models.CharField(max_length=64, blank=True, default='')
    completed_at = models.DateTimeField(null=True, blank=True)

    _IMMUTABLE_FIELDS = (
        ('organization_id', 'organization'),
        ('project_id', 'project'),
        ('team_id', 'team'),
        ('window_id', 'window'),
        ('chunk_id', 'chunk'),
        ('stage_kind', 'stage_kind'),
        ('level', 'level'),
        ('ordinal', 'ordinal'),
        ('target_key', 'target_key'),
        ('stage_key', 'stage_key'),
        ('input_hash', 'input_hash'),
        ('input_manifest', 'input_manifest'),
        ('prompt_contract', 'prompt_contract'),
        ('policy_id', 'policy'),
        ('policy_version', 'policy_version'),
        ('policy_role', 'policy_role'),
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'project', 'stage_key'],
                name='core_distill_stage_key_uniq',
            ),
            models.UniqueConstraint(
                fields=['window', 'stage_kind', 'level', 'ordinal', 'policy', 'policy_version'],
                name='core_distill_stage_coord_uniq',
            ),
            models.UniqueConstraint(
                fields=['window', 'target_key'],
                condition=models.Q(status=DistillationStageStatus.COMPLETE),
                name='core_distill_stage_target_complete_uniq',
            ),
            models.CheckConstraint(
                condition=~models.Q(stage_kind=DistillationStageKind.EXTRACT)
                | (models.Q(chunk__isnull=False) & models.Q(level=0)),
                name='core_distill_stage_extract_shape_ck',
            ),
            models.CheckConstraint(
                condition=~models.Q(stage_kind=DistillationStageKind.REDUCE)
                | (models.Q(chunk__isnull=True) & models.Q(level__gt=0)),
                name='core_distill_stage_reduce_shape_ck',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        status=DistillationStageStatus.REQUIRED,
                        accepted_provider_call__isnull=True,
                        response_hash='',
                        response_size__isnull=True,
                        output_snapshot__isnull=True,
                        output_hash='',
                        completed_at__isnull=True,
                    )
                    | models.Q(
                        status=DistillationStageStatus.COMPLETE,
                        accepted_provider_call__isnull=False,
                        response_hash__regex=r'^[0-9a-f]{64}$',
                        response_size__gt=0,
                        output_snapshot__isnull=False,
                        output_hash__regex=r'^[0-9a-f]{64}$',
                        completed_at__isnull=False,
                    )
                ),
                name='core_distill_stage_status_shape_ck',
            ),
            models.CheckConstraint(
                condition=models.Q(policy_version__gt=0),
                name='core_distill_stage_policy_version_pos',
            ),
            models.CheckConstraint(
                condition=models.Q(target_key__regex=r'^[0-9a-f]{64}$'),
                name='core_distill_stage_target_key_hex',
            ),
            models.CheckConstraint(
                condition=models.Q(stage_key__regex=r'^[0-9a-f]{64}$'),
                name='core_distill_stage_stage_key_hex',
            ),
            models.CheckConstraint(
                condition=models.Q(input_hash__regex=r'^[0-9a-f]{64}$'),
                name='core_distill_stage_input_hash_hex',
            ),
        ]
        indexes = [
            models.Index(
                fields=['organization', 'project', 'window', 'stage_kind', 'status'],
                name='core_distill_stage_scope_idx',
            ),
        ]
        ordering = ['window_id', 'stage_kind', 'level', 'ordinal']

    _COMPLETION_FIELDS = (
        ('output_snapshot', 'output_snapshot'),
        ('output_hash', 'output_hash'),
        ('response_hash', 'response_hash'),
        ('response_size', 'response_size'),
        ('accepted_provider_call_id', 'accepted_provider_call'),
        ('completed_at', 'completed_at'),
    )

    def clean(self) -> None:
        errors: dict[str, list[str]] = {}
        if self.project_id:
            check_project_organization(errors, 'project', self.project, self.organization_id)
        if self.team_id:
            check_organization_scope(errors, 'team', self.team, self.organization_id)
        if self.policy_id:
            check_organization_scope(errors, 'policy', self.policy, self.organization_id)
        if self.window_id:
            check_window_scope(errors, self.window, self.organization_id, self.project_id, self.team_id)
        if self.chunk_id:
            check_project_scope(errors, 'chunk', self.chunk, self.organization_id, self.project_id)
            if self.window_id and self.chunk.window_id != self.window_id:
                add_scope_error(errors, 'chunk', 'chunk window must match stage window')
        extra_fields = ('status', *(value_field for value_field, _ in self._COMPLETION_FIELDS))
        persisted = enforce_immutable_fields(self, self._IMMUTABLE_FIELDS, errors, extra_fields=extra_fields)
        self._enforce_completion_immutable(persisted, errors)
        raise_scope_errors(errors)

    def _enforce_completion_immutable(self, persisted: dict[str, object] | None, errors: dict[str, list[str]]) -> None:
        if persisted is None or persisted['status'] != DistillationStageStatus.COMPLETE:
            return

        if self.status != DistillationStageStatus.COMPLETE:
            add_scope_error(errors, 'status', 'completed stage cannot return to required')
        for value_field, error_field in self._COMPLETION_FIELDS:
            if getattr(self, value_field) != persisted[value_field]:
                add_scope_error(errors, error_field, f'{error_field} is immutable once complete')

        return

    def __str__(self) -> str:
        return f'stage:{self.window_id}:{self.stage_kind}:{self.level}:{self.ordinal}'


class DistillationObservationCoverage(TimestampedModel):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='distillation_coverages')
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='distillation_coverages')
    team = models.ForeignKey(
        Team,
        on_delete=models.PROTECT,
        related_name='distillation_coverages',
        null=True,
        blank=True,
    )
    window = models.ForeignKey(DistillationWindow, on_delete=models.PROTECT, related_name='coverages')
    observation = models.ForeignKey(Observation, on_delete=models.PROTECT, related_name='distillation_coverages')
    session_sequence = models.PositiveBigIntegerField()
    observation_digest = models.CharField(max_length=64)
    outcome = models.CharField(max_length=16, choices=DistillationCoverageOutcome.choices)
    deciding_stage = models.ForeignKey(DistillationStage, on_delete=models.PROTECT, related_name='coverages')

    _IMMUTABLE_FIELDS = (
        ('organization_id', 'organization'),
        ('project_id', 'project'),
        ('team_id', 'team'),
        ('window_id', 'window'),
        ('observation_id', 'observation'),
        ('session_sequence', 'session_sequence'),
        ('observation_digest', 'observation_digest'),
        ('outcome', 'outcome'),
        ('deciding_stage_id', 'deciding_stage'),
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['window', 'observation'],
                name='core_distill_coverage_window_obs_uniq',
            ),
            models.UniqueConstraint(
                fields=['window', 'session_sequence'],
                name='core_distill_coverage_window_seq_uniq',
            ),
            models.CheckConstraint(
                condition=models.Q(observation_digest__regex=r'^[0-9a-f]{64}$'),
                name='core_distill_coverage_digest_hex',
            ),
            models.CheckConstraint(
                condition=models.Q(session_sequence__gt=0),
                name='core_distill_coverage_seq_pos',
            ),
        ]
        indexes = [
            models.Index(
                fields=['organization', 'project', 'window'],
                name='core_distill_cov_scope_idx',
            ),
        ]
        ordering = ['window_id', 'session_sequence']

    def clean(self) -> None:
        errors: dict[str, list[str]] = {}
        if self.project_id:
            check_project_organization(errors, 'project', self.project, self.organization_id)
        if self.team_id:
            check_organization_scope(errors, 'team', self.team, self.organization_id)
        if self.window_id:
            check_window_scope(errors, self.window, self.organization_id, self.project_id, self.team_id)
        if self.observation_id:
            check_project_scope(errors, 'observation', self.observation, self.organization_id, self.project_id)
            if self.window_id and self.observation.session_id != self.window.session_id:
                add_scope_error(errors, 'observation', 'observation session must match window session')
        enforce_immutable_fields(self, self._IMMUTABLE_FIELDS, errors)
        raise_scope_errors(errors)

    def __str__(self) -> str:
        return f'coverage:{self.window_id}:{self.session_sequence}'


class MemoryCandidateSource(TimestampedModel):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='memory_candidate_sources')
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='memory_candidate_sources')
    team = models.ForeignKey(
        Team,
        on_delete=models.PROTECT,
        related_name='memory_candidate_sources',
        null=True,
        blank=True,
    )
    candidate = models.ForeignKey(MemoryCandidate, on_delete=models.PROTECT, related_name='sources')
    window = models.ForeignKey(DistillationWindow, on_delete=models.PROTECT, related_name='candidate_sources')
    observation = models.ForeignKey(Observation, on_delete=models.PROTECT, related_name='candidate_sources')
    stage = models.ForeignKey(DistillationStage, on_delete=models.PROTECT, related_name='candidate_sources')
    anchors = models.JSONField()
    anchors_hash = models.CharField(max_length=64)

    _IMMUTABLE_FIELDS = (
        ('organization_id', 'organization'),
        ('project_id', 'project'),
        ('team_id', 'team'),
        ('candidate_id', 'candidate'),
        ('window_id', 'window'),
        ('observation_id', 'observation'),
        ('stage_id', 'stage'),
        ('anchors', 'anchors'),
        ('anchors_hash', 'anchors_hash'),
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['candidate', 'window', 'observation'],
                name='core_candidate_source_uniq',
            ),
            models.CheckConstraint(
                condition=models.Q(anchors_hash__regex=r'^[0-9a-f]{64}$'),
                name='core_candidate_source_anchors_hex',
            ),
        ]
        indexes = [
            models.Index(
                fields=['organization', 'project', 'candidate'],
                name='core_cand_source_scope_idx',
            ),
            models.Index(
                fields=['window', 'observation'],
                name='core_cand_source_winobs_idx',
            ),
        ]
        ordering = ['candidate_id', 'window_id', 'observation_id']

    def clean(self) -> None:
        errors: dict[str, list[str]] = {}
        if self.project_id:
            check_project_organization(errors, 'project', self.project, self.organization_id)
        if self.team_id:
            check_organization_scope(errors, 'team', self.team, self.organization_id)
        if self.window_id:
            check_window_scope(errors, self.window, self.organization_id, self.project_id, self.team_id)
        enforce_immutable_fields(self, self._IMMUTABLE_FIELDS, errors)
        raise_scope_errors(errors)

    def __str__(self) -> str:
        return f'source:{self.candidate_id}:{self.observation_id}'
