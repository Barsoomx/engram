# C1.1 Loss-Aware Rollout Gate

Date: 2026-07-10

Status: proposed authorized deployment design

Depends on:

- `docs/decisions/2026-07-10-domain-progress-and-transport.md`;
- `docs/superpowers/specs/2026-07-10-checkpoint-1-lossless-work-creation.md`;
- D1 merged and the D2 pre-rollout block from
  `docs/superpowers/specs/2026-07-10-runtime-durability-prerequisite.md` closed
  on the exact previous application revision;
- merged rolling-default repair `5366def5`;
- `django-celery-outbox` as the sole package transport authority.

## Goal

Apply C1.1 and replace every backend process without a deployment-created gap
in publicly accepted evidence, without killing in-flight asynchronous work, and
without running migrations from a long-lived API.

## Required Outcomes

1. The previous API remains publicly routable while migration and candidate
   direct checks run.
2. One immutable approved candidate image owns the exact migration invocation.
3. Every expected worker acknowledges quiescence and reaches idle before stop.
4. Broker queues are empty before old workers stop; new API intake accumulates
   only identifiable durable outbox rows while relay is paused.
5. A healthy inactive API slot takes traffic before the active slot stops.
6. Every synthetic hook response succeeds and every ledger id maps to exactly
   one durable raw-event identity through cutover and rollback.
7. The previous image remains compatible with the expanded schema.
8. Transport counts remain operational evidence, never product-completion
   evidence.

## Non-Goals

This gate does not add a client-side hook spool, broker HA, a second outbox,
CP2 leases/reconciliation, credential rotation, C1.2 producer changes,
historical repair, or a claim that empty queues prove required work was created.

Arbitrary host/network outages can still lose fail-open client events. This
gate closes only the outage window created by Engram's own deployment.

## Transport Boundary

`django-celery-outbox` owns package persistence, publish retry, and dead letters.
RabbitMQ owns published messages. Engram never deletes package rows, purges
queues, acknowledges messages, or synthesizes domain work during release.

C1.1 carries no broker message across revisions. The broker census includes the
five Engram application queues and every Celery quorum native-delay bucket/
exchange discovered from the live topology, including `celery_delayed_*`.
Before old workers stop, Beat and relay are stopped, application queues reach
zero ready and zero unacknowledged, and every native-delay bucket reaches zero
total messages. API intake continues; new package rows remain in PostgreSQL
with relay stopped.

The controller records every carried package identity as
`(id, task_id, task_name, schema_version)` and requires:

- schema version supported by both exact images;
- task name present in the candidate registered-task manifest;
- every recorded row remains present until target relay starts, or is later
  reconciled to package deletion after the package-owned confirmed-publish
  boundary; worker receipt/execution is recorded separately and is not silently
  promoted to product completion;
- no dead-letter delta.

Equal counts alone are never accepted because deletion plus a new insert can
produce a false green.

## Serial Delivery

### L1 — Tracked rollout substrate

One infrastructure PR owns:

- a sanitized production Compose overlay;
- stable `api-a` and `api-b` services;
- an explicit one-shot migration service;
- a fail-closed traffic gate and per-slot revision header;
- a resumable host release controller;
- controller unit tests and a local Traefik cutover E2E;
- bounded deployment documentation.

It changes no hook, provider, task payload, domain model, public response body,
or tenant authorization path.

### L2 — Exact-image rehearsal and production execution

After L1 merges with green CI, L2 changes no tracked code. It rehearses exact
previous/candidate images, rollback, and decisive crash points, then executes
production and records the C1.1 gate. C1.2 cannot start earlier.

## Production Compose Contract

### Stable API slots

`api-a` and `api-b` share the backend contract but have unique DNS aliases.
Exactly one is active at steady state. Production frontend/server-side calls
use the public Traefik URL, not a shared slot alias; no internal client may
resolve an inactive slot through a stable `api` name. Each slot has:

- a unique Traefik router, service, and response-header middleware;
- no conflicting host port;
- an immutable image digest and explicit revision;
- an application readiness healthcheck;
- a gate marker in a host-mounted non-secret per-slot state directory, required
  by Docker health before Traefik may publish it;
