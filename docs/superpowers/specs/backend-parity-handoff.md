# Handoff: Backend ↔ Frontend Parity

Audience: backend implementation agent.
Context: the frontend was redesigned to the premium console and extended with UI for
several backend-ready features (branch `feat/frontend-premium-redesign`). This document
lists the remaining backend work in two directions:

- **Part A** — the new frontend renders things the backend does not yet serve.
- **Part B** — backend-ready features: where the frontend now stands and what backend
  work still blocks them.
- **Part C** — cross-cutting backend items (auth realms, list endpoints, envelopes).

Endpoint facts below were read from `apps/backend/engram/**` on this branch's base
(`origin/master`). Where an endpoint is marked "bearer API-key auth", it uses
`ResolveApiKeyScope` / `bearer_key(request)` and will NOT accept the console's
`Authorization: Token <session>` — see Part C.1.

---

## Part A — Backend missing for existing frontend

### A1. Dashboard analytics (currently presentational placeholders)
The Overview dashboard renders representative values; none have a backing endpoint.
- **Memories indexed** — total count + period delta. Suggest `GET /v1/admin/metrics/overview` (org/project scoped) or a count on the memories inspection list.
- **Context bundles assembled (7d)** — count + delta over a window.
- **Avg retrieval latency (ms)** — needs retrieval timing instrumentation surfaced as a metric.
- **Connected agents** — count + live sessions list: `{ agent_name, model_id, status: active|idle, last_seen }`. No agent-session/heartbeat endpoint exists.
- **Memory ingest series** — daily memory-captured counts for the last 14 days (time series).
- **Recent activity feed** — org-wide recent events; could derive from audit events widened beyond a single project.
- **Weekly digest** — `{ merged_count, retired_count, ready }` summary + a "review digest" action.

### A2. Memories list/detail enrichment
The memory inspection payload lacks fields the design needs (derived from `metadata` today, defaulting/omitting when absent):
- **kind** classification (`decision|convention|gotcha|architecture`) — first-class field.
- **tags**.
- **source file path** on the memory item.
- **captured-by agent** attribution.
- **confidence as numeric percent** (currently nullable string; parsed client-side).
- **authorized-for-injection** flag (currently inferred from `status==='active' && !refuted && !stale`).
- **related memories** — a real related/links endpoint (today derived from `retrieval_documents`/versions).
- **human project name/slug** on the memory payload (only `project_id` UUID is present).
- **add-to-context-bundle** action endpoint (CTA is presentational).
- **sidebar Memories badge count** — a quick count for the nav badge.

### A3. Projects
- **Per-project memory count** field on the Project payload (Projects table shows `—`).

### A4. Members
- **Invited/pending lifecycle state** — `Member` only has an `active` boolean; the design's amber "Invited" status cannot be represented.
- **Role display name** on the member (only `role` code today; the UI joins names from the roles query).

### A5. Audit
- **Actor/target display-name resolution** — rows show raw `actor_type/actor_id` and `target_type/target_id`; no identity resolution endpoint.

### A6. Settings (cards are presentational previews)
- **Embedding provider + model** read/update endpoint (Memory model card).
- **Retrieval settings** persistence (hybrid retrieval, require provenance toggles).
- **Purge organization memory** action endpoint (button is disabled/preview).

### A7. Login
- **GitHub OAuth / SSO** endpoint — the "Continue with GitHub" button is decorative; only username/password `login()` exists.

### A8. Org/Project switchers
- **Per-org membership count + viewer role** for the org dropdown meta (currently shows the slug). The switcher already consumes real org/project list endpoints.

---

## Part B — Backend-ready features: frontend status + remaining backend work

