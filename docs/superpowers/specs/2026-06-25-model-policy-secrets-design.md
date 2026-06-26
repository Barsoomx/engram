# Model Policy Secrets Design

## Goal

Add the first backend foundation for provider secrets and model policy so later
semantic retrieval, memory curation, digests, and provider-backed workers can
resolve task-specific providers without exposing raw credentials.

## Architecture

Create a small `engram.model_policy` Django app. The app owns provider secret
references, model policy records, resolution services, provider adapter
selection, and provider call audit rows.

The first version supports organization and team owned secrets. Raw secret
values are encrypted into database envelopes with key version, HMAC, and
rotation state. API responses, audit metadata, logs, and tests must never expose
the raw secret value. Vault integration is represented by a storage-mode field
and domain boundary, but this checkpoint only implements database envelopes.

Model policies can be scoped to organization, team, or project. Resolution is
explicit: project policy, then team policy, then organization policy. A policy
selects provider/model settings per task type, allowed/blocked providers,
fallback behavior, and the secret reference used by server-side provider calls.

Provider adapters are fake local adapters in this checkpoint. They prove that
workers can resolve a policy, select Anthropic generation, OpenAI generation, or
OpenAI embeddings, record provider/model/policy version/request metadata, and
return redacted call evidence without making network calls.

## API Surface

- `POST /v1/model-policy/secrets`
- `POST /v1/model-policy/secrets/<secret_id>/rotate`
- `POST /v1/model-policy/secrets/<secret_id>/disable`
- `GET /v1/model-policy/secrets/<secret_id>`
- `POST /v1/model-policy/policies`
- `GET /v1/model-policy/resolve`

Every endpoint uses API-key scope resolution. Secret writes require
`secrets:*`; policy writes require `model_policy:*`; policy resolution requires
`model_policy:*` for this admin foundation. Later worker-only reads can use an
internal service capability instead of an API key.

## Non-Goals

- No real Anthropic or OpenAI HTTP calls.
- No semantic retrieval implementation.
- No AI workflow or digest scheduler.
- No frontend/admin UI.
- No MCP tools.
- No project-owned raw secrets or service-account secrets.
- No external Vault adapter implementation beyond the storage-mode boundary.

## Security Requirements

- Raw provider secrets must not be stored in plaintext fields.
- Raw provider secrets must not appear in API responses, audit metadata,
  provider call records, repository verification docs, or logs.
- Secret rotation creates a new active envelope version and deactivates the old
  envelope.
- Disabled secrets cannot be selected for provider calls.
- Scope validation rejects cross-organization team, project, policy, and secret
  references before writes.
- API keys cannot export provider secret plaintext.
- Provider call records store provider, model, task type, policy version,
  tenant scope, request id, redaction state, token usage, latency, and cost
  metadata, but never request bodies or raw secrets.

## Verification

Focused backend tests cover encrypted secret creation, redacted responses,
rotation/disable behavior, project/team/organization policy resolution,
cross-scope denial, provider adapter selection, and provider call audit records.
Repository checks pin the new package surface and evidence docs.
