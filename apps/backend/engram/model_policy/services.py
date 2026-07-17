from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import structlog
from cryptography.fernet import Fernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.db.models import Q

from engram.core.environments import NON_PRODUCTION_ENVIRONMENTS
from engram.core.models import AuditEvent, AuditResult, Project, Team
from engram.core.redaction import redact_value
from engram.model_policy.base_url_validation import BaseUrlValidationError, validate_base_url
from engram.model_policy.errors import ModelPolicyError, ProviderSecretError
from engram.model_policy.models import (
    ModelPolicy,
    PolicyScope,
    Provider,
    ProviderCallRecord,
    ProviderSecret,
    ProviderSecretEnvelope,
    SecretScope,
)

logger = structlog.get_logger(__name__)

SECRET_KEY_VERSION = 'v1'

_MODEL_PREFIX_CONTEXT_WINDOWS = {
    'claude-': 200000,
    'gpt-5': 400000,
    'gpt-4': 128000,
    'deepseek-': 128000,
    'glm-': 128000,
}


@dataclass(frozen=True)
class ProviderSecretInput:
    organization_id: uuid.UUID
    project_id: uuid.UUID
    team_id: uuid.UUID | None
    name: str
    provider: str
    scope: str
    raw_secret: str
    request_id: str
    actor_id: str
    allowed_team_ids: tuple[uuid.UUID, ...] = ()


@dataclass(frozen=True)
class RotateProviderSecretInput:
    organization_id: uuid.UUID
    project_id: uuid.UUID
    team_id: uuid.UUID | None
    secret_id: uuid.UUID
    raw_secret: str
    request_id: str
    actor_id: str
    allowed_team_ids: tuple[uuid.UUID, ...] = ()


@dataclass(frozen=True)
class DisableProviderSecretInput:
    organization_id: uuid.UUID
    project_id: uuid.UUID
    team_id: uuid.UUID | None
    secret_id: uuid.UUID
    request_id: str
    actor_id: str
    allowed_team_ids: tuple[uuid.UUID, ...] = ()


@dataclass(frozen=True)
class ModelPolicyInput:
    organization_id: uuid.UUID
    project_id: uuid.UUID
    team_id: uuid.UUID | None
    name: str
    scope: str
    task_type: str
    provider: str
    model: str
    secret_id: uuid.UUID
    request_id: str
    actor_id: str
    scope_team_id: uuid.UUID | None = None
    base_url: str = ''
    context_window_tokens: int | None = None
    fallback_enabled: bool = False
    json_mode: bool | None = None


@dataclass(frozen=True)
class ResolveModelPolicyInput:
    organization_id: uuid.UUID
    project_id: uuid.UUID
    team_id: uuid.UUID | None
    task_type: str


@dataclass(frozen=True)
class ResolvedModelPolicy:
    policy: ModelPolicy


@dataclass(frozen=True)
class ProviderCallInput:
    organization_id: uuid.UUID
    project_id: uuid.UUID
    team_id: uuid.UUID | None
    policy: ModelPolicy
    request_id: str
    trace_id: str
    prompt: str
    system_prompt: str = ''
    response_kind: str = 'single'


@dataclass(frozen=True)
class ProviderCallResult:
    provider: str
    model: str
    call_record_id: uuid.UUID
    redaction_state: str
    generated_title: str
    generated_body: str


def encryption_key() -> bytes:
    raw_key = getattr(settings, 'ENGRAM_SECRET_ENCRYPTION_KEY', '')
    if raw_key:
        return base64.urlsafe_b64encode(hashlib.sha256(raw_key.encode()).digest())

    environment = getattr(settings, 'ENVIRONMENT', 'dev')
    if environment not in NON_PRODUCTION_ENVIRONMENTS:
        raise ImproperlyConfigured('ENGRAM_SECRET_ENCRYPTION_KEY is required outside dev/test environments')

    digest = hashlib.sha256(f'{settings.SECRET_KEY}:engram-model-policy:{SECRET_KEY_VERSION}'.encode()).digest()

    return base64.urlsafe_b64encode(digest)


def encrypt_secret(raw_secret: str) -> str:
    return Fernet(encryption_key()).encrypt(raw_secret.encode()).decode()


def secret_hmac(raw_secret: str) -> str:
    return hmac.new(encryption_key(), raw_secret.encode(), hashlib.sha256).hexdigest()


def secret_fingerprint(raw_secret: str) -> str:
    digest = hashlib.sha256(raw_secret.encode()).hexdigest()

    return f'sha256:{digest[:12]}...{digest[-12:]}'


def team_for_scope(organization_id: uuid.UUID, team_id: uuid.UUID | None, scope: str) -> Team | None:
    if scope == SecretScope.ORGANIZATION:
        if team_id is not None:
            raise ModelPolicyError('secret_scope_denied', 'Organization-scoped secret cannot use team scope')

        return None
    if team_id is None:
        raise ModelPolicyError('team_required', 'Team-scoped secret requires team_id')

    return Team.objects.get(organization_id=organization_id, id=team_id)


def ensure_secret_scope_allowed(secret: ProviderSecret, allowed_team_ids: tuple[uuid.UUID, ...]) -> None:
    if secret.team_id:
        if secret.team_id not in allowed_team_ids:
            raise ModelPolicyError('secret_scope_denied', 'Secret is outside requested team scope')

        return

    if allowed_team_ids:
        raise ModelPolicyError('secret_scope_denied', 'Secret is outside requested team scope')


def audit_model_policy_event(
    *,
    organization_id: uuid.UUID,
    project_id: uuid.UUID,
    team_id: uuid.UUID | None,
    actor_id: str,
    event_type: str,
    target_type: str,
    target_id: str,
    capability: str,
    request_id: str,
    metadata: dict[str, Any],
) -> None:
    AuditEvent.objects.create(
        organization_id=organization_id,
        project_id=project_id,
        team_id=team_id,
        event_type=event_type,
        actor_type='api_key',
        actor_id=actor_id,
        target_type=target_type,
        target_id=target_id,
        capability=capability,
        result=AuditResult.RECORDED,
        request_id=request_id,
        metadata=redact_value(metadata).value,
    )


class CreateProviderSecret:
    def execute(self, data: ProviderSecretInput) -> ProviderSecret:
        team = team_for_scope(data.organization_id, data.team_id, data.scope)
        if data.scope == SecretScope.ORGANIZATION and data.allowed_team_ids:
            raise ModelPolicyError('secret_scope_denied', 'Secret is outside requested team scope')

        with transaction.atomic():
            secret = ProviderSecret.objects.create(
                organization_id=data.organization_id,
                team=team,
                name=data.name,
                provider=data.provider,
                scope=data.scope,
                current_version=1,
                secret_fingerprint=secret_fingerprint(data.raw_secret),
            )
            ProviderSecretEnvelope.objects.create(
                organization_id=data.organization_id,
                team=team,
                secret=secret,
                version=1,
                key_version=SECRET_KEY_VERSION,
                ciphertext=encrypt_secret(data.raw_secret),
                hmac_digest=secret_hmac(data.raw_secret),
                active=True,
            )
            audit_model_policy_event(
                organization_id=data.organization_id,
                project_id=data.project_id,
                team_id=data.team_id,
                actor_id=data.actor_id,
                event_type='ProviderSecretCreated',
                target_type='provider_secret',
                target_id=str(secret.id),
                capability='secrets:*',
                request_id=data.request_id,
                metadata={
                    'provider': data.provider,
                    'scope': data.scope,
                    'fingerprint': secret_fingerprint(data.raw_secret),
                },
            )
            logger.info(
                'provider_secret_created',
                secret_id=str(secret.id),
                provider=data.provider,
                scope=data.scope,
            )

            return secret


class RotateProviderSecret:
    def execute(self, data: RotateProviderSecretInput) -> ProviderSecret:
        with transaction.atomic():
            secret = ProviderSecret.objects.select_for_update().get(
                organization_id=data.organization_id,
                id=data.secret_id,
            )
            ensure_secret_scope_allowed(secret, data.allowed_team_ids)

            ProviderSecretEnvelope.objects.filter(secret=secret, active=True).update(active=False)
            next_version = secret.current_version + 1
            ProviderSecretEnvelope.objects.create(
                organization_id=secret.organization_id,
                team_id=secret.team_id,
                secret=secret,
                version=next_version,
                key_version=SECRET_KEY_VERSION,
                ciphertext=encrypt_secret(data.raw_secret),
                hmac_digest=secret_hmac(data.raw_secret),
                active=True,
            )
            secret.current_version = next_version
            secret.active = True
            secret.rotation_state = 'rotated'
            secret.secret_fingerprint = secret_fingerprint(data.raw_secret)
            secret.save(
                update_fields=['current_version', 'active', 'rotation_state', 'secret_fingerprint', 'updated_at'],
            )
            audit_model_policy_event(
                organization_id=data.organization_id,
                project_id=data.project_id,
                team_id=data.team_id,
                actor_id=data.actor_id,
                event_type='ProviderSecretRotated',
                target_type='provider_secret',
                target_id=str(secret.id),
                capability='secrets:*',
                request_id=data.request_id,
                metadata={
                    'provider': secret.provider,
                    'fingerprint': secret_fingerprint(data.raw_secret),
                },
            )
            logger.info(
                'provider_secret_rotated',
                secret_id=str(secret.id),
                version=next_version,
            )

            return secret


