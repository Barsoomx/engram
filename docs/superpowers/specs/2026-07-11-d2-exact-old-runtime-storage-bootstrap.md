# D2 Exact-Old-Runtime Storage Bootstrap Refinement

Date: 2026-07-11

Status: implementation-ready refinement; production blocked on bounded batch recovery

Refines:

- `docs/superpowers/specs/2026-07-10-runtime-durability-prerequisite.md`;
- `docs/superpowers/specs/2026-07-10-cp1-1-loss-aware-rollout.md`.

## Goal

Close D2 on the exact currently deployed application revision without changing
the API, database schema, task payloads, routing, or provider behavior. Adopt
the existing RabbitMQ state and Beat schedule into attempt-specific Docker
named volumes, recreate only the old async runtime under the merged D1 process
contract, and retain a deterministic abort path to the original runtime.

This refinement exists because the read-only production preflight found a
RabbitMQ bind directory and a Beat file in the container writable layer, while
the prerequisite spec described both sources as anonymous volumes. The source
type changes the copy mechanism, not the D2 safety invariants.

## Authority And Scope

On conflict, this document refines only D2 source discovery, copy, activation,
and recovery mechanics. The prerequisite and rollout specs continue to own all
other gates.

D2 may change only:

- the RabbitMQ container and its storage attachment;
- the five workers, relay, and Beat containers;
- the Beat storage attachment;
- non-secret D2 state, evidence, and a dedicated target Compose project.

The existing API, frontend, PostgreSQL, Redis, database rows, broker messages,
outbox rows, task payloads, secrets, and original source storage are not D2
mutation targets. The API remains running and untouched while durable outbox
rows accumulate during the broker interval.

This is a narrow operational controller, not a reusable deployment framework.

## Verified Starting Shape

The 2026-07-11 read-only preflight observed:

- Compose project `engram`, working directory `/opt/engram`, and one implicit
  network `engram_default`;
- backend revision `e4f68eeac2e571e7b1d8442bf61c54f06221070c`,
  backend image id
  `sha256:fb3fc25a94cfa83e11af9f2a929a51625c14ae2ce7eb2f3e1f280df8dab73145`,
  and immutable repository digest
  `sha256:42d532b69a8d3c38095c84f69eed49ecdfe988892aeb6bc209023d5905e3992f`;
- RabbitMQ bind source `/opt/engram/volumes/rabbitmq` mounted at
  `/var/lib/rabbitmq`, nodename `rabbit@engram-rabbitmq`, hostname
  `engram-rabbitmq`, and Mnesia directory
  `/var/lib/rabbitmq/mnesia/rabbit@engram-rabbitmq`;
- Beat schedule `/srv/app/celerybeat-schedule` in the old Beat container and no
  Beat mount;
- shell PID 1 in every old async application container;
- four of five expected Celery worker replies; the batch worker is absent,
  `engram-batch` has ready messages and no consumer, and the old image reports
  disabled broker reconnect after connection loss.

These values are discovery evidence, not hard-coded controller defaults. D2
must capture them again from live state. A changed value is a new preflight and
review decision, never an inferred continuation.

The missing batch reply is a current hard stop. No D2 mutation may start until
a separately recorded exact-old-runtime repair restores all five replies and
the ordinary D2 preconditions below pass.

## Source Descriptors

This production D2 controller accepts exactly the verified source shape:

```text
RabbitSource = BindDirectory(canonical_host_path)
BeatSource   = ContainerFile(container_id, absolute_path)
```

Discovery comes only from selected `docker inspect` fields and exact configured
paths. The controller rejects:

- zero or multiple mounts at the expected target;
- a bind path that is relative, missing, symlink-resolved to a different path,
  or different from the inspected container source;
- a Beat path that is missing, not a regular file, or has sibling database
  files the controller cannot enumerate and validate;
- a Docker volume, anonymous volume, Beat bind, or any other runtime source type;
- a live source that differs from the journaled container id, image, mount,
  nodename, hostname, Mnesia directory, file metadata, or queue topology.

The production Rabbit source is copied as an exact bind directory, not
relabeled as an anonymous volume. The production Beat source is copied
read-only from the stopped old container into a host attempt snapshot before
it is written to the target volume. A different future source shape requires a
new reviewed refinement rather than an unused generic branch in this controller.

## Immutability And Identity

