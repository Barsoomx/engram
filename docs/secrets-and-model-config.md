# Secrets And Model Configuration

## Goal

Each organization or team owns its model provider credentials. The server stores
or references secrets, resolves model policy, and calls providers from trusted
server-side workers. Raw provider keys must not be written into hooks, agent
prompts, logs, observations, memory content, or frontend responses.

## Secret Store

Support two storage modes:

- External vault adapter: HashiCorp Vault-compatible interface for customers
  that already run a secret store.
- Database envelope encryption: encrypted secret payloads in PostgreSQL with
  HMAC verification, key versioning, and rotation metadata.

Both modes expose the same domain API:

- create secret reference;
- rotate secret;
- disable secret;
- test secret with redacted result;
- audit every secret read/use;
- report which model policies depend on a secret.

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
- budget limits;
- allowed providers;
- blocked providers;
- retention and logging preferences;
- region or deployment endpoint constraints;
- fallback behavior.

Developers should not need to understand provider wiring. They choose a project
or team context; the server resolves policy.

## Safety Requirements

- Secrets are redacted before logs, traces, observations, and audit details.
- Provider request bodies are classified before optional retention.
- Memory generation must reject or mask detected secrets.
- Secret reads require `secrets:read` or a server-side job capability, never
  plain `memories:read`.
- API keys cannot be used to export provider secrets.
- Rotating a secret creates an audit event and invalidates dependent health
  checks until they pass again.

## Simple First Version

Start with organization and team secrets, plus project-level model-policy
overrides that select existing secrets. Project and service-account owned raw
secrets are later. Avoid per-file, per-branch, or arbitrary condition
expressions until customer use proves they are needed.

See [Backend contracts](backend-contracts.md) for vault adapter and envelope
encryption invariants.