class DisableProviderSecret:
    def execute(self, data: DisableProviderSecretInput) -> ProviderSecret:
        with transaction.atomic():
            secret = ProviderSecret.objects.select_for_update().get(
                organization_id=data.organization_id,
                id=data.secret_id,
            )
            ensure_secret_scope_allowed(secret, data.allowed_team_ids)

            secret.active = False
            secret.rotation_state = 'disabled'
            secret.save(update_fields=['active', 'rotation_state', 'updated_at'])
            audit_model_policy_event(
                organization_id=data.organization_id,
                project_id=data.project_id,
                team_id=data.team_id,
                actor_id=data.actor_id,
                event_type='ProviderSecretDisabled',
                target_type='provider_secret',
                target_id=str(secret.id),
                capability='secrets:*',
                request_id=data.request_id,
                metadata={'provider': secret.provider},
            )

            return secret


@dataclass(frozen=True)
class DisableModelPolicyInput:
    organization_id: uuid.UUID
    project_id: uuid.UUID
    team_id: uuid.UUID | None
    policy_id: uuid.UUID
    request_id: str
    actor_id: str
    allowed_team_ids: tuple[uuid.UUID, ...] = ()


@dataclass(frozen=True)
class UpdateModelPolicyInput:
    organization_id: uuid.UUID
    project_id: uuid.UUID
    team_id: uuid.UUID | None
    policy_id: uuid.UUID
    request_id: str
    actor_id: str
    allowed_team_ids: tuple[uuid.UUID, ...] = ()
    name: str | None = None
    provider: str | None = None
    model: str | None = None
    secret_id: uuid.UUID | None = None
    active: bool | None = None
    fallback_enabled: bool | None = None
    task_type: str | None = None
    base_url: str | None = None
    context_window_tokens: int | None = None
    clear_context_window_tokens: bool = False


@dataclass(frozen=True)
class EnableProviderSecretInput:
    organization_id: uuid.UUID
    project_id: uuid.UUID
    team_id: uuid.UUID | None
    secret_id: uuid.UUID
    request_id: str
    actor_id: str
    allowed_team_ids: tuple[uuid.UUID, ...] = ()


@dataclass(frozen=True)
class UpdateProviderSecretInput:
    organization_id: uuid.UUID
    project_id: uuid.UUID
    team_id: uuid.UUID | None
    secret_id: uuid.UUID
    name: str
    request_id: str
    actor_id: str
    allowed_team_ids: tuple[uuid.UUID, ...] = ()


def ensure_policy_in_scope(policy: ModelPolicy, allowed_team_ids: tuple[uuid.UUID, ...]) -> None:
    if policy.team_id and policy.team_id not in allowed_team_ids:
        raise ModelPolicyError('model_policy_not_found', 'Model policy was not found')


class CreateModelPolicy:
    def execute(self, data: ModelPolicyInput) -> ModelPolicy:
        team_id = data.scope_team_id if data.scope_team_id is not None else data.team_id
        if data.scope == PolicyScope.TEAM and team_id != data.team_id:
            raise ModelPolicyError('policy_scope_mismatch', 'Policy team scope must match requested team')
        if data.scope == PolicyScope.PROJECT and data.scope_team_id is not None and data.scope_team_id != data.team_id:
            raise ModelPolicyError('policy_scope_mismatch', 'Project policy team scope must match requested team')

        secret = ProviderSecret.objects.get(organization_id=data.organization_id, id=data.secret_id)
        if secret.team_id and secret.team_id != team_id:
            raise ModelPolicyError('policy_scope_mismatch', 'Policy secret team must match policy team')

        project = Project.objects.get(organization_id=data.organization_id, id=data.project_id)
        team = Team.objects.get(organization_id=data.organization_id, id=team_id) if team_id else None
        if data.scope == PolicyScope.ORGANIZATION:
            project = None
            team = None
        elif data.scope == PolicyScope.TEAM:
            project = None

        metadata: dict[str, Any] = {}
        if data.base_url:
            metadata['base_url'] = data.base_url
        if data.context_window_tokens:
            metadata['context_window_tokens'] = data.context_window_tokens
        if data.json_mode is not None:
            metadata['json_mode'] = data.json_mode

        with transaction.atomic():
            policy = ModelPolicy.objects.create(
                organization_id=data.organization_id,
                team=team,
                project=project,
                name=data.name,
                scope=data.scope,
                task_type=data.task_type,
                provider=data.provider,
                model=data.model,
                secret=secret,
                version=1,
                fallback_enabled=data.fallback_enabled,
                metadata=metadata,
            )
            audit_model_policy_event(
                organization_id=data.organization_id,
                project_id=data.project_id,
                team_id=data.team_id,
                actor_id=data.actor_id,
                event_type='ModelPolicyCreated',
                target_type='model_policy',
                target_id=str(policy.id),
                capability='model_policy:*',
                request_id=data.request_id,
                metadata={'provider': data.provider, 'model': data.model, 'task_type': data.task_type},
            )
            logger.info(
                'model_policy_created',
                policy_id=str(policy.id),
                task_type=data.task_type,
                provider=data.provider,
            )

        return policy


class UpdateModelPolicy:
    def _apply_scalar_updates(
        self,
        policy: ModelPolicy,
        data: UpdateModelPolicyInput,
        update_fields: list[str],
    ) -> None:
        scalar_fields = ('name', 'provider', 'model', 'task_type', 'active', 'fallback_enabled')
        for field in scalar_fields:
            value = getattr(data, field)
            if value is not None:
                setattr(policy, field, value)
                update_fields.append(field)

    def _apply_secret_update(
        self,
        policy: ModelPolicy,
        data: UpdateModelPolicyInput,
        update_fields: list[str],
    ) -> None:
        if data.secret_id is None:
            return

        try:
            secret = ProviderSecret.objects.get(
                organization_id=data.organization_id,
                id=data.secret_id,
            )
        except ProviderSecret.DoesNotExist as err:
            raise ModelPolicyError('policy_scope_mismatch', 'Secret not found in organization') from err

        if secret.team_id and secret.team_id != policy.team_id:
            raise ModelPolicyError('policy_scope_mismatch', 'Policy secret team must match policy team')

        policy.secret = secret
        update_fields.append('secret_id')

    def _apply_base_url_update(
        self,
        policy: ModelPolicy,
        data: UpdateModelPolicyInput,
        update_fields: list[str],
    ) -> None:
        if data.base_url is None:
            return

        current = dict(policy.metadata or {})
        if data.base_url:
            current['base_url'] = data.base_url
        else:
            current.pop('base_url', None)
        policy.metadata = current
        update_fields.append('metadata')

    def _apply_context_window_update(
        self,
        policy: ModelPolicy,
        data: UpdateModelPolicyInput,
        update_fields: list[str],
    ) -> None:
        if data.clear_context_window_tokens:
            current = dict(policy.metadata or {})
            if 'context_window_tokens' in current:
                current.pop('context_window_tokens')
                policy.metadata = current
                if 'metadata' not in update_fields:
                    update_fields.append('metadata')

            return

        # clear_context_window_tokens is False here, so the view already determined this
        # None came from an omitted field, not an explicit null; leave any override untouched.
        if data.context_window_tokens is None:
            return

        current = dict(policy.metadata or {})
        current['context_window_tokens'] = data.context_window_tokens
        policy.metadata = current
        if 'metadata' not in update_fields:
            update_fields.append('metadata')

    def execute(self, data: UpdateModelPolicyInput) -> ModelPolicy:
        with transaction.atomic():
            try:
                policy = ModelPolicy.objects.select_for_update().get(
                    organization_id=data.organization_id,
                    id=data.policy_id,
                )
            except ModelPolicy.DoesNotExist as err:
                raise ModelPolicyError('model_policy_not_found', 'Model policy was not found') from err

            ensure_policy_in_scope(policy, data.allowed_team_ids)

            update_fields: list[str] = ['version', 'updated_at']
            self._apply_scalar_updates(policy, data, update_fields)
            self._apply_secret_update(policy, data, update_fields)
            self._apply_base_url_update(policy, data, update_fields)
            self._apply_context_window_update(policy, data, update_fields)

            policy.version += 1
            policy.save(update_fields=update_fields)
            audit_model_policy_event(
                organization_id=data.organization_id,
                project_id=data.project_id,
                team_id=data.team_id,
                actor_id=data.actor_id,
                event_type='ModelPolicyUpdated',
                target_type='model_policy',
                target_id=str(policy.id),
                capability='model_policy:*',
                request_id=data.request_id,
                metadata={'provider': policy.provider, 'model': policy.model, 'task_type': policy.task_type},
            )
            logger.info(
                'model_policy_updated',
                policy_id=str(policy.id),
                version=policy.version,
            )

            return policy