| Feature | Backend endpoint(s) | Capability | Auth realm | Frontend (this branch) | Remaining backend work |
|---|---|---|---|---|---|
| **Search Debugger** | `POST /v1/admin/search-debug/` | `memories:read` | Console session (Token) ✓ | Built & functional (`/search-debug`) | None — works as built |
| **Context Bundles** | `GET /v1/inspection/context-bundles`, `/{id}` | `memories:admin` | Bearer API-key | Built (`/context-bundles` list + detail) | Accept console-session auth (Part C.1) |
| **Secrets** | `POST /v1/model-policy/secrets`, `GET /{id}`, `POST /{id}/rotate`, `POST /{id}/disable` | `secrets:*` | Bearer API-key | Built (`/secrets` create/rotate/disable + list) | **Add `GET /secrets` list** (in progress) + console-session auth |
| **Model Policies** | `POST /v1/model-policy/policies`, `GET /v1/model-policy/resolve` | `model_policy:*` | Bearer API-key | Built (`/model-policies` create + resolve tester + list) | **Add `GET /policies` list** (in progress) + console-session auth |
| **Memory enrichment** | `GET/POST /v1/memories/{id}/links`, `POST /v1/memories/{id}/feedback`, `POST /v1/memories/{id}/version`; versions via `GET /v1/inspection/memories/{id}` | `memories:read` / `memories:review` | Bearer API-key | Built (links + feedback + version history on memory detail) | Console-session auth; a general `GET /v1/memories/{id}/diff` (only memory-review has a diff today) |
| **Health / Ops dashboard** | Probes `GET /-/healthz/`, `/-/readyz/`, `/-/startup/`; `GET /-/metrics` (Prometheus text, bearer-token gated) | none / metrics token | n/a | Health page shows probe status | **Expose JSON ops metrics**: queue depth, outbox lag, worker + provider failure counts, index lag. Today `/-/metrics` is Prometheus text with only `http_requests_total{method,status}` |

Notes:
- The user reported a merged `POST /v1/admin/search-debug/`; it is present on `origin/master`
  (`db483e41`) and the frontend is wired to it directly.
- Secrets/Policies **list endpoints do not exist yet** (the `*ListView` classes implement
  only `post`). The frontend list calls are written tolerantly (`{items}`/`{results}`) and
  degrade to an empty state until the GET list lands.

---

## Part C — Cross-cutting backend items

### C1. Auth realm unification (highest priority)
Two auth realms are mixed:
- **Console session** (`Authorization: Token <session>` + `X-Engram-Organization`): used by
  `search-debug`, members, projects, api-keys, roles, organizations, teams, workflow-runs,
  memory-review.
- **Bearer API-key** (`ResolveApiKeyScope` / `bearer_key`): used by inspection
  (memories, audit-events, context-bundles), `memories/{id}/links|feedback|version`, and
  `model-policy` (secrets/policies).

The admin console authenticates with the Token session, so it cannot satisfy the bearer
endpoints as-is. This already affects the existing memories/audit inspection pages and now
the new context-bundles / secrets / policies / memory-enrichment pages. **Decide and
implement one path**: (a) accept the console Token session (with capability checks) on
these endpoints, or (b) mint a short-lived scoped API key for the console session and have
the frontend send it as a bearer for these calls. Until then, these pages will surface auth
errors against a real backend.

### C2. List endpoints for model-policy
`ProviderSecretListView` and `ModelPolicyListView` implement only `post`, so a `GET` on
`/v1/model-policy/secrets` and `/v1/model-policy/policies` currently returns **HTTP 405
Method Not Allowed**. Add a scoped `get` (list) to both (+ a `scoped_policies` helper
mirroring `scoped_secrets`). `provider_secret_response` already masks the secret (raw is
write-only), so listing is safe. (In progress.) Until it lands, the frontend `listProviderSecrets`
/ `listModelPolicies` clients swallow 404 **and 405** and render an empty state, so the
pages degrade gracefully rather than erroring.

### C3. List envelope consistency
Inspection lists return `{count, items}`; admin lists return `{results}`; model-policy
returns bare objects. Standardize the list envelope (recommend `{count, items}`) so the
frontend client does not need per-endpoint tolerance.
