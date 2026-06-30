from __future__ import annotations

from django.db import models

from engram.core.models import (
    AuditResult,
    Organization,
    Project,
    Team,
    TimestampedModel,
    add_scope_error,
    check_organization_scope,
    check_project_organization,
    raise_scope_errors,
)


class Provider(models.TextChoices):
    ANTHROPIC = 'anthropic', 'Anthropic'
    OPENAI = 'openai', 'OpenAI'
    DEEPSEEK = 'deepseek', 'DeepSeek'


class SecretScope(models.TextChoices):
    ORGANIZATION = 'organization', 'Organization'
    TEAM = 'team', 'Team'


class PolicyScope(models.TextChoices):
    ORGANIZATION = 'organization', 'Organization'
    TEAM = 'team', 'Team'
    PROJECT = 'project', 'Project'


class TaskType(models.TextChoices):
    GENERATION = 'generation', 'Generation'
    EMBEDDING = 'embedding', 'Embedding'
    CURATION = 'curation', 'Curation'
    DIGEST = 'digest', 'Digest'
    RERANK = 'rerank', 'Rerank'
    ADMIN_ASSISTANT = 'admin_assistant', 'Admin Assistant'


class SecretStorageMode(models.TextChoices):
    DATABASE_ENVELOPE = 'database_envelope', 'Database Envelope'
    EXTERNAL_VAULT = 'external_vault', 'External Vault'


class ProviderSecret(TimestampedModel):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='provider_secrets')
    team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name='provider_secrets', null=True, blank=True)
    name = models.CharField(max_length=255)
    provider = models.CharField(max_length=40, choices=Provider.choices)
    scope = models.CharField(max_length=40, choices=SecretScope.choices)
    storage_mode = models.CharField(
        max_length=40,
        choices=SecretStorageMode.choices,
        default=SecretStorageMode.DATABASE_ENVELOPE,
    )
    current_version = models.PositiveIntegerField(default=1)
    active = models.BooleanField(default=True)
    rotation_state = models.CharField(max_length=40, default='active')
    secret_fingerprint = models.CharField(max_length=80, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['organization', 'provider', 'active']),
            models.Index(fields=['organization', 'team', 'provider']),
        ]
        ordering = ['organization_id', 'team_id', 'name']

    def clean(self) -> None:
        errors: dict[str, list[str]] = {}
        if self.team_id:
            check_organization_scope(errors, 'team', self.team, self.organization_id)
        if self.scope == SecretScope.ORGANIZATION and self.team_id:
            add_scope_error(errors, 'team', 'organization scoped secret cannot reference a team')
        if self.scope == SecretScope.TEAM and not self.team_id:
            add_scope_error(errors, 'team', 'team scoped secret requires a team')
        raise_scope_errors(errors)

    def __str__(self) -> str:
        return f'{self.provider}:{self.name}'


class ProviderSecretEnvelope(TimestampedModel):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='provider_secret_envelopes')
    team = models.ForeignKey(
        Team,
        on_delete=models.PROTECT,
        related_name='provider_secret_envelopes',
        null=True,
        blank=True,
    )
    secret = models.ForeignKey(ProviderSecret, on_delete=models.CASCADE, related_name='envelopes')
    version = models.PositiveIntegerField()
    key_version = models.CharField(max_length=40)
    ciphertext = models.TextField()
    hmac_digest = models.CharField(max_length=128)
    active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['secret', 'version'], name='model_policy_secret_envelope_unique_version'),
        ]
        indexes = [
            models.Index(fields=['organization', 'secret', 'active']),
        ]
        ordering = ['secret_id', 'version']

    def clean(self) -> None:
        errors: dict[str, list[str]] = {}
        if self.team_id:
            check_organization_scope(errors, 'team', self.team, self.organization_id)
        if self.secret_id:
            check_organization_scope(errors, 'secret', self.secret, self.organization_id)
            if self.team_id != self.secret.team_id:
                add_scope_error(errors, 'team', 'envelope team must match secret team')
        raise_scope_errors(errors)

    def __str__(self) -> str:
        return f'{self.secret_id}:v{self.version}'