- direct Granian PID 1, `SIGTERM`, and the runtime prerequisite's API grace.

The active marker persists across active-slot container restart. Before every
inactive-slot start, the controller removes only that inactive marker and proves
the slot is unpublished. Direct checks then run inside the unhealthy container.

Slots use fixed priorities. If the inactive slot has higher priority, creating
its marker shifts public requests while old remains available. If it has lower
priority, the controller first creates its marker and proves its healthy router
and service exist, then removes the old slot's marker; Traefik falls through to
the already-published candidate. The controller never assumes the inactive slot
is always higher priority.

The controller records active/inactive slot and priorities in non-secret state.
Slots alternate each release, so no second route switch or API recreation is
performed merely to restore a service name.

### Migration service

API slots start Granian only and never run `manage.py migrate`.

The release-profile `migration` service:

- uses the exact candidate digest;
- has Traefik disabled, no restart, and no port;
- accepts only the exact reviewed migration target;
- runs under bounded PostgreSQL lock/statement timeouts;
- is protected by the controller's exclusive host lock and recorded intent.

One mutation owner means one one-shot migration process/invocation. If the
migration commits before phase completion, resume inspects migration state and
physical schema and advances without launching a second schema mutation.
Counting one `django_migrations` row alone is not sufficient proof.

### Public slot identity

Traefik adds `X-Engram-Deployment-Slot` and `X-Engram-Revision` from reviewed
non-secret Compose values. Public verification must identify the expected slot
and revision; shared database health is not route identity.

## Async Quiescence

Every external mutation has an atomically persisted `*_INTENT` phase before it
and an observed `*_COMPLETE` phase after it. Resume reconciles the phase record
with live Beat, relay, worker, broker, and container state; it never assumes an
intent completed.

### Precheck

Record:

- five expected worker nodes and their exact queues;
- complete ping, active-queues, registered, active, reserved, and scheduled
  replies;
- Rabbit ready, unacknowledged, and consumers for every Engram queue;
- total message counts and bindings for every Celery native-delay queue/
  exchange, even when no worker reports the task as scheduled;
- outbox identity set and dead-letter baseline;
- candidate task manifest and supported package schema versions;
- Beat singleton, backend revisions, and runtime-prerequisite evidence.

Missing/malformed worker replies, unsupported package rows, or task-manifest
mismatch fail the gate.

### Drain order

1. Persist `BEAT_STOP_INTENT`; stop Beat; prove absence and schedule-volume
   continuity; persist `BEAT_STOP_COMPLETE`.
2. Persist `RELAY_STOP_INTENT`; signal relay. Its PID-1 handler drains within
   its package deadline. Prove relay absence and record outbox identities;
   persist `RELAY_STOP_COMPLETE`.
3. Leave old workers consuming until every Engram application queue reaches
   zero ready and zero unacknowledged, every native-delay bucket reaches zero
   total messages, and active/reserved/scheduled are empty.
4. Persist `CONSUMER_CANCEL_INTENT` with all node/queue pairs; issue targeted
   `cancel_consumer`; require every acknowledgement and absence from
   `active_queues`; persist `CONSUMER_CANCEL_COMPLETE`.
5. Require two identical idle samples at least five seconds apart. The total
   drain deadline is 12 minutes from `RELAY_STOP_COMPLETE`.
6. Persist `OLD_WORKER_STOP_INTENT`; stop idle workers; prove all old worker
   containers absent; persist `OLD_WORKER_STOP_COMPLETE`.

Because Beat and relay are already stopped, no trusted publisher can refill an
application or native-delay queue between the zero sample and consumer
cancellation. Any unexpected new broker message fails the gate.

### Target start order

1. Persist `TARGET_WORKER_START_INTENT`; start target workers while relay and
   Beat remain stopped and broker queues are proven empty; require exact
   revision, all ping/registered/queue replies, then persist
   `TARGET_WORKER_START_COMPLETE`.