class DisableModelPolicy:
    def execute(self, data: DisableModelPolicyInput) -> ModelPolicy:
        with transaction.atomic():
            try:
                policy = ModelPolicy.objects.select_for_update().get(
                    organization_id=data.organization_id,
                    id=data.policy_id,
                )
            except ModelPolicy.DoesNotExist as err:
                raise ModelPolicyError('model_policy_not_found', 'Model policy was not found') from err

            ensure_policy_in_scope(policy, data.allowed_team_ids)

            policy.active = False
            policy.save(update_fields=['active', 'updated_at'])
            audit_model_policy_event(
                organization_id=data.organization_id,
                project_id=data.project_id,
                team_id=data.team_id,
                actor_id=data.actor_id,
                event_type='ModelPolicyDisabled',
                target_type='model_policy',
                target_id=str(policy.id),
                capability='model_policy:*',
                request_id=data.request_id,
                metadata={'provider': policy.provider, 'model': policy.model},
            )

            return policy


class EnableProviderSecret:
    def execute(self, data: EnableProviderSecretInput) -> ProviderSecret:
        with transaction.atomic():
            secret = ProviderSecret.objects.select_for_update().get(
                organization_id=data.organization_id,
                id=data.secret_id,
            )
            ensure_secret_scope_allowed(secret, data.allowed_team_ids)

            secret.active = True
            secret.rotation_state = 'active'
            secret.save(update_fields=['active', 'rotation_state', 'updated_at'])
            audit_model_policy_event(
                organization_id=data.organization_id,
                project_id=data.project_id,
                team_id=data.team_id,
                actor_id=data.actor_id,
                event_type='ProviderSecretEnabled',
                target_type='provider_secret',
                target_id=str(secret.id),
                capability='secrets:*',
                request_id=data.request_id,
                metadata={'provider': secret.provider},
            )

            return secret


class UpdateProviderSecret:
    def execute(self, data: UpdateProviderSecretInput) -> ProviderSecret:
        with transaction.atomic():
            secret = ProviderSecret.objects.select_for_update().get(
                organization_id=data.organization_id,
                id=data.secret_id,
            )
            ensure_secret_scope_allowed(secret, data.allowed_team_ids)

            secret.name = data.name
            secret.save(update_fields=['name', 'updated_at'])
            audit_model_policy_event(
                organization_id=data.organization_id,
                project_id=data.project_id,
                team_id=data.team_id,
                actor_id=data.actor_id,
                event_type='ProviderSecretRenamed',
                target_type='provider_secret',
                target_id=str(secret.id),
                capability='secrets:*',
                request_id=data.request_id,
                metadata={'provider': secret.provider, 'name': data.name},
            )

            return secret


class ResolveModelPolicy:
    def execute(self, data: ResolveModelPolicyInput) -> ResolvedModelPolicy:
        queryset = ModelPolicy.objects.select_related('secret', 'organization', 'team', 'project').filter(
            organization_id=data.organization_id,
            task_type=data.task_type,
            active=True,
            secret__active=True,
        )
        project_queryset = queryset.filter(project_id=data.project_id)
        if data.team_id is not None:
            project_queryset = project_queryset.filter(Q(team_id=data.team_id) | Q(team__isnull=True))
        else:
            project_queryset = project_queryset.filter(team__isnull=True)
        policy = project_queryset.order_by('-updated_at', '-created_at').first()
        if policy is None and data.team_id is not None:
            policy = (
                queryset.filter(scope=PolicyScope.TEAM, team_id=data.team_id)
                .order_by(
                    '-updated_at',
                    '-created_at',
                )
                .first()
            )
        if policy is None:
            policy = (
                queryset.filter(scope=PolicyScope.ORGANIZATION, team__isnull=True, project__isnull=True)
                .order_by(
                    '-updated_at',
                    '-created_at',
                )
                .first()
            )
        if policy is None:
            raise ModelPolicyError('model_policy_not_found', 'Model policy was not found')

        return ResolvedModelPolicy(policy=policy)


def _log_repeat_attempt(data: ProviderCallInput | EmbeddingCallInput) -> None:
    policy = data.policy
    repeated = ProviderCallRecord.objects.filter(
        organization_id=data.organization_id,
        project_id=data.project_id,
        task_type=policy.task_type,
        request_id=data.request_id,
    ).exists()
    if not repeated:
        return

    logger.warning(
        'provider_request_repeated',
        request_id=data.request_id,
        task_type=policy.task_type,
    )


_COST_QUANTUM = Decimal('0.000001')
_ONE_MILLION = Decimal(1_000_000)


def _coerce_token_count(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)

    return None


def _parse_openai_usage(response: dict[str, Any]) -> dict[str, int] | None:
    usage = response.get('usage')
    if not isinstance(usage, dict):
        return None
    input_tokens = _coerce_token_count(usage.get('prompt_tokens'))
    output_tokens = _coerce_token_count(usage.get('completion_tokens'))
    total_tokens = _coerce_token_count(usage.get('total_tokens'))
    if input_tokens is None and output_tokens is None and total_tokens is None:
        return None
    input_tokens = input_tokens or 0
    output_tokens = output_tokens or 0
    if total_tokens is None:
        total_tokens = input_tokens + output_tokens

    return {'input_tokens': input_tokens, 'output_tokens': output_tokens, 'total_tokens': total_tokens}


def _parse_anthropic_usage(response: dict[str, Any]) -> dict[str, int] | None:
    usage = response.get('usage')
    if not isinstance(usage, dict):
        return None
    input_tokens = _coerce_token_count(usage.get('input_tokens'))
    output_tokens = _coerce_token_count(usage.get('output_tokens'))
    if input_tokens is None and output_tokens is None:
        return None
    input_tokens = input_tokens or 0
    output_tokens = output_tokens or 0

    return {'input_tokens': input_tokens, 'output_tokens': output_tokens, 'total_tokens': input_tokens + output_tokens}


def _build_token_usage(
    real_usage: dict[str, int] | None,
    input_estimate: int,
    output_estimate: int,
) -> dict[str, Any]:
    if real_usage is not None:
        return {
            'input_tokens': real_usage['input_tokens'],
            'output_tokens': real_usage['output_tokens'],
            'total_tokens': real_usage['total_tokens'],
            'source': 'provider',
        }

    return {'input_tokens': input_estimate, 'output_tokens': output_estimate, 'source': 'estimated'}


def _resolve_policy_pricing(policy: ModelPolicy) -> dict[str, Decimal] | None:
    metadata = policy.metadata if isinstance(policy.metadata, dict) else {}
    raw = metadata.get('pricing')
    if raw is None:
        return None
    if not isinstance(raw, dict):
        logger.warning('provider_pricing_malformed', policy_id=str(policy.id), reason='not_a_mapping')

        return None
    pricing: dict[str, Decimal] = {}
    for key in ('input_per_mtok', 'output_per_mtok'):
        if raw.get(key) is None:
            continue
        try:
            value = Decimal(str(raw[key]))
        except (InvalidOperation, TypeError, ValueError):
            logger.warning('provider_pricing_malformed', policy_id=str(policy.id), field=key)

            return None
        if value < 0:
            logger.warning('provider_pricing_malformed', policy_id=str(policy.id), field=key, reason='negative')

            return None
        pricing[key] = value
    if 'input_per_mtok' not in pricing:
        logger.warning('provider_pricing_malformed', policy_id=str(policy.id), reason='missing_input_per_mtok')

        return None

    return pricing


def _build_cost_metadata(
    policy: ModelPolicy,
    real_usage: dict[str, int] | None,
    *,
    output_billable: bool = True,
) -> dict[str, Any]:
    pricing = _resolve_policy_pricing(policy)
    if pricing is None:
        return {'estimated': True, 'cost_usd': '0.0000', 'pricing_source': 'unknown'}
    if real_usage is None:
        return {'estimated': True, 'cost_usd': '0.0000', 'pricing_source': 'no_usage'}
    input_cost = (Decimal(real_usage['input_tokens']) / _ONE_MILLION) * pricing['input_per_mtok']
    output_cost = Decimal(0)
    if output_billable:
        output_cost = (Decimal(real_usage['output_tokens']) / _ONE_MILLION) * pricing.get('output_per_mtok', Decimal(0))
    total = (input_cost + output_cost).quantize(_COST_QUANTUM)

    return {'estimated': False, 'cost_usd': str(total), 'pricing_source': 'policy'}


