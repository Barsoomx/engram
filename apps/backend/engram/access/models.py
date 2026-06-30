from __future__ import annotations

from django.db import models

from engram.core.models import (
    Organization,
    Project,
    ProjectTeam,
    Team,
    TimestampedModel,
    add_scope_error,
    check_organization_scope,
    check_project_organization,
    raise_scope_errors,
)


class IdentityType(models.TextChoices):
    USER = 'user', 'User'
    SERVICE_ACCOUNT = 'service_account', 'Service account'


class Identity(TimestampedModel):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='identities')
    identity_type = models.CharField(
        max_length=40,
        choices=IdentityType.choices,
        default=IdentityType.SERVICE_ACCOUNT,
    )
    external_id = models.CharField(max_length=255)
    display_name = models.CharField(max_length=255)
    email = models.EmailField(blank=True)
    active = models.BooleanField(default=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'identity_type', 'external_id'],
                name='access_identity_unique_external_id_per_org_type',
            ),
        ]
        indexes = [
            models.Index(fields=['organization', 'identity_type', 'active']),
        ]
        ordering = ['organization_id', 'identity_type', 'external_id']

    def __str__(self) -> str:
        return f'{self.identity_type}:{self.external_id}'


class Capability(TimestampedModel):
    code = models.CharField(max_length=120, unique=True)
    description = models.TextField(blank=True)

    class Meta:
        ordering = ['code']

    def __str__(self) -> str:
        return self.code


class Role(TimestampedModel):
    code = models.CharField(max_length=120, unique=True)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    built_in = models.BooleanField(default=True)

    class Meta:
        ordering = ['code']

    def __str__(self) -> str:
        return self.code


class RoleCapability(TimestampedModel):
    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name='capability_links')
    capability = models.ForeignKey(Capability, on_delete=models.CASCADE, related_name='role_links')

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['role', 'capability'], name='access_role_capability_unique_pair'),
        ]
        ordering = ['role_id', 'capability_id']

    def __str__(self) -> str:
        return f'{self.role_id}:{self.capability_id}'


class MembershipStatus(models.TextChoices):
    ACTIVE = 'active', 'Active'
    INVITED = 'invited', 'Invited'
    SUSPENDED = 'suspended', 'Suspended'


class OrganizationMembership(TimestampedModel):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='organization_memberships')
    identity = models.ForeignKey(Identity, on_delete=models.CASCADE, related_name='organization_memberships')
    role = models.ForeignKey(Role, on_delete=models.PROTECT, related_name='organization_memberships')
    active = models.BooleanField(default=True)
    status = models.CharField(
        max_length=40,
        choices=MembershipStatus.choices,
        default=MembershipStatus.ACTIVE,
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'identity'],
                name='access_org_membership_unique_identity',
            ),
        ]
        indexes = [
            models.Index(fields=['organization', 'active']),
        ]
        ordering = ['organization_id', 'identity_id']

    def clean(self) -> None:
        errors: dict[str, list[str]] = {}
        if self.identity_id:
            check_organization_scope(errors, 'identity', self.identity, self.organization_id)
        raise_scope_errors(errors)

    def __str__(self) -> str:
        return f'{self.organization_id}:{self.identity_id}'


class TeamMembership(TimestampedModel):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='team_memberships')
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name='memberships')
    identity = models.ForeignKey(Identity, on_delete=models.CASCADE, related_name='team_memberships')
    role = models.ForeignKey(Role, on_delete=models.PROTECT, related_name='team_memberships')
    active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['team', 'identity'], name='access_team_membership_unique_identity'),
        ]
        indexes = [
            models.Index(fields=['organization', 'team', 'active']),
        ]
        ordering = ['organization_id', 'team_id', 'identity_id']

    def clean(self) -> None:
        errors: dict[str, list[str]] = {}
        if self.team_id:
            check_organization_scope(errors, 'team', self.team, self.organization_id)
        if self.identity_id:
            check_organization_scope(errors, 'identity', self.identity, self.organization_id)
        raise_scope_errors(errors)

    def __str__(self) -> str:
        return f'{self.team_id}:{self.identity_id}'


class ProjectGrant(TimestampedModel):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='project_grants')
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='access_grants')
    identity = models.ForeignKey(Identity, on_delete=models.CASCADE, related_name='project_grants')
    role = models.ForeignKey(Role, on_delete=models.PROTECT, related_name='project_grants')
    active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['project', 'identity'], name='access_project_grant_unique_identity'),
        ]
        indexes = [
            models.Index(fields=['organization', 'project', 'active']),
        ]
        ordering = ['organization_id', 'project_id', 'identity_id']

    def clean(self) -> None:
        errors: dict[str, list[str]] = {}
        if self.project_id:
            check_project_organization(errors, 'project', self.project, self.organization_id)
        if self.identity_id:
            check_organization_scope(errors, 'identity', self.identity, self.organization_id)
        raise_scope_errors(errors)

    def __str__(self) -> str:
        return f'{self.project_id}:{self.identity_id}'


class ApiKey(TimestampedModel):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='api_keys')
    owner_identity = models.ForeignKey(Identity, on_delete=models.PROTECT, related_name='api_keys')
    name = models.CharField(max_length=255)
    key_prefix = models.CharField(max_length=16)
    key_hash = models.CharField(max_length=64, unique=True)
    key_fingerprint = models.CharField(max_length=64, unique=True)
    team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name='api_keys', null=True, blank=True)
    project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name='api_keys', null=True, blank=True)
    active = models.BooleanField(default=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['organization', 'key_prefix']),
            models.Index(fields=['organization', 'project', 'active']),
            models.Index(fields=['organization', 'owner_identity']),
        ]
        ordering = ['organization_id', 'name']

    def clean(self) -> None:
        errors: dict[str, list[str]] = {}
        if self.owner_identity_id:
            check_organization_scope(errors, 'owner_identity', self.owner_identity, self.organization_id)
        if self.team_id:
            check_organization_scope(errors, 'team', self.team, self.organization_id)
        if self.project_id:
            check_project_organization(errors, 'project', self.project, self.organization_id)
        if not errors and self.team_id and self.project_id:
            linked = ProjectTeam.objects.filter(
                organization_id=self.organization_id,
                project_id=self.project_id,
                team_id=self.team_id,
            ).exists()
            if not linked:
                add_scope_error(errors, 'team', 'team must be linked to api key project')
        raise_scope_errors(errors)

    def __str__(self) -> str:
        return f'{self.organization_id}:{self.key_prefix}'


class ApiKeyCapability(TimestampedModel):
    api_key = models.ForeignKey(ApiKey, on_delete=models.CASCADE, related_name='capability_links')
    capability = models.ForeignKey(Capability, on_delete=models.CASCADE, related_name='api_key_links')

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['api_key', 'capability'], name='access_api_key_capability_unique_pair'),
        ]
        ordering = ['api_key_id', 'capability_id']

    def __str__(self) -> str:
        return f'{self.api_key_id}:{self.capability_id}'
