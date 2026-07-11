# Runtime Durability Prerequisite

Date: 2026-07-10

Status: proposed authorized infrastructure design

Pre-rollout block: D1 merged and D2 storage bootstrap recorded

## Goal

Make ordinary process and container recreation preserve the transport and
scheduler state that Engram already claims is durable, and make every
long-running process honor a bounded graceful shutdown.

This is an infrastructure prerequisite. It does not deploy the C1.1 schema,
change API routing, switch a product producer, or claim product-domain
completion.

## Current Contradictions

- API, five workers, Beat, and relay run as children of `sh -ec`; Docker signals
  the shell rather than the long-running process.
- Compose's implicit stop timeout is shorter than Granian's graceful budget,
  the relay drain contract, and the 660-second maximum Celery task hard limit.
- workers explicitly disable broker reconnect and long-lived services lack a
  restart policy;
- RabbitMQ uses an anonymous data volume and an implicit nodename; recreating
  the service can select a new empty Mnesia directory after the outbox row has
  already been deleted on publish;
- Beat stores `celerybeat-schedule` inside its replaceable container.

## Required Runtime Contract

### Direct PID 1 and stop budgets

Long-running commands use Compose list form or a startup wrapper whose final
operation is `exec`. At steady state, container PID 1 must be:

- Granian for API;
- Celery worker for every queue worker;
- Celery Beat for scheduler;
- the Django outbox relay process for relay.

Minimum contracts:

- API: `SIGTERM`, 45-second grace, exceeding Granian's 35-second worker-kill
  budget;
- workers: `SIGTERM`, 12-minute grace, exceeding the 660-second maximum task
  hard limit plus margin;
- relay: `SIGTERM`, 60-second Docker grace with an explicit package shutdown
  timeout of at most 45 seconds;
- Beat: `SIGTERM`, 30-second grace.

The worker grace is a final safety net. Release operations must still cancel
consumers and observe idle workers before stopping them.

### Autonomous broker reconnect

Celery retries broker connection on startup and after a connection loss with
an unbounded retry count and bounded Celery/Kombu backoff. Soft shutdown on idle
is enabled. A bounded Rabbit restart must not require worker recreation.

### RabbitMQ state

Fresh installs use:

- a named volume mounted at `/var/lib/rabbitmq`;
- an explicit stable nodename/hostname;
- `restart: unless-stopped`;
- a readiness probe that can inspect the application vhost and queue listing,
  not only ping the Erlang VM.

Existing production cannot rename the node while copying Mnesia files. The
first named-volume adoption must:

1. record the current anonymous volume, exact Rabbit nodename, hostname,
   Mnesia directory, definitions, queue counts, and ownership;
2. stop Beat and relay, cancel consumers, observe idle workers, and stop those
   workers before Rabbit stops; API intake remains available and accumulates
   durable outbox rows;
3. stop Rabbit cleanly;
4. copy the source volume into the new named volume without changing ownership;
5. configure `RABBITMQ_NODENAME` with the exact source value, set the container
   hostname to the nodename suffix after `@`, and prove that hostname resolves
   on the Compose network;
6. preserve the Erlang cookie contents, owner, and mode without printing it;
7. start Rabbit and prove the same definitions, queues, ready/unacknowledged
   counts, nodename, and data directory;
8. start old workers, then relay, then Beat and reconcile the outbox/broker
   baseline.

Starting a blank node when a source volume exists is a hard stop. A later
human-readable nodename cleanup requires its own supported offline rename or
export/import rehearsal.

### Beat state and singleton

Beat uses an explicit schedule file on a named volume. Exactly one Beat may run.
The production adoption copies the existing schedule file when present and
valid. If it is missing or corrupt, bootstrap stops outside all due-boundary
windows until the bounded idempotent catch-up behavior is reviewed.

The file proves scheduler-cursor continuity only. It does not prove that a
domain occurrence was durably created or completed.

### Restart policy

API, workers, relay, Beat, RabbitMQ, Redis, PostgreSQL, and frontend use an
explicit reviewed restart policy. Controlled `docker compose stop` remains
stopped under `unless-stopped`; an unexpected process/host restart recovers.

## Serial Delivery

### D1 — Runtime contract code

Owned files:

- `deploy/compose/docker-compose.yml`;
- a minimal API startup wrapper only if local migrate-then-exec cannot be
  represented safely in Compose list form;
- `apps/backend/engram/celeryconfig.py`;
- focused Celery and Compose contract tests;
- one bounded Compose fault-E2E script;
- the Compose deployment documentation.

D1 changes no migration, hook, provider, task payload, routing table, public API,
or tenant-scoped data path.

### D2 — Exact-old-runtime persistence bootstrap

D2 is an operational checkpoint after D1 merges. It uses the currently deployed
application image and changes no application revision or database schema.
It leaves the single public API container running and untouched; API PID-1
correction is activated later through the loss-aware two-slot cutover.

D2 holds an exclusive host lock and atomically persists a non-secret phase
journal. Every external mutation has an intent phase before it and an observed
completion phase after it:

```text
PRECHECK
  -> OLD_ASYNC_STOP_INTENT -> OLD_ASYNC_STOP_COMPLETE
  -> RABBIT_STOP_INTENT -> RABBIT_STOP_COMPLETE
  -> RABBIT_COPY_INTENT -> RABBIT_COPY_COMPLETE
  -> BEAT_COPY_INTENT -> BEAT_COPY_COMPLETE
  -> NAMED_RABBIT_START_INTENT -> NAMED_RABBIT_VERIFIED
  -> OLD_ASYNC_RESTORE_INTENT -> OLD_ASYNC_RESTORED
  -> COMPLETE
```

Resume reconciles the journal with live containers, mounts, nodename, file
ownership, and queue counts before completing or aborting the same transition.
The original anonymous Rabbit/Beat source volumes and stopped source containers
remain immutable throughout D2 and its rollback observation window. Copy
helpers mount sources read-only; a partial destination is quarantined under its
attempt identity rather than deleted or reused.

It rehearses and then performs:

- anonymous-to-named Rabbit volume adoption with exact nodename preservation;
- ephemeral-to-named Beat schedule adoption;
- async/Rabbit/Beat recreation under direct PID 1, explicit stop grace, and
  restart policies;
- explicit old-worker restart after Rabbit returns; the deployed old image is
  known to disable autonomous reconnect, so D2 does not claim otherwise;
- abort back to the original anonymous volume/container when any identity or
  count differs.

C1.1 production mutation cannot begin until D2 evidence is recorded. D1's
reconnect configuration remains dormant in production until the C1.1 target
workers start.

### D3 — Target-runtime activation

D3 is part of the C1.1 rollout after the target workers start. It proves their
effective configuration contains startup reconnect, reconnect-after-loss,
unbounded retries, and idle soft shutdown. The exact-image rehearsal performs
the broker outage/recovery fault against that target image. Production does not
restart Rabbit a second time merely to demonstrate the already-rehearsed fault.

## Required Tests

### Focused RED/GREEN contracts

- API/worker/Beat/relay steady-state PID 1 is not `sh`;
- every stop grace meets the exact minimum above;
- relay package timeout is strictly below Docker grace;
- worker reconnect-at-startup, reconnect-after-loss, unbounded retries, and
  idle soft shutdown are enabled;
- Rabbit has a named data volume, explicit nodename/hostname, readiness, and
  restart policy;
- Beat has one service, explicit schedule path, named volume, and restart
  policy;
- no secret value or rendered environment appears in diagnostic output.

### Fault E2E

1. Publish a deterministic task, pause consumers after the outbox row has been
   removed, recreate Rabbit on the same named volume, and prove the ready
   message remains and reaches one terminal test result.
2. In exact-target-image rehearsal, stop Rabbit while workers and relay run,
   create a durable outbox item, restart Rabbit, and prove worker/relay recovery
   without container recreation.
3. Recreate Beat and prove the schedule file survives and only one Beat process
   runs.
4. Send `SIGTERM` to API, relay, Beat, and an idle worker and prove each exits
   through its own handler inside the reviewed grace period.
5. Rehearse anonymous-to-named copy from a random source nodename, then prove a
   deliberately wrong nodename/blank directory is detected and rejected before
   any producer or consumer resumes.
6. Inject a controller crash immediately before and after every D2 mutation
   above, including partial copy and named-Rabbit start, and prove deterministic
   resume or rollback while the immutable source remains attachable.

Package-owned relay select/publish/delete correctness remains verified in the
`django-celery-outbox` reference gate; Engram does not duplicate package
internals.

## Verification and Review

The D1 PR records:

- TDD RED/GREEN and fault-E2E commands;
- scoped Ruff/format, Compose syntax using only example values, and repository
  quality;
- independent exactness and Karpathy reviews;
- bounded secret-output review only;
- Claude/Fable xhigh adversarial review;
- current-head CI.

D2 records exact old image/digest, volume ids, nodename, counts, commands, exit
codes, timestamps, abort rehearsal, and post-bootstrap observation. It never
runs `docker compose config --format json`, prints `.env`, or uses `down -v`.

## Stop Conditions

Stop before mutation or restore the original old runtime if:

- the current Rabbit nodename, Mnesia directory, or anonymous source volume is
  ambiguous;
- any worker reply is missing;
- active/reserved/scheduled tasks or broker unacknowledged deliveries do not
  reach zero within 12 minutes;
- the copied named volume changes definitions, queue topology, ready/
  unacknowledged counts, ownership, or nodename;
- Beat schedule state cannot be copied or safely reinitialized;
- any D2-mutated async service still has shell PID 1 or requires `SIGKILL`;
- D2 old workers fail explicit restart/re-registration after Rabbit returns;
- D3 target workers fail autonomous reconnect in exact-image rehearsal;
- recovery would require queue purge, volume deletion, outbox mutation, or
  database restore.

## Gate Closure

The pre-rollout prerequisite closes when D1 is merged with all review/CI
evidence and D2 proves production uses named Rabbit/Beat state, direct PID 1,
reviewed grace, and restart policies for async services on the exact old
application revision. The unchanged single API is not restarted in D2.

The full runtime contract closes during C1.1 only after D3 proves the target
API has direct Granian PID 1, target workers expose the reviewed reconnect
configuration, and the exact-image broker outage rehearsal has passed.

It remains a transport/runtime claim. Empty queues are not evidence that
required product work was ever created or completed.