def _apply_fake_provider_delay() -> None:
    raw_delay = os.environ.get('ENGRAM_FAKE_PROVIDER_DELAY_MS', '0')
    try:
        delay_ms = int(raw_delay)
    except ValueError as error:
        raise ImproperlyConfigured('ENGRAM_FAKE_PROVIDER_DELAY_MS must be an integer from 0 to 5000') from error
    if not 0 <= delay_ms <= 5000:
        raise ImproperlyConfigured('ENGRAM_FAKE_PROVIDER_DELAY_MS must be an integer from 0 to 5000')
    if delay_ms:
        time.sleep(delay_ms / 1000)


class FakeProviderGateway:
    def call(self, data: ProviderCallInput) -> ProviderCallResult:
        policy = data.policy
        secret = policy.secret
        if not secret.active:
            raise ProviderSecretError('provider secret is disabled')
        if not ProviderSecretEnvelope.objects.filter(secret=secret, active=True).exists():
            raise ProviderSecretError('provider secret has no active envelope')

        _apply_fake_provider_delay()
        _log_repeat_attempt(data)

        redacted_prompt = redact_value(data.prompt)
        generated_title, generated_body = fake_generated_content(data, str(redacted_prompt.value))
        prompt_was_redacted = redacted_prompt.redacted or '[REDACTED]' in data.prompt
        token_count = len(data.prompt.split())
        record = ProviderCallRecord.objects.create(
            organization_id=data.organization_id,
            project_id=data.project_id,
            team_id=data.team_id,
            policy=policy,
            secret=secret,
            provider=policy.provider,
            model=policy.model,
            task_type=policy.task_type,
            policy_version=policy.version,
            request_id=data.request_id,
            trace_id=data.trace_id,
            redaction_state='redacted' if prompt_was_redacted else 'clean',
            token_usage=_build_token_usage(None, token_count, 0),
            latency_ms=0,
            cost_metadata=_build_cost_metadata(policy, None),
            result=AuditResult.RECORDED,
            metadata={'prompt_retained': False},
        )

        return ProviderCallResult(
            provider=policy.provider,
            model=policy.model,
            call_record_id=record.id,
            redaction_state=record.redaction_state,
            generated_title=generated_title,
            generated_body=generated_body,
        )

    def embed(self, data: EmbeddingCallInput) -> EmbeddingCallResult:
        policy = data.policy
        secret = policy.secret
        if not secret.active:
            raise ProviderSecretError('provider secret is disabled')
        if not ProviderSecretEnvelope.objects.filter(secret=secret, active=True).exists():
            raise ProviderSecretError('provider secret has no active envelope')

        _apply_fake_provider_delay()
        _log_repeat_attempt(data)

        redacted_text = redact_value(data.text)
        embedding = tuple(generated_embedding(str(redacted_text.value)))
        text_was_redacted = redacted_text.redacted or '[REDACTED]' in data.text
        token_count = len(_embedding_grams(str(redacted_text.value)))
        record = ProviderCallRecord.objects.create(
            organization_id=data.organization_id,
            project_id=data.project_id,
            team_id=data.team_id,
            policy=policy,
            secret=secret,
            provider=policy.provider,
            model=policy.model,
            task_type=policy.task_type,
            policy_version=policy.version,
            request_id=data.request_id,
            trace_id=data.trace_id,
            redaction_state='redacted' if text_was_redacted else 'clean',
            token_usage=_build_token_usage(None, token_count, 0),
            latency_ms=0,
            cost_metadata=_build_cost_metadata(policy, None, output_billable=False),
            result=AuditResult.RECORDED,
            metadata={'prompt_retained': False},
        )

        return EmbeddingCallResult(
            provider=policy.provider,
            model=policy.model,
            call_record_id=record.id,
            redaction_state=record.redaction_state,
            embedding=embedding,
        )


def generated_candidate_content(prompt: str) -> tuple[str, str]:
    digest = hashlib.sha256(prompt.encode()).hexdigest()[:12]

    return f'Provider-generated memory {digest}', f'Provider-generated candidate body {digest}'


def generated_candidates_payload(prompt: str) -> str:
    digest = hashlib.sha256(prompt.encode()).hexdigest()[:12]
    candidates = [
        {
            'title': f'Provider-synthesized memory {digest} high',
            'body': f'Provider-synthesized candidate body {digest} high',
            'confidence': 0.9,
            'supporting_observation_ids': [],
            'source_ids': [0],
            'kind': 'gotcha',
        },
        {
            'title': f'Provider-synthesized memory {digest} low',
            'body': f'Provider-synthesized candidate body {digest} low',
            'confidence': 0.4,
            'supporting_observation_ids': [],
            'source_ids': [1],
        },
    ]

    return json.dumps({'memories': candidates})


def generated_curation_judgment_payload() -> str:
    return json.dumps({'decision': 'keep_both', 'reason': 'fake provider default judgment'})


def generated_distill_extract_payload(prompt: str) -> str:
    observation_ids: list[str] = []
    seen_ids: set[str] = set()
    for line in prompt.splitlines():
        if not line.startswith('Observation: '):
            continue
        observation_id = line.removeprefix('Observation: ')
        if (
            re.fullmatch(
                r'[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}',
                observation_id,
            )
            is None
        ):
            continue
        canonical_id = observation_id.lower()
        if canonical_id in seen_ids:
            continue
        seen_ids.add(canonical_id)
        observation_ids.append(canonical_id)

    if not observation_ids:
        return json.dumps({'memories': [], 'no_signal_observation_ids': []})

    digest = hashlib.sha256(prompt.encode()).hexdigest()[:12]
    return json.dumps(
        {
            'memories': [
                {
                    'title': f'Provider-distilled memory {digest}',
                    'body': f'Provider-distilled memory body {digest}',
                    'confidence': 0.9,
                    'supporting_observation_ids': observation_ids,
                    'kind': 'gotcha',
                },
            ],
            'no_signal_observation_ids': [],
        },
    )


def generated_distill_reduce_payload(prompt: str) -> str:
    try:
        parsed = json.loads(prompt)
    except (json.JSONDecodeError, TypeError):
        parsed = None

    source_ids: list[str] = []
    seen_ids: set[str] = set()
    drafts = parsed.get('drafts') if isinstance(parsed, dict) else None
    if isinstance(drafts, list):
        for draft in drafts:
            if not isinstance(draft, dict):
                continue
            source_id = draft.get('id')
            if not isinstance(source_id, str) or not source_id or source_id in seen_ids:
                continue
            seen_ids.add(source_id)
            source_ids.append(source_id)

    if not source_ids:
        return json.dumps({'memories': []})

    digest = hashlib.sha256(prompt.encode()).hexdigest()[:12]
    return json.dumps(
        {
            'memories': [
                {
                    'title': f'Provider-reduced memory {digest}',
                    'body': f'Provider-reduced memory body {digest}',
                    'confidence': 0.9,
                    'source_ids': source_ids,
                    'kind': 'gotcha',
                },
            ],
        },
    )


def generated_curation_decision_payload(prompt: str) -> str:
    try:
        envelope = json.loads(prompt)
    except (json.JSONDecodeError, TypeError):
        envelope = {}
    candidate = envelope.get('candidate') if isinstance(envelope, dict) else {}
    candidate_refs = candidate.get('evidence_refs') if isinstance(candidate, dict) else []
    if not isinstance(candidate_refs, list):
        candidate_refs = []
    raw_comparisons = envelope.get('comparisons') if isinstance(envelope, dict) else []
    comparisons = []
    for item in raw_comparisons if isinstance(raw_comparisons, list) else []:
        if not isinstance(item, dict):
            continue
        target_refs = item.get('evidence_refs')
        comparisons.append(
            {
                'memory_version_id': item.get('memory_version_id'),
                'relation': 'unrelated',
                'target_evidence_refs': target_refs if isinstance(target_refs, list) else [],
            }
        )

    return json.dumps(
        {
            'schema_version': 1,
            'outcome': 'publish_new',
            'relation': 'unrelated',
            'target_memory_version_id': None,
            'candidate_evidence_refs': candidate_refs,
            'comparisons': comparisons,
            'applicability': 'same',
            'temporal_order': 'not_applicable',
            'reason_code': 'distinct_claim',
            'reason': 'fake provider curation decision',
        }
    )


