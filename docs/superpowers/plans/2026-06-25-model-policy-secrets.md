# Model Policy Secrets Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the backend provider secret and model policy foundation required by later provider-backed memory workflows.

**Architecture:** Create `engram.model_policy` with encrypted database secret envelopes, scoped model policies, resolution services, fake provider adapters, API views, and focused tests. Use existing API-key scope resolution, audit events, and redaction tooling.

**Tech Stack:** Django, DRF, pytest, cryptography Fernet envelopes, existing Engram access/core models.

## Global Constraints

- Organization and team secrets only in this checkpoint.
- Project, team, and organization model policy scopes are allowed.
- Raw provider secrets must never appear in responses, audit metadata, provider call records, or verification docs.
- Secret API mutations require `secrets:*`.
- Model policy writes and API resolution require `model_policy:*`.
- Provider adapters are fake local adapters; no real network calls.
- Do not add frontend, MCP, semantic retrieval, or AI workflow jobs in this slice.

---

### Task 1: Model Policy Backend Foundation

**Files:**
- Create: `apps/backend/engram/model_policy/__init__.py`
- Create: `apps/backend/engram/model_policy/apps.py`
- Create: `apps/backend/engram/model_policy/models.py`
- Create: `apps/backend/engram/model_policy/migrations/0001_initial.py`
- Create: `apps/backend/engram/model_policy/serializers.py`
- Create: `apps/backend/engram/model_policy/services.py`
- Create: `apps/backend/engram/model_policy/views.py`
- Create: `apps/backend/engram/model_policy/urls.py`
- Create: `apps/backend/engram/model_policy/model_policy_tests.py`
- Modify: `apps/backend/settings/settings.py`
- Modify: `apps/backend/settings/urls.py`
- Modify: `apps/backend/pyproject.toml`
- Modify: `apps/backend/poetry.lock`
- Modify: `scripts/repository_layout.py`
- Modify: `tests/repository/test_backend_runtime_contract.py`
- Create: `docs/security/reviews/2026-06-25-model-policy-secrets.md`
- Modify: `docs/verification-matrix.md`

**Interfaces:**
- Produces: `ProviderSecret`, `ProviderSecretEnvelope`, `ModelPolicy`, `ProviderCallRecord`
- Produces: `CreateProviderSecret`, `RotateProviderSecret`, `DisableProviderSecret`, `ResolveModelPolicy`, `FakeProviderGateway`
- Produces: `/v1/model-policy/secrets`, `/v1/model-policy/secrets/<secret_id>/rotate`, `/v1/model-policy/secrets/<secret_id>/disable`, `/v1/model-policy/secrets/<secret_id>`, `/v1/model-policy/policies`, `/v1/model-policy/resolve`

- [x] Write failing API/domain tests for secret create/detail redaction and encrypted envelope storage.
- [x] Write failing tests for secret rotation and disabled-secret provider-call denial.
- [x] Write failing tests for project -> team -> organization model policy resolution and cross-scope rejection.
- [x] Write failing tests for fake provider adapter selection and provider call audit records.
- [x] Add `cryptography` dependency and lock update.
- [x] Implement models and migration.
- [x] Implement serializers, services, fake provider gateway, views, URLs, and app registration.
- [x] Add repository layout/runtime contracts for the new app and dependency.
- [x] Run focused backend tests until green.
- [x] Run adjacent access/context/memory tests.
- [x] Run repository and Compose verification gates.
- [x] Run independent security review.
- [x] Record verification evidence.
- [x] Commit with `feat: add model policy secrets foundation`.
