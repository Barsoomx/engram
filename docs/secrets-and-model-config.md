# Secrets And Model Configuration

## Goal

Each organization or team owns its model provider credentials. The server stores
or references secrets, resolves model policy, and calls providers from trusted
server-side workers. Raw provider keys must not be written into hooks, agent
prompts, logs, observations, memory content, or frontend responses.

## Secret Store

Storage mode:

- Database envelope encryption: encrypted secret payloads in PostgreSQL with
  HMAC verification, key versioning, and rotation metadata.

This mode exposes the following domain API:

- create secret reference;
- rotate secret;
- disable secret;
- audit secret reads authenticated via API key (allowed and denied); a
  session-authenticated read is audited only when denied.

## Secret Scope

V1 secrets can belong to:

- organization;
- team;

Later secrets can belong to:

- project;
- service account.

Resolution order should be explicit and visible:

```text
project override
  -> team default
    -> organization default
      -> platform fallback, only if enabled by the tenant
```

Fallback must never silently cross tenant boundaries.

## Model Policy

Model settings are configured per organization, team, and project, similar to
Sentry project settings:

- default provider;
- default model;
- task-specific models for distillation, embedding, retrieval rerank, memory
  conflict detection, and admin assistant features;
- region or deployment endpoint constraints;
- fallback behavior.

Developers should not need to understand provider wiring. They choose a project
or team context; the server resolves policy.

V1 provider support:

- Anthropic models for teams that want Claude-quality generation.
- OpenAI models for cost-aware memory generation, digesting, curation, and
  embeddings.
- DeepSeek models as an additional cost-aware provider option.
- Task-level routing so an organization can use a cheaper OpenAI or DeepSeek
  model for routine observation distillation and reserve a stronger model for
  contradiction resolution or high-impact summaries.
- Provider health and cost metadata visible to admins before they choose a
  default.

The product must support multiple generation backends. Provider selection is an
organization/team setting, and every generated memory records the provider,
model, and policy version. Cost metadata is recorded as an estimated
placeholder, not derived from actual token usage.

## Safety Requirements

- Secrets are redacted before logs, traces, observations, and audit details.
- Provider request bodies are never retained (`prompt_retained` is always
  false).
- Memory generation must reject or mask detected secrets.
- Secret reads require `secrets:read` or a server-side job capability, never
  plain `memories:read`.
- API keys cannot be used to export provider secrets.
- Rotating a secret creates an audit event.

## Simple First Version

Start with organization and team secrets, plus project-level model-policy
overrides that select existing secrets. Project and service-account owned raw
secrets are later. Avoid per-file, per-branch, or arbitrary condition
expressions until customer use proves they are needed.

See [Backend contracts](backend-contracts.md) for secret storage and envelope
encryption invariants.