def fake_generated_content(data: ProviderCallInput, prompt: str) -> tuple[str, str]:
    title, body = generated_candidate_content(prompt)
    if data.response_kind == 'candidates':
        return title, generated_candidates_payload(prompt)
    if data.response_kind == 'curation_judgment':
        return title, generated_curation_judgment_payload()
    if data.response_kind == 'distill_extract.v1':
        return title, generated_distill_extract_payload(prompt)
    if data.response_kind == 'distill_reduce.v1':
        return title, generated_distill_reduce_payload(prompt)
    if data.response_kind == 'curation_decision_v1':
        return title, generated_curation_decision_payload(prompt)

    return title, body


def decrypt_secret(envelope: ProviderSecretEnvelope) -> str:
    return Fernet(encryption_key()).decrypt(envelope.ciphertext.encode()).decode()


def default_base_url(provider: str) -> str:
    if provider == 'deepseek':
        return 'https://api.deepseek.com/v1'

    if provider == 'anthropic':
        return 'https://api.anthropic.com'

    return 'https://api.openai.com/v1'


def deepseek_thinking_override(provider: str, task_type: str, response_kind: str) -> dict[str, object]:
    if provider == 'deepseek' and response_kind == 'curation_decision_v1':
        return {'thinking': {'type': 'enabled'}}

    if provider == 'deepseek' and task_type in ('curation', 'digest'):
        return {'thinking': {'type': 'disabled'}}

    return {}


_STRUCTURED_RESPONSE_KINDS = frozenset(
    {'candidates', 'curation_judgment', 'distill_extract.v1', 'distill_reduce.v1', 'curation_decision_v1'}
)
_JSON_OBJECT_DEFAULT_BY_PROVIDER = {'openai': True, 'deepseek': False}


def policy_supports_json_object(policy: ModelPolicy) -> bool:
    metadata = policy.metadata if isinstance(policy.metadata, dict) else {}
    override = metadata.get('json_mode')
    if isinstance(override, bool):
        return override

    return _JSON_OBJECT_DEFAULT_BY_PROVIDER.get(policy.provider, False)


def openai_json_mode_override(response_kind: str, policy: ModelPolicy) -> dict[str, object]:
    if response_kind in _STRUCTURED_RESPONSE_KINDS and policy_supports_json_object(policy):
        return {'response_format': {'type': 'json_object'}}

    return {}


_DEFAULT_MAX_TOKENS = 1024
_MAX_TOKENS_BY_KIND = {
    'candidates': 8192,
    'curation_judgment': 1024,
    'distill_extract.v1': 8192,
    'distill_reduce.v1': 8192,
    'curation_decision_v1': 4096,
}
_FIXED_MAX_TOKEN_KINDS = frozenset({'distill_extract.v1', 'distill_reduce.v1', 'curation_decision_v1'})
_CURATION_DECISION_OUTCOMES = (
    'publish_new',
    'merge_evidence',
    'revise_memory',
    'supersede_memory',
    'reject_candidate',
    'open_conflict',
)
_CURATION_DECISION_RELATIONS = (
    'unrelated',
    'compatible_distinct',
    'equivalent',
    'candidate_revises',
    'candidate_supersedes',
    'redundant',
    'unsupported',
    'mutually_incompatible',
)
_CURATION_DECISION_REASON_CODES = (
    'distinct_claim',
    'equivalent_claim',
    'same_subject_revision',
    'ordered_replacement',
    'redundant_claim',
    'unsupported_claim',
    'same_scope_contradiction',
)
_CURATION_DECISION_TEMPORAL_ORDERS = ('candidate_newer', 'target_newer', 'unordered', 'not_applicable')
_CURATION_DECISION_SCHEMA_INSTRUCTIONS = (
    'Return exactly one JSON object and nothing else: no prose, no markdown code fences. '
    'The object must contain exactly these keys and no additional properties (recursively): '
    'schema_version (integer, always 1); '
    f'outcome (one of: {", ".join(_CURATION_DECISION_OUTCOMES)}); '
    f'relation (one of: {", ".join(_CURATION_DECISION_RELATIONS)}); '
    'target_memory_version_id (a shortlist memory_version_id string, or null); '
    'candidate_evidence_refs (array of provided evidence reference tokens, unique, at most 16); '
    'comparisons (array with exactly one object per shortlist entry, in the given order; each object has '
    'memory_version_id, relation, and target_evidence_refs); '
    'applicability (one of: same, different); '
    f'temporal_order (one of: {", ".join(_CURATION_DECISION_TEMPORAL_ORDERS)}); '
    f'reason_code (one of: {", ".join(_CURATION_DECISION_REASON_CODES)}); '
    'reason (a short redacted explanation, at most 500 characters). '
    'Only reference memory_version_id values and evidence tokens present in the input. '
    'A non-null target_memory_version_id must be one of the shortlist entries, and its comparison relation '
    'must equal the top-level relation.'
    ' Allowed outcome and relation combinations (any other combination is invalid): '
    'publish_new with relation unrelated or compatible_distinct, target_memory_version_id null, '
    'candidate evidence tier supported or corroborated and comparison_complete true; '
    'merge_evidence with relation equivalent, a non-null target and both candidate and target evidence tiers '
    'supported or corroborated; '
    'revise_memory with relation candidate_revises, a non-null target, candidate evidence tier corroborated, '
    'target evidence tier supported or corroborated and temporal_order candidate_newer used only when the '
    'candidate evidence is clearly newer, which the system verifies; '
    'supersede_memory with relation candidate_supersedes, a non-null target, candidate evidence tier '
    'corroborated, target evidence tier supported or corroborated, temporal_order candidate_newer used only '
    'when the candidate evidence is clearly newer, which the system verifies, and comparison_complete true; '
    'reject_candidate with relation redundant, a non-null target and target evidence tier supported or '
    'corroborated; '
    'reject_candidate with relation unsupported, target null and the candidate having no supporting evidence, '
    'evidence tier none; '
    'open_conflict with relation mutually_incompatible, a non-null target, temporal_order unordered, both '
    'candidate and target evidence tiers supported or corroborated, non-empty evidence refs on both sides, '
    'comparison_complete true and applicability same. '
    'The top-level relation describes the selected target; with a null target use unrelated, '
    'compatible_distinct or unsupported. '
    'merge_evidence, revise_memory and supersede_memory additionally require applicability same. '
    'When comparison_complete is true, every shortlist comparison is unrelated or compatible_distinct and the '
    'candidate evidence tier is supported or corroborated, choose publish_new with target null. '
    'When no combination satisfies its requirements, choose the reject_candidate form that matches the '
    'candidate evidence.'
)


_MEMORY_KIND_VALUES = ('decision', 'convention', 'gotcha', 'architecture', 'incident')
_DISTILL_EXTRACT_SCHEMA_INSTRUCTIONS = (
    'Return exactly one JSON object and nothing else: no prose, no markdown code fences. '
    'The object must contain exactly these keys and no additional properties: '
    'memories (array of at most 12 objects); '
    'no_signal_observation_ids (array of observation ids, unique, may be empty). '
    'Each memories entry must contain exactly these keys and no additional properties: '
    'title (non-blank string, at most 255 characters); '
    'body (non-blank string, at most 3000 characters); '
    'confidence (a JSON number between 0 and 1, never a string); '
    'supporting_observation_ids (non-empty array of unique observation ids); '
    f'kind (optional, one of: {", ".join(_MEMORY_KIND_VALUES)}). '
    'Only use observation ids copied verbatim from the input observations. '
    'Every input observation id must appear at least once across the memories supporting_observation_ids '
    'and no_signal_observation_ids: none may be omitted, and no id may appear in both. '
    'The same observation id may support more than one memory.'
)
_DISTILL_REDUCE_SCHEMA_INSTRUCTIONS = (
    'Return exactly one JSON object and nothing else: no prose, no markdown code fences. '
    'The object must contain exactly the key memories (array of objects) and no additional properties. '
    'Each memories entry must contain exactly these keys and no additional properties: '
    'title (non-blank string, at most 255 characters); '
    'body (non-blank string, at most 3000 characters); '
    'confidence (a JSON number between 0 and 1); '
    'source_ids (non-empty array of unique draft ids); '
    f'kind (optional, one of: {", ".join(_MEMORY_KIND_VALUES)}). '
    'Only use draft ids copied verbatim from the input drafts. '
    'Every input draft id must appear in the source_ids of at least one memories entry: none may be omitted. '
    'Return at most reduction_target memories, as given by the reduction_target key of the input object, '
    'and when more than one draft is given return strictly fewer memories than the number of input drafts.'
)


