# Security Policy

Engram is being redesigned as a server-only engineering memory product. The
protected security boundary is the company server, not a developer-machine
worker.

## Supported Branches

- `master`: target product and architecture branch.
- `upstream`: source snapshot from the original Apache-2.0 project, retained for
  reference only.

## Scope

Security review covers:

- server APIs and admin UI;
- hook adapters for Claude Code and Codex;
- API keys, scoped agent tokens, and managed hook credentials;
- tenant/team/project isolation;
- PostgreSQL storage, migrations, audit log, and durable outbox;
- vault adapter and encrypted database secret storage;
- retrieval authorization and memory injection.

Security review does not treat local SQLite, local vector stores, or local
background workers as valid target-runtime controls.

## Required Invariants

- No provider secrets in hooks, prompts, observations, memories, traces, logs, or
  frontend responses.
- API keys and agent tokens can narrow access, never expand owner permissions.
- Authorization filters run before retrieval results are ranked or returned.
- Hook writes derive scope from server-side bindings, not from client-supplied
  scope expansion.
- Every memory read, memory write, secret use, model-policy resolution, and
  sensitive admin action is audited.
- Raw secrets are stored only in an external vault or encrypted database envelope
  with key versioning.
- Local retry envelopes are disabled by default for sensitive payloads and must
  not become a local memory store.

## Reporting

Report vulnerabilities through GitHub private vulnerability reporting when
available, or to the repository owner through a private channel. Do not file
public issues for exploitable vulnerabilities.

Include:

- affected commit or deployment version;
- component and endpoint;
- reproduction steps;
- impact;
- whether tenant isolation, secrets, memory injection, or audit integrity are
  affected.

Do not include real provider secrets, customer memory, or production API keys in
reports.
