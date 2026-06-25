from __future__ import annotations

import base64
import hashlib
import hmac
import math
import re
import uuid
from dataclasses import dataclass
from typing import Any

from cryptography.fernet import Fernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.db.models import Q

from engram.core.models import AuditEvent, AuditResult, Project, Team
from engram.core.redaction import redact_value
from engram.model_policy.models import (
    ModelPolicy,
    PolicyScope,
    ProviderCallRecord,
    ProviderSecret,
    ProviderSecretEnvelope,
    SecretScope,
)

SECRET_KEY_VERSION = 'v1'
NON_PRODUCTION_ENVIRONMENTS = {'dev', 'development', 'local', 'test'}


class ModelPolicyError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ProviderSecretError(Exception):
    pass


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
                metadata={'provider': data.provider, 'scope': data.scope, 'raw_secret': data.raw_secret},
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
                metadata={'provider': secret.provider, 'raw_secret': data.raw_secret},
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

        return policy


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


class FakeProviderGateway:
    def call(self, data: ProviderCallInput) -> ProviderCallResult:
        policy = data.policy
        secret = policy.secret
        if not secret.active:
            raise ProviderSecretError('provider secret is disabled')
        if not ProviderSecretEnvelope.objects.filter(secret=secret, active=True).exists():
            raise ProviderSecretError('provider secret has no active envelope')

        existing_record = (
            ProviderCallRecord.objects.filter(
                organization_id=data.organization_id,
                project_id=data.project_id,
                task_type=policy.task_type,
                request_id=data.request_id,
            )
            .order_by('created_at')
            .first()
        )
        if existing_record is not None:
            redacted_prompt = redact_value(data.prompt)
            generated_title, generated_body = generated_candidate_content(str(redacted_prompt.value))

            return ProviderCallResult(
                provider=existing_record.provider,
                model=existing_record.model,
                call_record_id=existing_record.id,
                redaction_state=existing_record.redaction_state,
                generated_title=generated_title,
                generated_body=generated_body,
            )

        redacted_prompt = redact_value(data.prompt)
        generated_title, generated_body = generated_candidate_content(str(redacted_prompt.value))
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
            token_usage={'input_tokens': token_count, 'output_tokens': 0},
            latency_ms=0,
            cost_metadata={'estimated': True, 'cost_usd': '0.0000'},
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

        existing_record = (
            ProviderCallRecord.objects.filter(
                organization_id=data.organization_id,
                project_id=data.project_id,
                task_type=policy.task_type,
                request_id=data.request_id,
            )
            .order_by('created_at')
            .first()
        )
        redacted_text = redact_value(data.text)
        embedding = tuple(generated_embedding(str(redacted_text.value)))
        if existing_record is not None:
            return EmbeddingCallResult(
                provider=existing_record.provider,
                model=existing_record.model,
                call_record_id=existing_record.id,
                redaction_state=existing_record.redaction_state,
                embedding=embedding,
            )

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
            token_usage={'input_tokens': token_count, 'output_tokens': 0},
            latency_ms=0,
            cost_metadata={'estimated': True, 'cost_usd': '0.0000'},
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


EMBEDDING_DIMENSION = 64


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