def curation_schema_prompt_prefix(response_kind: str) -> str:
    if response_kind == 'curation_decision_v1':
        return _CURATION_DECISION_SCHEMA_INSTRUCTIONS

    if response_kind == 'distill_extract.v1':
        return _DISTILL_EXTRACT_SCHEMA_INSTRUCTIONS

    if response_kind == 'distill_reduce.v1':
        return _DISTILL_REDUCE_SCHEMA_INSTRUCTIONS

    return ''


_ANTHROPIC_STRUCTURED_TOOLS: dict[str, dict[str, object]] = {
    'candidates': {
        'name': 'emit_memories',
        'description': 'Return the synthesized engineering memories.',
        'input_schema': {
            'type': 'object',
            'properties': {
                'memories': {
                    'type': 'array',
                    'items': {
                        'type': 'object',
                        'properties': {
                            'title': {'type': 'string'},
                            'body': {'type': 'string'},
                            'confidence': {'type': 'number'},
                            'supporting_observation_ids': {'type': 'array', 'items': {'type': 'string'}},
                            'source_ids': {'type': 'array', 'items': {'type': 'integer'}},
                            'kind': {
                                'type': 'string',
                                'enum': ['decision', 'convention', 'gotcha', 'architecture', 'incident'],
                            },
                        },
                        'required': ['title', 'body', 'confidence'],
                    },
                },
            },
            'required': ['memories'],
        },
    },
    'curation_judgment': {
        'name': 'emit_judgment',
        'description': 'Return the curation judgment.',
        'input_schema': {
            'type': 'object',
            'properties': {
                'decision': {'type': 'string', 'enum': ['merge', 'keep_both', 'reject', 'contradicts']},
                'reason': {'type': 'string'},
            },
            'required': ['decision', 'reason'],
        },
    },
    'distill_extract.v1': {
        'name': 'emit_distillation_extraction',
        'description': 'Return the distillation extraction.',
        'input_schema': {
            'type': 'object',
            'properties': {
                'memories': {
                    'type': 'array',
                    'maxItems': 12,
                    'items': {
                        'type': 'object',
                        'properties': {
                            'title': {'type': 'string', 'minLength': 1, 'maxLength': 255},
                            'body': {'type': 'string', 'minLength': 1, 'maxLength': 3000},
                            'confidence': {'type': 'number', 'minimum': 0, 'maximum': 1},
                            'supporting_observation_ids': {
                                'type': 'array',
                                'items': {'type': 'string'},
                                'minItems': 1,
                                'uniqueItems': True,
                            },
                            'kind': {
                                'type': 'string',
                                'enum': ['decision', 'convention', 'gotcha', 'architecture', 'incident'],
                            },
                        },
                        'required': ['title', 'body', 'confidence', 'supporting_observation_ids'],
                        'additionalProperties': False,
                    },
                },
                'no_signal_observation_ids': {
                    'type': 'array',
                    'items': {'type': 'string'},
                    'uniqueItems': True,
                },
            },
            'required': ['memories', 'no_signal_observation_ids'],
            'additionalProperties': False,
        },
    },
    'distill_reduce.v1': {
        'name': 'emit_distillation_reduction',
        'description': 'Return the distillation reduction.',
        'input_schema': {
            'type': 'object',
            'properties': {
                'memories': {
                    'type': 'array',
                    'maxItems': 12,
                    'items': {
                        'type': 'object',
                        'properties': {
                            'title': {'type': 'string', 'minLength': 1, 'maxLength': 255},
                            'body': {'type': 'string', 'maxLength': 3000},
                            'confidence': {'type': 'number', 'minimum': 0, 'maximum': 1},
                            'source_ids': {
                                'type': 'array',
                                'items': {'type': 'string'},
                                'minItems': 1,
                                'uniqueItems': True,
                            },
                            'kind': {
                                'type': 'string',
                                'enum': ['decision', 'convention', 'gotcha', 'architecture', 'incident'],
                            },
                        },
                        'required': ['title', 'body', 'confidence', 'source_ids'],
                        'additionalProperties': False,
                    },
                },
            },
            'required': ['memories'],
            'additionalProperties': False,
        },
    },
    'curation_decision_v1': {
        'name': 'emit_curation_decision',
        'description': 'Return the strict curation decision verdict.',
        'input_schema': {
            'type': 'object',
            'additionalProperties': False,
            'properties': {
                'schema_version': {'type': 'integer', 'enum': [1]},
                'outcome': {'type': 'string', 'enum': list(_CURATION_DECISION_OUTCOMES)},
                'relation': {'type': 'string', 'enum': list(_CURATION_DECISION_RELATIONS)},
                'target_memory_version_id': {'type': ['string', 'null']},
                'candidate_evidence_refs': {
                    'type': 'array',
                    'items': {'type': 'string'},
                    'maxItems': 16,
                    'uniqueItems': True,
                },
                'comparisons': {
                    'type': 'array',
                    'maxItems': 12,
                    'items': {
                        'type': 'object',
                        'additionalProperties': False,
                        'properties': {
                            'memory_version_id': {'type': 'string'},
                            'relation': {'type': 'string', 'enum': list(_CURATION_DECISION_RELATIONS)},
                            'target_evidence_refs': {
                                'type': 'array',
                                'items': {'type': 'string'},
                                'maxItems': 16,
                                'uniqueItems': True,
                            },
                        },
                        'required': ['memory_version_id', 'relation', 'target_evidence_refs'],
                    },
                },
                'applicability': {'type': 'string', 'enum': ['same', 'different']},
                'temporal_order': {'type': 'string', 'enum': list(_CURATION_DECISION_TEMPORAL_ORDERS)},
                'reason_code': {'type': 'string', 'enum': list(_CURATION_DECISION_REASON_CODES)},
                'reason': {'type': 'string', 'maxLength': 500},
            },
            'required': [
                'schema_version',
                'outcome',
                'relation',
                'target_memory_version_id',
                'candidate_evidence_refs',
                'comparisons',
                'applicability',
                'temporal_order',
                'reason_code',
                'reason',
            ],
        },
    },
}


def resolve_max_tokens(policy: ModelPolicy, response_kind: str) -> int:
    if response_kind in _FIXED_MAX_TOKEN_KINDS:
        return _MAX_TOKENS_BY_KIND[response_kind]

    metadata = policy.metadata if isinstance(policy.metadata, dict) else {}
    raw = metadata.get('max_tokens')
    if isinstance(raw, bool):
        raw = None
    try:
        override = int(raw)
    except (TypeError, ValueError):
        override = 0
    if override > 0:
        return override

    return _MAX_TOKENS_BY_KIND.get(response_kind, _DEFAULT_MAX_TOKENS)


def resolve_context_window_tokens(policy: ModelPolicy) -> int | None:
    metadata = policy.metadata if isinstance(policy.metadata, dict) else {}
    override = metadata.get('context_window_tokens')
    if isinstance(override, int) and override > 0:
        return override

    model = policy.model.lower()
    best_match: str | None = None
    for prefix in _MODEL_PREFIX_CONTEXT_WINDOWS:
        if model.startswith(prefix) and (best_match is None or len(prefix) > len(best_match)):
            best_match = prefix

    if best_match is not None:
        return _MODEL_PREFIX_CONTEXT_WINDOWS[best_match]

    return None


def _resolve_base_url(policy: ModelPolicy) -> str:
    metadata = policy.metadata if isinstance(policy.metadata, dict) else {}
    base_url = str(metadata.get('base_url') or '').strip()

    return base_url if base_url else default_base_url(policy.provider)


def _recheck_custom_base_url(policy: ModelPolicy, base_url: str) -> None:
    metadata = policy.metadata if isinstance(policy.metadata, dict) else {}
    if not str(metadata.get('base_url') or '').strip():
        return

    try:
        validate_base_url(base_url)
    except BaseUrlValidationError as error:
        raise ModelPolicyError('provider_base_url_invalid', error.public_message, retryable=False) from error


def provider_http_timeout() -> int:
    return int(os.environ.get('ENGRAM_PROVIDER_HTTP_TIMEOUT', '60'))


def _embedding_http_timeout() -> int:
    return int(os.environ.get('ENGRAM_EMBEDDING_HTTP_TIMEOUT', '30'))


_NO_PROMPT_REDACTION_STATE = 'not_applicable'


def _elapsed_ms(started_at: float) -> int:
    return int((time.monotonic() - started_at) * 1000)