2. In a read-only repeatable-read PostgreSQL transaction, persist a final MVCC
   snapshot identifier and the complete set of visible outbox identities as
   `RELAY_RESTART_CUTOFF_COMPLETE`. Rows not visible in that snapshot are
   classified as post-cutoff even if their sequence id was allocated earlier.
3. Persist `TARGET_RELAY_START_INTENT`; start target relay, verify revision and
   liveness, then persist `TARGET_RELAY_START_COMPLETE`.
4. Reconcile every cutoff identity to still-present package state,
   package-owned confirmed-publish deletion, or explicit dead-letter failure.
   Post-cutoff rows follow ordinary package processing and cannot substitute
   for a missing cutoff identity.
5. Persist `TARGET_BEAT_START_INTENT`; start Beat last, prove exact revision,
   persistent schedule path, and singleton; persist
   `TARGET_BEAT_START_COMPLETE`.
6. Prove the effective target-worker reconnect/idle-shutdown configuration and
   close runtime D3; do not restart production Rabbit solely for this proof.

### Abort order

Abort mutations use the same intent/completion discipline.

If target async startup partially began, recovery first stops target Beat,
stops target relay, cancels target consumers, observes them idle, and stops
target workers. It then restores old workers, verifies their registrations and
queues, starts old relay, and starts old Beat last. Mixed-revision consumers or
two Beat instances are forbidden.

Before old workers stop, abort re-adds every cancelled old consumer, verifies
the acknowledgement set, restarts relay if needed, and starts Beat last.

## API and Migration State Machine

The controller holds an exclusive host lock and atomically replaces a
non-secret JSON phase record containing image digests, revisions, active slot,
migration target, phase, timestamps, and ledger path. It never stores
credentials or rendered environment.

```text
PRECHECK
  -> ASYNC_DRAIN_PHASES
  -> MIGRATION_INTENT
  -> MIGRATED_AND_PHYSICALLY_VERIFIED
  -> INACTIVE_SLOT_START_INTENT
  -> INACTIVE_SLOT_DIRECT_VERIFIED
  -> INACTIVE_GATE_OPEN_INTENT
  -> PUBLIC_CUTOVER_VERIFIED
  -> OLD_SLOT_DRAINED
  -> OLD_SLOT_STOP_COMPLETE
  -> TARGET_ASYNC_START_PHASES
  -> COMPLETE
```

### Continuous hook ledger

Start at least 60 seconds before async drain and continue until at least 60
seconds after target Beat starts. Rehearsal runs 2 requests/second for at least
10 minutes. Production runs 1 request/second for at least 15 minutes.

Every request uses a deterministic unique id. The secret-free ledger records id,
start/end, HTTP status, latency, and observed slot/revision. Accepted outcomes
are 202 or the documented duplicate-success response. Thresholds are exact:
zero timeout/reset/404/5xx responses, zero missing ids, and zero duplicate raw
identities.

The production ledger uses a dedicated canary organization/project with
realtime work disabled, one stable canary session, lifecycle-only events, and
no user or repository content. Only a dedicated canary service identity/key is
granted to that organization; it has no ordinary user membership, project
grant, team link, or cross-organization visibility. Idle sweep may later create
canary-scoped lifecycle/work history, so the gate does not claim that candidates
are impossible. Instead, request-scoping tests and a post-run query prove no
canary observation, candidate, memory, or retrieval document is visible from
any ordinary production organization/project scope. Durable canary evidence is
retained as rollout audit history.

### Migration proof

The exact candidate image applies
`0032b_agentsession_end_work_db_default` after its recorded dependency. Then
prove:

- both exact migration rows are applied;
- `end_work_contract_version` is `NOT NULL` with physical default 0;
- an exact previous-image historical writer omits the new field, observes
  stored value 0 inside a transaction, and rolls that transaction back;
- C1.1 no-backfill counts remain zero;
- the continuous public ledger remains clean through the old API.

### Inactive-slot direct proof

With its gate absent:

- PID 1 is Granian;
- readiness and system check pass;
- a direct hook creates its exact raw-event identity;
- digest/revision match the phase record;
- public traffic still reports the old active slot;
- no migration process remains.

### Public cutover and old-slot drain

Persist gate-open intent, create the inactive-slot marker, and wait for healthy
plus Traefik propagation. For a lower-priority candidate, additionally persist
old-gate-close intent and close the old gate only after candidate publication is
proved. Public headers must then switch to the candidate revision while the
ledger remains clean.

Keep the old slot alive for at least 45 seconds, then stop it with graceful
shutdown intent/completion phases. Do not remove its image during rollback
observation. The new slot becomes the recorded active slot.

### Rollback

Never reverse 0032/0032b.

Before cutover, stop the gated inactive slot; old remains public. After cutover,
start and directly verify the previous image in the inactive slot, open its
gate, prove its router is published, and use the same fixed-priority transition:
either it wins immediately or the failed slot's gate closes only after the
previous slot is ready. Then prove public previous revision plus a persisted
hook, drain/stop the failed slot, and restore old async processes in the exact
abort order. Every step has intent/completion state and ledger reconciliation.

## Exact-Image Rehearsal

Use exact immutable previous/candidate images, the same Compose merge, and the
same Traefik Docker-provider health behavior as production. A disposable stack
contains representative old-schema rows and sustained hook traffic.

Unit tests fault every controller transition with a fake runner. Exact-image
integration injects four decisive crash classes:

1. partial consumer cancellation;
2. migration committed before phase completion;
3. inactive gate opened before public verification completes;
4. cutover or rollback interrupted between active/inactive slots.

It also proves relay/outbox identity reconciliation, previous-writer schema
compatibility, target registration before relay start, and the full rollback.
It schedules at least one native delayed/countdown delivery and proves the drain
cannot report zero until every corresponding delayed bucket is empty.
Runtime durability faults remain owned by the prerequisite spec rather than
being duplicated here.

## Verification Per Slice

L1 records TDD RED/GREEN, controller and local Traefik E2E, Ruff/format,
Compose syntax using example values, `git diff --check`, repository quality,
independent exactness, Karpathy simplicity, bounded secret-output/request-path
review, Fable/xhigh, and current-head CI.

The bounded security question is only whether controller output renders secrets
or routing bypasses the unchanged authenticated API. No frontend check or broad
security scan belongs to this deployment-only slice.

## Stop Conditions

Stop or execute the recorded abort path if:

- runtime durability prerequisite evidence is absent;
- candidate digest, attestation, revision, active slot, or migration intent is
  ambiguous;
- a long-lived API can run migration or a second migration invocation would be
  launched after commit;
- migration lock/statement budget is exceeded or physical default is wrong;
- previous writer fails;
- any worker reply is missing;
- relay shutdown or drain exceeds 12 minutes;
- an application queue has ready/unacknowledged messages or a native-delay
  bucket has any message at old-worker stop;
- any carried package identity disappears without its reconciled path, uses an
  unsupported schema, or names an unregistered candidate task;
- target async processes start out of order or mixed revisions appear;
- inactive slot is public before its gate, direct hook fails, or public slot
  identity is unprovable;
- any ledger request fails or any id is missing/duplicated;
- recovery would require queue purge, package mutation, migration reversal,
  database restore, or `SIGKILL` without a new reviewed decision.

## Gate Closure

C1.1 closes only when:

- runtime prerequisite and L1 merged/deployed serially with review/CI evidence;
- exact-image rehearsal and rollback pass all four decisive crash classes;
- production migration, previous writer, physical schema, and no-backfill proofs
  pass;
- every old async process is replaced in the reviewed order;
- exactly one target API slot and one target Beat remain;
- migration and inactive containers are absent;
- all carried outbox identities reconcile and broker queues recover normally;
- the production ledger runs 1 request/second for 15 minutes with exactly zero
  request errors, missing ids, or duplicate identities;
- commands, exit codes, SHAs, digests, counts, timestamps, and rollback evidence
  are recorded.

Only then may C1.2 start.