`PRECHECK` records provisional source identities only. Clean shutdown is
allowed to flush authoritative state. After old Beat exits gracefully, the
controller captures its final schedule snapshot and freezes that snapshot.
After every client is quiesced, the controller captures the final logical
Rabbit topology/count baseline while Rabbit is still running, then stops Rabbit
cleanly, captures its final filesystem manifest, and freezes that bind source.
From each final capture boundary the controller does not mutate that source
unless a journaled pre-authority abort explicitly transfers authority back:

- no helper receives a writable source mount;
- no `docker cp` targets a source container;
- no source path is deleted, renamed, chowned, chmodded, or reused as a target;
- the old source containers receive no `exec`, copy-in, or writable-layer
  mutation after they stop;
- the final source and snapshot metadata/content digests are reconciled at
  every later phase boundary while frozen, and any drift is an abort condition
  before the authority barrier or a target-only recovery stop afterward.

The old containers remain stopped as rollback anchors until the authority
barrier. Before directly signaling shell-wrapped old async children, D2
journaledly changes only their restart policy from `unless-stopped` to `no` so
Docker cannot relaunch the unsafe source command. Abort restores the recorded
policy before explicit source start. The other permitted metadata change is a
journaled disconnect/reconnect of the old Rabbit container from
`engram_default`, because the `rabbitmq` network alias must have exactly one
owner during target activation. Container id, image, command, environment,
writable layer, name, labels, and source mounts remain unchanged.

The target runs as the dedicated Compose project `engram-d2`. Its Compose file
creates an isolated verification network for target Rabbit and declares the
existing `engram_default` network as external for target workers, relay, and
Beat. After offline verification, the controller journaledly connects target
Rabbit to `engram_default`; Compose does not attach it there early. This avoids
replacing or relabeling the old `engram` project containers. The target Rabbit
alone owns aliases `rabbitmq` and `engram-rabbitmq` while active. Target
workers, relay, and Beat need no inbound production alias.

## Attempt-Specific Named Targets

Each attempt creates fresh, explicit Docker volumes:

```text
engram_d2_rabbitmq_<attempt-id>
engram_d2_beat_<attempt-id>
```

The attempt id is a controller-generated lowercase hexadecimal identifier
stored before volume creation. The controller passes the exact names to the
tracked D2 Compose file as external volumes. It never relies on implicit
project volume naming.

Every target volume receives immutable non-secret labels for checkpoint,
attempt id, and source class at creation. Lifecycle state (`copying`,
`verified`, `active`, or `quarantined`) lives only in the atomic journal and
evidence ledger because Docker volume labels cannot be updated in place. A
crash, failed copy, failed verification, or abort leaves the volume present and
records it as quarantined. A quarantined or partially populated volume is never
deleted, emptied, or reused. Resume may continue only the same journaled
attempt after reconciling its exact contents; otherwise a new attempt gets new
target names.

No D2 command uses `docker compose down -v`, `docker volume rm`, queue purge,
outbox mutation, database restore, or source-path cleanup.

## Exact-Old Target Runtime

The tracked target file is a narrow Compose overlay applied after the exact
hashed live `/opt/engram/docker-compose.yml`. The overlay contains overrides
only for RabbitMQ, the five workers, relay, and Beat; it reuses the live file's
existing secret/env wiring without the controller reading, copying, or printing
values. Every Compose call supplies an explicit allowlisted target service and
`--no-deps`, so the base file's API, frontend, PostgreSQL, and Redis definitions
cannot be created or changed by D2. The live base file itself is never edited.

Before mutation, the controller records and requires:

- one immutable backend repository digest and image id shared by API, all five
  workers, relay, and Beat;
- the exact application revision label;
- the exact RabbitMQ image id and repository digest;
- the existing Compose file and Rabbit configuration hashes;
- exact old commands as source evidence, selected restart policies, network
  aliases, and mount descriptors.

Target application containers use that immutable old backend digest. Their
commands intentionally differ from the recorded shell-wrapped source commands:
the only command transform is the exact reviewed D1 direct-command contract,
plus D1 `SIGTERM`, grace periods, and `restart: unless-stopped`. The target API
is absent; the original API is never created, stopped, or recreated by the D2
project.

D2 evaluates and records only
`broker_connection_retry_on_startup`, `broker_connection_retry`, and
`broker_connection_max_retries` from the fully loaded Celery app configuration.
During precheck, a one-shot exact-image Python probe receives the existing env
file from Docker, runs with no network, starts no worker or consumer, imports
`engram.celery_app`, and prints only a three-key JSON object. This observes the
effective values after the image's ordinary configuration precedence without
reading or printing env values. After target workers start, the same selected
probe runs inside one target container and must match the precheck record. The
actual values are evidence, not a D2 enablement gate: old workers are started
explicitly after Rabbit verification. D2 claims no autonomous reconnect
behavior; D3 owns that target-runtime proof.