class OpenAICompatibleGateway:
    def __init__(self, base_url: str, api_key: str, *, opener: Any = None, timeout: int | None = None) -> None:
        self._base_url = base_url.rstrip('/')
        self._api_key = api_key
        self._opener = opener or urllib.request.urlopen
        self._timeout = timeout if timeout is not None else provider_http_timeout()

    def call(self, data: ProviderCallInput) -> ProviderCallResult:
        policy = data.policy
        _log_repeat_attempt(data)
        redacted_prompt = redact_value(data.prompt)
        prompt_text = str(redacted_prompt.value)
        schema_prefix = curation_schema_prompt_prefix(data.response_kind)
        if schema_prefix:
            prompt_text = f'{schema_prefix}\n\n{prompt_text}'

        extra: dict[str, object] = {}
        extra.update(deepseek_thinking_override(policy.provider, policy.task_type, data.response_kind))
        extra.update(openai_json_mode_override(data.response_kind, policy))
        extra['max_tokens'] = resolve_max_tokens(policy, data.response_kind)
        started_at = time.monotonic()
        try:
            content, real_usage = self._chat_completion(
                policy.model,
                prompt_text,
                system_prompt=data.system_prompt,
                extra=extra,
            )
        except ModelPolicyError as error:
            self._record_error(data, policy, error, latency_ms=_elapsed_ms(started_at))
            raise

        latency_ms = _elapsed_ms(started_at)
        title = _completion_title(content, data.response_kind)
        body = _completion_body(content, data.response_kind)
        record = self._record_call(
            data,
            policy,
            redaction_state='redacted' if redacted_prompt.redacted or '[REDACTED]' in data.prompt else 'clean',
            token_usage=_build_token_usage(real_usage, len(prompt_text.split()), len(content.split())),
            cost_metadata=_build_cost_metadata(policy, real_usage),
            latency_ms=latency_ms,
        )

        return ProviderCallResult(
            provider=policy.provider,
            model=policy.model,
            call_record_id=record.id,
            redaction_state=record.redaction_state,
            generated_title=title,
            generated_body=body,
        )

    def embed(self, data: EmbeddingCallInput) -> EmbeddingCallResult:
        policy = data.policy
        _log_repeat_attempt(data)
        redacted_text = redact_value(data.text)
        text_value = str(redacted_text.value)

        started_at = time.monotonic()
        try:
            embedding, real_usage = self._embeddings(policy.model, text_value)
        except ModelPolicyError as error:
            self._record_error(data, policy, error, latency_ms=_elapsed_ms(started_at))
            raise

        latency_ms = _elapsed_ms(started_at)
        record = self._record_call(
            data,
            policy,
            redaction_state='redacted' if redacted_text.redacted or '[REDACTED]' in data.text else 'clean',
            token_usage=_build_token_usage(real_usage, len(text_value.split()), 0),
            cost_metadata=_build_cost_metadata(policy, real_usage, output_billable=False),
            latency_ms=latency_ms,
        )

        return EmbeddingCallResult(
            provider=policy.provider,
            model=policy.model,
            call_record_id=record.id,
            redaction_state=record.redaction_state,
            embedding=embedding,
        )

    def _record_call(
        self,
        data: ProviderCallInput | EmbeddingCallInput,
        policy: ModelPolicy,
        *,
        redaction_state: str,
        token_usage: dict[str, Any],
        cost_metadata: dict[str, Any],
        latency_ms: int,
    ) -> ProviderCallRecord:
        return ProviderCallRecord.objects.create(
            organization_id=data.organization_id,
            project_id=data.project_id,
            team_id=data.team_id,
            policy=policy,
            secret=policy.secret,
            provider=policy.provider,
            model=policy.model,
            task_type=policy.task_type,
            policy_version=policy.version,
            request_id=data.request_id,
            trace_id=data.trace_id,
            redaction_state=redaction_state,
            token_usage=token_usage,
            latency_ms=latency_ms,
            cost_metadata=cost_metadata,
            result=AuditResult.RECORDED,
            metadata={'prompt_retained': False, 'transport': 'http'},
        )

    def _record_error(
        self,
        data: ProviderCallInput | EmbeddingCallInput,
        policy: ModelPolicy,
        error: ModelPolicyError,
        *,
        latency_ms: int,
    ) -> ProviderCallRecord:
        return ProviderCallRecord.objects.create(
            organization_id=data.organization_id,
            project_id=data.project_id,
            team_id=data.team_id,
            policy=policy,
            secret=policy.secret,
            provider=policy.provider,
            model=policy.model,
            task_type=policy.task_type,
            policy_version=policy.version,
            request_id=data.request_id,
            trace_id=data.trace_id,
            redaction_state=_NO_PROMPT_REDACTION_STATE,
            token_usage={},
            latency_ms=latency_ms,
            cost_metadata={},
            result=AuditResult.ERROR,
            metadata={
                'error_code': error.code,
                'http_status': error.http_status,
                'prompt_retained': False,
                'transport': 'http',
            },
        )

    def _chat_completion(
        self,
        model: str,
        prompt: str,
        system_prompt: str = '',
        extra: dict[str, object] | None = None,
    ) -> tuple[str, dict[str, int] | None]:
        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({'role': 'system', 'content': system_prompt})
        messages.append({'role': 'user', 'content': prompt})
        payload_dict: dict[str, object] = {
            'model': model,
            'messages': messages,
            'temperature': 0.2,
        }
        if extra:
            payload_dict.update(extra)
        payload = json.dumps(payload_dict).encode()
        response = self._open(self._base_url + '/chat/completions', payload, timeout=self._timeout)

        return str(response['choices'][0]['message']['content']), _parse_openai_usage(response)

    def _embeddings(self, model: str, text: str) -> tuple[tuple[float, ...], dict[str, int] | None]:
        payload = json.dumps({'model': model, 'input': text}).encode()
        response = self._open(self._base_url + '/embeddings', payload, timeout=_embedding_http_timeout())
        embedding = tuple(float(component) for component in response['data'][0]['embedding'])

        return fit_embedding_dimension(embedding), _parse_openai_usage(response)

    def _open(self, url: str, body: bytes, timeout: int) -> dict[str, Any]:
        request = urllib.request.Request(  # noqa: S310 - url built from operator-configured base_url
            url,
            data=body,
            headers={'Authorization': f'Bearer {self._api_key}', 'Content-Type': 'application/json'},
            method='POST',
        )
        try:
            with self._opener(request, timeout=timeout) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as error:
            retryable = error.code == 429 or error.code >= 500
            raise ModelPolicyError(
                'provider_http_error',
                f'provider returned {error.code}',
                retryable=retryable,
                http_status=error.code,
            ) from error
        except TimeoutError as error:
            raise ModelPolicyError(
                'provider_timeout',
                'provider timed out',
                retryable=True,
            ) from error
        except urllib.error.URLError as error:
            if isinstance(error.reason, TimeoutError):
                raise ModelPolicyError(
                    'provider_timeout',
                    'provider timed out',
                    retryable=True,
                ) from error

            raise ModelPolicyError(
                'provider_unreachable',
                f'provider unreachable: {error.reason}',
                retryable=True,
            ) from error


_TITLE_LABEL_RE = re.compile(r'^\s*title\s*:\s*', re.IGNORECASE)
_BODY_LABEL_RE = re.compile(r'^\s*body\s*:\s*', re.IGNORECASE)


def _strip_label(text: str, pattern: re.Pattern[str]) -> str:
    return pattern.sub('', text, count=1)


def _split_completion(content: str) -> tuple[str, str]:
    lines = [line for line in content.splitlines() if line.strip()]
    if not lines:
        return 'Provider-generated memory', content

    title = _strip_label(lines[0], _TITLE_LABEL_RE)[:255]
    if len(lines) == 1:
        body = _strip_label(_strip_label(content, _TITLE_LABEL_RE), _BODY_LABEL_RE)

        return title, body

    body = _strip_label('\n'.join(lines[1:]), _BODY_LABEL_RE)

    return title, body


def _anthropic_content_text(response: dict[str, Any]) -> str:
    blocks = response.get('content') or []
    for block in blocks:
        if isinstance(block, dict) and block.get('type') == 'tool_use':
            return json.dumps(block.get('input') or {})
    for block in blocks:
        if isinstance(block, dict) and block.get('type') == 'text':
            return str(block.get('text') or '')

    return str(blocks[0].get('text') or '') if blocks else ''


def _completion_body(content: str, response_kind: str) -> str:
    if response_kind in _STRUCTURED_RESPONSE_KINDS:
        return content

    return _split_completion(content)[1]


def _completion_title(content: str, response_kind: str) -> str:
    if response_kind in _STRUCTURED_RESPONSE_KINDS:
        return ''

    return _split_completion(content)[0]