class ModelPolicy(TimestampedModel):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='model_policies')
    team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name='model_policies', null=True, blank=True)
    project = models.ForeignKey(
        Project,
        on_delete=models.PROTECT,
        related_name='model_policies',
        null=True,
        blank=True,
    )
    secret = models.ForeignKey(ProviderSecret, on_delete=models.PROTECT, related_name='model_policies')
    name = models.CharField(max_length=255)
    scope = models.CharField(max_length=40, choices=PolicyScope.choices)
    task_type = models.CharField(max_length=40, choices=TaskType.choices)
    provider = models.CharField(max_length=40, choices=Provider.choices)
    model = models.CharField(max_length=120)
    version = models.PositiveIntegerField(default=1)
    active = models.BooleanField(default=True)
    allowed_providers = models.JSONField(default=list, blank=True)
    blocked_providers = models.JSONField(default=list, blank=True)
    fallback_enabled = models.BooleanField(default=False)
    retention_policy = models.JSONField(default=dict, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['organization', 'project', 'task_type', 'active']),
            models.Index(fields=['organization', 'team', 'task_type', 'active']),
            models.Index(fields=['organization', 'scope', 'task_type']),
        ]
        ordering = ['organization_id', 'scope', 'task_type', 'name']

    def clean(self) -> None:
        errors: dict[str, list[str]] = {}
        if self.team_id:
            check_organization_scope(errors, 'team', self.team, self.organization_id)
        if self.project_id:
            check_project_organization(errors, 'project', self.project, self.organization_id)
        if self.secret_id:
            check_organization_scope(errors, 'secret', self.secret, self.organization_id)
            if self.provider and self.secret.provider != self.provider:
                add_scope_error(errors, 'secret', 'secret provider must match policy provider')
            if self.secret.team_id and self.team_id and self.secret.team_id != self.team_id:
                add_scope_error(errors, 'secret', 'secret team must match policy team')
        if self.scope == PolicyScope.ORGANIZATION and (self.team_id or self.project_id):
            add_scope_error(errors, 'scope', 'organization policy cannot reference team or project')
        if self.scope == PolicyScope.TEAM and (not self.team_id or self.project_id):
            add_scope_error(errors, 'scope', 'team policy requires team and no project')
        if self.scope == PolicyScope.PROJECT and not self.project_id:
            add_scope_error(errors, 'project', 'project policy requires project')
        raise_scope_errors(errors)

    def __str__(self) -> str:
        return f'{self.task_type}:{self.provider}:{self.model}'


class ProviderCallRecord(TimestampedModel):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='provider_call_records')
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='provider_call_records')
    team = models.ForeignKey(
        Team,
        on_delete=models.PROTECT,
        related_name='provider_call_records',
        null=True,
        blank=True,
    )
    policy = models.ForeignKey(ModelPolicy, on_delete=models.PROTECT, related_name='provider_call_records')
    secret = models.ForeignKey(ProviderSecret, on_delete=models.PROTECT, related_name='provider_call_records')
    provider = models.CharField(max_length=40, choices=Provider.choices)
    model = models.CharField(max_length=120)
    task_type = models.CharField(max_length=40, choices=TaskType.choices)
    policy_version = models.PositiveIntegerField()
    request_id = models.CharField(max_length=255)
    trace_id = models.CharField(max_length=255, blank=True)
    redaction_state = models.CharField(max_length=40)
    token_usage = models.JSONField(default=dict, blank=True)
    latency_ms = models.PositiveIntegerField(default=0)
    cost_metadata = models.JSONField(default=dict, blank=True)
    result = models.CharField(max_length=40, choices=AuditResult.choices, default=AuditResult.RECORDED)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['organization', 'project', 'task_type']),
            models.Index(fields=['organization', 'provider', 'model']),
            models.Index(fields=['organization', 'project', 'task_type', 'request_id']),
        ]
        ordering = ['organization_id', 'project_id', 'created_at']

    def clean(self) -> None:
        errors: dict[str, list[str]] = {}
        if self.project_id:
            check_project_organization(errors, 'project', self.project, self.organization_id)
        if self.team_id:
            check_organization_scope(errors, 'team', self.team, self.organization_id)
        if self.policy_id:
            check_organization_scope(errors, 'policy', self.policy, self.organization_id)
        if self.secret_id:
            check_organization_scope(errors, 'secret', self.secret, self.organization_id)
        raise_scope_errors(errors)

    def __str__(self) -> str:
        return f'{self.provider}:{self.model}:{self.request_id}'