## Beat Adoption

After the old Beat is stopped, the controller:

1. enumerates the exact schedule path and all database sidecar files matching
   that basename;
2. copies them out of the old container into the attempt directory without a
   copy-in or container start;
3. records owner, group, mode, size, and a content digest without logging file
   content;
4. opens the copied schedule read-only with the exact old application image and
   proves the scheduler database is parseable;
5. copies the immutable snapshot into the fresh named Beat volume while
   preserving metadata;
6. reopens the target copy read-only and proves the same metadata, digest, and
   scheduler snapshot before Beat can start.

A missing, corrupt, or ambiguous schedule is a hard stop. D2 does not invent a
schedule, reset it, or perform catch-up. Any future reinitialization requires a
separate due-boundary and idempotent-catch-up review.

The final stopped schedule snapshot also records the exact-old scheduler's
earliest next-due UTC horizon and a reviewed safety margin. The controller
recomputes that horizon from the unchanged snapshot immediately before either
old-source abort start or target Beat start. Crossing the horizon before the
authority barrier requires abort or a new reviewed decision; crossing it after
the barrier requires target-only recovery and never revives the stale old Beat.

## Rabbit Copy And Verification

The Rabbit copy helper uses the already present exact RabbitMQ image, no
network, a read-only root filesystem, a read-only source mount, and one writable
fresh target mount. It preserves numeric ownership, permissions, symlinks, and
file contents. No helper prints the Erlang cookie or broker data.

Offline verification compares source and target tree manifests and content
digests, with a content-only equality check for the Erlang cookie and separate
owner/group/mode evidence. It then starts target Rabbit on an isolated D2
verification network with the exact hostname/nodename pair. Before production
network attachment it proves:

- the exact nodename and Mnesia directory;
- the same vhost, definitions, queue names/types, bindings, ready counts, and
  unacknowledged counts;
- the same cookie content, owner, group, and mode without printing content;
- no blank-node initialization when source state exists.

Only a verified target may replace the old Rabbit network alias. The old Rabbit
is stopped before copy and remains stopped until abort or observation closure.

## Controller And Durable State

The controller is a Python standard-library CLI under `scripts/` with
adjacent pytest function tests. It reuses the bounded subprocess, selected
inspect, redaction, deadline, stop, Rabbit-state, and Beat-snapshot patterns
from `scripts/e2e_runtime_durability.py`; it does not create a generic runner or
deployment abstraction.

It holds a non-blocking exclusive `fcntl.flock` on a root-owned state file. The
non-secret JSON journal is written in the same directory by:

1. writing a new temporary file;
2. flushing and `fsync`ing the file;
3. atomically replacing the journal;
4. `fsync`ing the containing directory.

A missing journal starts a new attempt only at `PRECHECK`. Malformed JSON,
unsupported schema, a missing required identity, or a journal/live mismatch is
a hard stop. The controller never guesses the last successful action.

The top-level phases remain:

```text
PRECHECK
  -> OLD_ASYNC_STOP_INTENT -> OLD_ASYNC_STOP_COMPLETE
  -> RABBIT_STOP_INTENT -> RABBIT_STOP_COMPLETE
  -> RABBIT_COPY_INTENT -> RABBIT_COPY_COMPLETE
  -> BEAT_COPY_INTENT -> BEAT_COPY_COMPLETE
  -> NAMED_RABBIT_START_INTENT -> NAMED_RABBIT_VERIFIED
  -> RABBIT_ALIAS_CUTOVER_INTENT -> RABBIT_ALIAS_CUTOVER_COMPLETE
  -> TARGET_AUTHORITY_COMMIT_INTENT -> TARGET_AUTHORITY_COMMITTED
  -> OLD_ASYNC_RESTORE_INTENT -> OLD_ASYNC_RESTORED
  -> COMPLETE
```

`NAMED_RABBIT_*` starts and verifies target Rabbit only on the isolated
verification network. `RABBIT_ALIAS_CUTOVER_*` records and reconciles the old
Rabbit production-network disconnect, target production-network connect, exact
two aliases, and proof that no other container owns either alias. A crash
between disconnect and connect deterministically resumes the same cutover or
enters abort from the two selected network-attachment records.