class AnthropicMessagesGateway:
    def __init__(self, base_url: str, api_key: str, *, opener: Any = None, timeout: int | None = None) -> None:
        self._base_url = base_url.rstrip('/')
        self._api_key = api_key
        self._opener = opener or urllib.request.urlopen
        self._timeout = timeout if timeout is not None else provider_http_timeout()

    def call(self, data: ProviderCallInput) -> ProviderCallResult:
        policy = data.policy
        _log_repeat_attempt(data)
        redacted_prompt = redact_value(data.prompt)
        prompt_text = str(redacted_prompt.value)

        started_at = time.monotonic()
        try:
            content, real_usage = self._messages(
                policy.model,
                prompt_text,
                system_prompt=data.system_prompt,
                response_kind=data.response_kind,
                max_tokens=resolve_max_tokens(policy, data.response_kind),
            )
        except ModelPolicyError as error:
            self._record_error(data, policy, error, latency_ms=_elapsed_ms(started_at))
            raise

        latency_ms = _elapsed_ms(started_at)
        title = _completion_title(content, data.response_kind)
        body = _completion_body(content, data.response_kind)
        record = self._record_call(
            data,
            policy,
            redaction_state='redacted' if redacted_prompt.redacted or '[REDACTED]' in data.prompt else 'clean',
            token_usage=_build_token_usage(real_usage, len(prompt_text.split()), len(content.split())),
            cost_metadata=_build_cost_metadata(policy, real_usage),
            latency_ms=latency_ms,
        )

        return ProviderCallResult(
            provider=policy.provider,
            model=policy.model,
            call_record_id=record.id,
            redaction_state=record.redaction_state,
            generated_title=title,
            generated_body=body,
        )

    def embed(self, data: EmbeddingCallInput) -> EmbeddingCallResult:
        raise ModelPolicyError(
            'anthropic_embeddings_unsupported',
            'Anthropic-compatible providers do not expose embeddings through this gateway',
        )

    def _record_call(
        self,
        data: ProviderCallInput | EmbeddingCallInput,
        policy: ModelPolicy,
        *,
        redaction_state: str,
        token_usage: dict[str, Any],
        cost_metadata: dict[str, Any],
        latency_ms: int,
    ) -> ProviderCallRecord:
        return ProviderCallRecord.objects.create(
            organization_id=data.organization_id,
            project_id=data.project_id,
            team_id=data.team_id,
            policy=policy,
            secret=policy.secret,
            provider=policy.provider,
            model=policy.model,
            task_type=policy.task_type,
            policy_version=policy.version,
            request_id=data.request_id,
            trace_id=data.trace_id,
            redaction_state=redaction_state,
            token_usage=token_usage,
            latency_ms=latency_ms,
            cost_metadata=cost_metadata,
            result=AuditResult.RECORDED,
            metadata={'prompt_retained': False, 'transport': 'http-anthropic'},
        )

    def _record_error(
        self,
        data: ProviderCallInput | EmbeddingCallInput,
        policy: ModelPolicy,
        error: ModelPolicyError,
        *,
        latency_ms: int,
    ) -> ProviderCallRecord:
        return ProviderCallRecord.objects.create(
            organization_id=data.organization_id,
            project_id=data.project_id,
            team_id=data.team_id,
            policy=policy,
            secret=policy.secret,
            provider=policy.provider,
            model=policy.model,
            task_type=policy.task_type,
            policy_version=policy.version,
            request_id=data.request_id,
            trace_id=data.trace_id,
            redaction_state=_NO_PROMPT_REDACTION_STATE,
            token_usage={},
            latency_ms=latency_ms,
            cost_metadata={},
            result=AuditResult.ERROR,
            metadata={
                'error_code': error.code,
                'http_status': error.http_status,
                'prompt_retained': False,
                'transport': 'http-anthropic',
            },
        )

    def _messages(
        self,
        model: str,
        prompt: str,
        system_prompt: str = '',
        *,
        response_kind: str = 'single',
        max_tokens: int = _DEFAULT_MAX_TOKENS,
    ) -> tuple[str, dict[str, int] | None]:
        payload_dict: dict[str, object] = {
            'model': model,
            'max_tokens': max_tokens,
            'messages': [{'role': 'user', 'content': prompt}],
        }
        if system_prompt:
            payload_dict['system'] = system_prompt
        tool = _ANTHROPIC_STRUCTURED_TOOLS.get(response_kind)
        if tool is not None:
            payload_dict['tools'] = [tool]
            payload_dict['tool_choice'] = {'type': 'tool', 'name': tool['name']}
        payload = json.dumps(payload_dict).encode()
        response = self._open(self._base_url + '/v1/messages', payload, timeout=self._timeout)

        return _anthropic_content_text(response), _parse_anthropic_usage(response)

    def _open(self, url: str, body: bytes, timeout: int) -> dict[str, Any]:
        request = urllib.request.Request(  # noqa: S310 - url built from operator-configured base_url
            url,
            data=body,
            headers={
                'x-api-key': self._api_key,
                'anthropic-version': '2023-06-01',
                'content-type': 'application/json',
            },
            method='POST',
        )
        try:
            with self._opener(request, timeout=timeout) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as error:
            retryable = error.code == 429 or error.code >= 500
            raise ModelPolicyError(
                'provider_http_error',
                f'provider returned {error.code}',
                retryable=retryable,
                http_status=error.code,
            ) from error
        except TimeoutError as error:
            raise ModelPolicyError(
                'provider_timeout',
                'provider timed out',
                retryable=True,
            ) from error
        except urllib.error.URLError as error:
            if isinstance(error.reason, TimeoutError):
                raise ModelPolicyError(
                    'provider_timeout',
                    'provider timed out',
                    retryable=True,
                ) from error

            raise ModelPolicyError(
                'provider_unreachable',
                f'provider unreachable: {error.reason}',
                retryable=True,
            ) from error


def get_provider_gateway(
    policy: ModelPolicy,
    *,
    opener: Any = None,
    timeout: int | None = None,
) -> FakeProviderGateway | OpenAICompatibleGateway | AnthropicMessagesGateway:
    mode = os.environ.get('ENGRAM_PROVIDER_MODE', 'fake')

    if mode != 'real':
        return FakeProviderGateway()

    secret = policy.secret
    if not secret.active:
        raise ProviderSecretError('provider secret is disabled')
    envelope = (
        ProviderSecretEnvelope.objects.filter(secret=secret, active=True).order_by('-version', '-created_at').first()
    )
    if envelope is None:
        raise ProviderSecretError('provider secret has no active envelope')
    base_url = _resolve_base_url(policy)
    _recheck_custom_base_url(policy, base_url)
    api_key = decrypt_secret(envelope)
    if policy.provider == Provider.ANTHROPIC:
        gateway: FakeProviderGateway | OpenAICompatibleGateway | AnthropicMessagesGateway = AnthropicMessagesGateway(
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
        )
    else:
        gateway = OpenAICompatibleGateway(base_url=base_url, api_key=api_key, timeout=timeout)
    if opener is not None:
        gateway._opener = opener

    return gateway


EMBEDDING_DIMENSION = 1536


def fit_embedding_dimension(embedding: tuple[float, ...]) -> tuple[float, ...]:
    if len(embedding) == EMBEDDING_DIMENSION:
        return embedding

    if len(embedding) < EMBEDDING_DIMENSION:
        return embedding + (0.0,) * (EMBEDDING_DIMENSION - len(embedding))

    truncated = embedding[:EMBEDDING_DIMENSION]
    norm = math.sqrt(sum(component**2 for component in truncated))
    if norm == 0.0:
        return truncated

    return tuple(component / norm for component in truncated)


@dataclass(frozen=True)
class EmbeddingCallInput:
    organization_id: uuid.UUID
    project_id: uuid.UUID
    team_id: uuid.UUID | None
    policy: ModelPolicy
    request_id: str
    trace_id: str
    text: str


@dataclass(frozen=True)
class EmbeddingCallResult:
    provider: str
    model: str
    call_record_id: uuid.UUID
    redaction_state: str
    embedding: tuple[float, ...]


def _embedding_grams(text: str) -> tuple[str, ...]:
    cleaned = re.sub(r'[^a-z0-9]+', '', text.lower())
    if len(cleaned) < 3:
        return ()

    return tuple(cleaned[i : i + 3] for i in range(len(cleaned) - 2))


def generated_embedding(text: str) -> list[float]:
    vector = [0.0] * EMBEDDING_DIMENSION
    for gram in _embedding_grams(text):
        digest = hashlib.sha256(gram.encode()).digest()
        dim_index = int.from_bytes(digest[:8], 'big') % EMBEDDING_DIMENSION
        sign = 1.0 if digest[8] % 2 == 0 else -1.0
        vector[dim_index] += sign

    norm = math.sqrt(sum(component * component for component in vector))
    if norm == 0:
        return [0.0] * EMBEDDING_DIMENSION

    return [round(component / norm, 6) for component in vector]
