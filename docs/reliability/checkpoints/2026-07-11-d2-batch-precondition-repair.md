# D2 Batch Worker Precondition Repair

Date: 2026-07-11

Status: blocked; do not execute

## Decision

The exact-same-container restart described below is rejected before mutation.
Read-only follow-up proved `State.OOMKilled=true`; the command does not set
concurrency, so Celery uses its CPU-based default of four on this host; the
worker already consumes about 1.955 GiB; only about 2.121 GiB is available with
no swap or cgroup limit; and `task_acks_late=False`. Automatic `unless-stopped`
relaunch would immediately expose 307 ready messages to the same OOM-prone
early-ack runtime and could lose work before execution completes.

No signal or production mutation was performed. A replacement requires its own
reviewed, journaled bounded-concurrency/runtime-recovery design. It must prevent
automatic same-command restart before any signal, prove graceful child and
shell exit, and establish a reviewed memory and acknowledgement safety envelope
before a replacement consumer sees the queue. This document remains as the
rejected-plan and blocker record; its mutation section is not executable.

## Purpose

Restore the missing `engram-batch` consumer so D2 can later satisfy its
five-worker read-only precheck. This is not storage adoption, a release, a
configuration change, or permission to begin D2 mutations.

The rejected repair would have signaled and, only if necessary, explicitly
started the existing batch container. It would not have changed the container,
image, command, environment, network, queue, message, outbox, database, or any
other service.

## Read-Only Evidence

The 2026-07-11 preflight observed:

- container `engram-worker-batch-1` is running from
  `ghcr.io/barsoomx/engram-backend:sha-e4f68ee`;
- image id is
  `sha256:fb3fc25a94cfa83e11af9f2a929a51625c14ae2ce7eb2f3e1f280df8dab73145`;
- repository digest is
  `sha256:42d532b69a8d3c38095c84f69eed49ecdfe988892aeb6bc209023d5905e3992f`;
- revision is `e4f68eeac2e571e7b1d8442bf61c54f06221070c`;
- PID 1 is `sh -ec`; its Celery child command targets only `engram-batch`
  with prefetch multiplier one;
- restart policy is `unless-stopped`; the container is on `engram_default`
  with its recorded container-name and `worker-batch` aliases;
- four expected workers reply to ping/active/reserved/scheduled, but the batch
  node does not;
- those four workers report no active, reserved, or scheduled tasks;
- `engram-batch` most recently had 307 ready, zero unacknowledged, and zero
  consumers;
- the batch log reports broker connection loss followed by disabled autonomous
  reconnect and shutdown, while the container remained running;
- Docker reports `OOMKilled=true`, restart count zero, and no resource limit;
- the worker uses about 1.955 GiB on a four-CPU host with about 2.121 GiB
  available and no swap;
- effective `task_acks_late` is `False`.

All counts and identities must be refreshed immediately before action. Drift is
a stop, not permission to adapt this procedure.

## Preconditions

The repair may start only if read-only checks prove:

- the exact container id, image id/digest, revision, command, restart policy
  `unless-stopped`, network `engram_default`, recorded aliases, and intended
  `engram-batch` queue still match this record;
- batch remains absent from the complete five-node control reply set;
- exactly the other four expected nodes reply;
- `engram-batch` has zero unacknowledged messages and zero consumers; its ready
  count is refreshed and may be zero or non-zero because restoring a missing
  future consumer is required in either case;
- the batch container has exactly one direct child of PID 1, and that child's
  NUL-delimited command tokens, executable, parent pid, and process start time
  exactly match the recorded Celery batch worker identity with no extra args;
- the batch child has not already entered a detectable termination state;
- Docker reports no OOM/forced-kill state; the current production value fails
  this condition;
- RabbitMQ remains application-ready and host memory remains inside the
  separately reviewed safe envelope for restarting this exact worker;
- the non-secret intent ledger is durably written before the signal.

Do not inspect or record env values, task payloads, message bodies, credentials,
or full container configuration.

## Mutation

Blocked by the Decision above. The following rejected procedure must not be
run, even if its other preconditions later appear green.

Perform exactly one initial mutation: send `SIGTERM` inside the existing
container to the single validated Celery child, not to shell PID 1.

One Python process inside the container must derive the child from
`/proc/1/task/1/children`, require one numeric pid, open a pidfd, and compare the
exact ordered NUL-delimited command tokens, `/proc/<pid>/exe`, parent pid,
process state, and start-time field to the just-recorded identity. It then uses
`signal.pidfd_send_signal(..., SIGTERM)` on that pidfd. If pidfd signaling is
unavailable, identity changes, or any extra/different token appears, it stops
without a numeric-pid fallback. It must not use a name-wide `pkill`,
`docker kill`, `docker stop`, `docker restart`, or any kill signal other than
`TERM`.

Then observe for at most the existing 12-minute worker grace budget:

- the Celery child exits gracefully;
- shell PID 1 is observed to exit as its child terminates; a surviving shell is
  a hard stop, not evidence that restart policy will act;
- Docker's existing `restart: unless-stopped` policy either restarts the same
  container id or leaves that exact container stopped;
- no OOM, forced kill, or changed image/config is observed.

If the exact container becomes stopped and restart policy does not restart it,
one separately journaled fallback is allowed: `docker start` that same exact
container id. No recreate or replacement is allowed.

## Success Evidence

Success requires all of:

- same container id, image id/digest, revision, command, network, and queue
  binding;
- observed exit of both the original Celery child and original shell PID 1;
- a later start timestamp or incremented restart count consistent with the
  recorded action;
- five exact worker ping and active-queue replies, including one batch node
  consuming only `engram-batch`;
- batch queue consumer count one and zero unacknowledged messages at the
  observation boundary;
- refreshed ready counts plus outbox/dead-letter counts recorded over a bounded
  observation interval;
- API and infrastructure health unchanged.

Ready work may begin draining because the legitimate consumer is restored.
Message completion is observed, not forced or rewritten.

## Stop Conditions

Stop without another mutation if:

- any identity, command, child-process, queue, or health precondition is
  ambiguous;
- more than one child exists or the child command differs;
- `engram-batch` has an unacknowledged message before the signal;
- graceful child exit does not complete within 12 minutes;
- the child exits but shell PID 1 remains alive;
- Docker reports OOM, a forced kill, image/config drift, or a new container id;
- the exact old container restarts but the fifth reply/consumer does not return;
- success would require `SIGKILL`, container recreation, queue purge, message
  mutation, outbox/database work, secret access, or another service action.

## Review And Recording Gate

Before execution, this procedure requires independent correctness review and a
fresh Fable adversarial review. After execution, append exact UTC timestamps,
sanitized commands, exit codes, pre/post identities and counts, restart/health
observations, and reviewer dispositions. D2 remains blocked until a fresh
reviewer verifies the completed evidence.