`TARGET_AUTHORITY_COMMIT_*` is an irreversible transport-authority barrier.
Immediately before it, target workers, relay, and Beat are absent; no producer
or consumer is connected to target Rabbit; copied queue/topology/count state and
the final Beat snapshot still match; and the due horizon remains safe. Before
the committed record, a journaled abort may transfer authority back, unfreeze,
and reattach the exact old runtime. After the committed record, no old Rabbit,
worker, relay, or Beat source may run again: every crash, failed start, or
observation failure preserves and recovers forward using only the named target
services and volumes. This prevents an acknowledged target message, confirmed
relay publish, or Beat tick from forking authority back to a stale source.

Each phase contains an ordered list of named external actions. The journal
persists an action intent before invocation and its selected observed result,
exit code, and completion timestamp after reconciliation. A process crash
releases the host lock but leaves the intent durable. Resume inspects live state
and either records the already completed action, safely performs the still
absent action, or enters abort. It never repeats a non-idempotent action based
only on an exit code.

Command evidence stores an operation label and redacted/bounded selected output,
not raw command lines containing values. The journal and evidence must not
contain env values, credentials, cookie content, message payloads, task args,
or full container configuration.

## Precheck And Quiescence

`PRECHECK` is read-only and requires:

- all expected containers, images, revisions, source descriptors, hashes,
  network identities, and target names are unambiguous;
- exactly five expected worker ping, active-queues, registered, active,
  reserved, and scheduled replies;
- each worker consumes only its expected queue;
- Beat schedule validation succeeds and the initial due-horizon estimate is
  outside the reviewed safety margin;
- outbox identity/dead-letter baselines and all Rabbit queue/binding/count
  baselines are captured;
- every discovered `celery_delayed_*` queue has zero total messages; D2 does not
  attempt time-transition reconciliation for delayed broker state;
- no old async container reports OOM/forced-kill state or an unsafe runtime
  precondition;
- no earlier non-complete D2 journal or unclassified D2 target exists.

Ready messages may be non-zero. They are durable copy input and must match
exactly after target Rabbit starts. The zero condition applies to active,
reserved, scheduled, and unacknowledged work, not ready messages.

After precheck, `OLD_ASYNC_STOP_INTENT` records the exact children and original
restart policies, changes the old async restart policies to `no`, and then:

1. signals the exact old Beat child, waits for its handler and shell/container
   exit, and captures the final Beat snapshot and due horizon;
2. signals the exact relay child, waits for its package drain and
   shell/container exit, then records the complete outbox identity set as
   `(id, task_id, task_name, schema_version)` plus the dead-letter baseline;
3. persists every worker-node/queue cancellation intent, issues targeted
   `cancel_consumer`, requires every acknowledgement and absence of each queue
   from `active_queues`, and refuses partial cancellation;
4. requires two identical samples at least five seconds apart with active,
   reserved, scheduled, and broker-unacknowledged counts zero, native-delay
   totals zero, and application ready counts stable;
5. signals each exact worker child, waits for its handler and shell/container
   exit, and proves no forced kill before Rabbit stop.

Each shell-wrapped source stop derives exactly one direct child, verifies its
ordered NUL-delimited command tokens, executable, parent pid, state, and start
time, opens a pidfd, revalidates identity, and sends only `SIGTERM` through the
pidfd. A missing reply is never idle. A shell that survives its child, an
automatic restart, OOM, or any need for `SIGKILL` is a hard stop.

Only after those steps does D2 capture the final logical Rabbit topology,
bindings, ready/unacknowledged counts, and native-delay census while Rabbit is
still running. It then stops Rabbit cleanly and captures the authoritative
filesystem manifest/digest. Those paired records are the copy baseline;
precheck counts are not.

## Restore, Abort, And Observation

After `TARGET_AUTHORITY_COMMITTED`, successful forward restore starts target
workers explicitly, proves five exact worker replies and queue bindings, then
opens a read-only repeatable-read PostgreSQL transaction and records its MVCC
snapshot plus every visible outbox identity as the relay restart cutoff. It
starts relay and reconciles every cutoff identity to still-present package
state, package-owned confirmed-publish deletion, or explicit dead letter;
post-cutoff rows cannot substitute for a missing identity. It starts exactly
one target Beat last only after revalidating the due horizon.

Restore also proves the ready-message baseline, no unexpected
unacknowledged/native-delay work, direct PID 1, stop-grace configuration,
restart policy, target volume ids, and Beat schedule continuity. Equal outbox
or dead-letter counts alone are never accepted.

Before `TARGET_AUTHORITY_COMMITTED`, abort uses its own intent/completion
actions. Its authority-transfer intent explicitly unfreezes the old source only
after target producers/consumers are proven absent:

1. stop target Beat, relay, workers, and Rabbit gracefully;
2. disconnect target Rabbit from `engram_default`;
3. leave target volumes quarantined and present;
4. reconnect the exact old Rabbit container with its recorded aliases and start
   it against the untouched source;
5. prove original nodename, topology, counts, ownership, and readiness;
6. explicitly restart original workers, then relay, then Beat;
7. prove five worker replies, one Beat, queue bindings/counts, and outbox/dead-
   letter reconciliation.

After `TARGET_AUTHORITY_COMMITTED`, this old-source abort path is forbidden.
Recovery stops target consumers/publishers as needed but uses only target
workers, relay, Beat, and the same named Rabbit/Beat volumes until the target
restore proofs pass.

The observation window is target-authoritative and begins at the authority
barrier. The stopped source containers, source storage, and Beat snapshot remain
present as forensic artifacts after D2 closure, but never become runtime
authority again; cleanup is not part of this checkpoint.

## Verification

### Unit and contract tests

- bind-directory/container-file discovery and rejection of every other source
  shape;
- exact digest/revision and selected effective-config validation;
- lock exclusion, atomic write ordering, corrupt-journal hard stop;
- intent-before-mutation and crash reconciliation for every action;
- final source capture occurs after clean Beat/Rabbit shutdown, not precheck;
- per-node cancellation acknowledgement, empty active queues, two stable idle
  samples, and empty native-delay buckets;
- direct child pidfd shutdown under disabled source restart policy with no
  shell survivor or forced kill;
- irreversible authority commit forbids old-source rollback before target work;
- source mounts are read-only and target names are attempt-specific;
- partial destinations quarantine and are never deleted/reused;
- Rabbit identity/topology/count and Beat metadata/digest mismatches stop;
- missing worker reply is not idle; ready messages may remain while unacked may
  not;
- relay cutoff identities reconcile by identity rather than count;
- Beat due horizon is revalidated at abort/target start;
- target Compose contract excludes API and uses exact-old image, direct PID 1,
  reviewed grace, restart policy, external network, and external named volumes;
- command/output redaction and explicit forbidden-command tests.

### Disposable rehearsal

Use a disposable source project shaped like production: Rabbit on a bind
directory, Beat in a container writable layer, shell-PID-1 old async containers,
and a distinct target Compose project. Rehearse:

- successful bind-to-named Rabbit and container-file-to-named Beat adoption;
- wrong nodename, blank target, altered ownership, changed count/topology, and
  corrupt Beat rejection;
- controller termination immediately before and after every external mutation,
  including partial copy, target Rabbit start, alias cutover, and authority
  commit;
- deterministic same-attempt resume where safe, old-source abort only before
  authority commit, and target-only forward recovery afterward;
- original source remains attachable and every partial target remains present;
- a durable ready message survives while consumers are stopped;
- pre-authority abort restores the exact source runtime without queue purge or
  database work, while post-authority faults never start the old source.

### Production gate

Production D2 runs only after unit/contract tests, disposable crash rehearsal,
independent correctness and simplicity reviews, fresh Fable adversarial review,
and current-head CI are green. It records exact commands, exit codes,
timestamps, identities, counts, journal transitions, abort-rehearsal evidence,
and post-bootstrap observation without secrets.

## Stop Conditions

In addition to the prerequisite spec, stop before mutation or enter abort when:

- the batch or any other expected worker reply is missing;
- any old async container reports OOM/forced-kill state or cannot be stopped by
  exact-child `SIGTERM` plus observed shell/container exit;
- the source type/path/volume/container file differs from its journaled
  descriptor;
- the exact image digest, revision, Compose hash, Rabbit config hash, nodename,
  hostname, Mnesia directory, network, or source container id changes;
- target project isolation or exclusive Rabbit network alias ownership cannot
  be proved;
- the controller's source non-mutation controls or boundary drift checks fail;
- the controller would need a volume delete, source write, `down -v`, queue
  purge, outbox mutation, database restore, secret print, or `SIGKILL`;
- resume cannot distinguish absent, completed, and partially completed action
  state from selected live evidence;
- native-delay totals are non-zero, consumer cancellation is partially
  acknowledged, stable idle samples differ, an outbox cutoff identity is
  unexplained, or the Beat due horizon is crossed.

## Closure

D2 closes only when production uses the attempt-specific named Rabbit and Beat
volumes on the exact old application revision; the target async services satisfy
the D1 direct-PID-1, grace, and restart contract; Rabbit identity/topology/counts
and Beat continuity match; all five workers re-register explicitly; source and
abort artifacts remain intact; and the complete non-secret evidence ledger is
recorded. C1.1 production mutation remains blocked until then.
