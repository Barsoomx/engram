# Checkpoint 2 Leases And Reconciliation

Date: 2026-07-11; status: proposed focused specification for C2.1-C2.3
Roadmap gate: Checkpoint 2; baseline: `master` at `79ddb15a5d872f963c6464847c800b798d78caef`

Dependencies: `docs/decisions/2026-07-10-domain-progress-and-transport.md`, the
complete CP1 spec, and the tracked memory-loop invariants/fault matrix.

## Goal

Make required `WorkflowWork` recoverable independently of one Celery delivery.
A bounded fenced lease and append-only `WorkflowRun` record each execution;
reconciliation starts from scoped domain invariants, never queue depth.

This development-only spec authorizes schema, service, adapter, query, command,
and test changes in CI, not production access/deploy/migrations, Beat,
historical repair, or dead-letter mutation.

## Current Baseline And Hard Dependency

The baseline has C1.1 identity (`WorkflowWork` and nullable
`WorkflowRun.work`), not completed CP1. Only `process_observation_work_v1`
executes; session/digest adapters fail closed, legacy schedules and
`RetryFailedDistillations` use mutable inputs/historical-success heuristics,
and C1.2/C1.3 producer, sequence, lifecycle, digest, and task cutovers are
unmerged.

No C2 implementation slice may start until C1.2, C1.3a, C1.3b, C1.3c, and
C1.3d have merged serially and their Checkpoint 1 gate is recorded. In
particular, all new producers must emit the four versioned id-only work tasks,
session sequences must be contractual, the legacy-producer census must be
empty, and pre-C2 queued/running workers and packages must be drained.

Additive schema does not make unfinished CP1 writers safe. A branch from this
baseline stops after spec/test work and never simulates missing CP1 behavior.

## Success Boundary

Checkpoint 2 is complete in development when:

- concurrent delivery creates one current lease and fences an expired owner;
- every v1 execution has a durable run, owner, token, start, heartbeat/expiry,
  finish, and typed outcome without rewriting earlier runs;
- provider and infrastructure failures create bounded-delay logical retry;
- configuration failure blocks without semantic rejection and resumes only
  after its non-secret configuration fingerprint changes;
- deterministic invalid input becomes a visible terminal operational failure;
- exact current session generations cannot be hidden by older success;
- candidate reconciliation consumes the canonical CP3/CP5 work builder without
  owning candidate input identity or executing the semantic state machine;
- scoped session/candidate/projection/package reports are deterministic and
  concurrent reconciliation converges on one queued attempt per grace window;
- P3/P4 are exact, P6 is builder-aware, and P13 stays partial until CP10;
- `django-celery-outbox` remains the only delivery, relay, retry, and
  dead-letter authority.

## Non-Goals

Checkpoint 2 does not:

- add a broker, relay, Engram outbox, package-status mirror, delivery receipt,
  transport retry table, dead-letter replay, or queue poller;
- infer completion, need, or failure from an empty package table;
- add CP3 chunk/stage/coverage or execute CP5 candidate semantics;
- rebuild retrieval projections or add projection work; CP4 owns that contract;
- reinterpret, relink, delete, or bulk-repair historical `WorkflowRun`,
  candidate, memory, package, or dead-letter rows;
- expose public/mutating/global repair surfaces or hold a database lock across
  a provider/broker call;
- change product disposition because an operational attempt failed.

## Serial Delivery Spine

Only one C2 slice is mutable at a time. Each slice merges and closes its CI
gate before the next begins.

1. **C2.1 - execution claim and fencing.** Add the minimal execution fields,
   typed failure contract, claim/heartbeat/fence/finish services, and versioned
   task-adapter integration.
2. **C2.2 - session-work reconciliation.** Replace historical-run heuristics
   with exact current-generation findings and idempotent due-attempt signaling.
3. **C2.3 - candidate/projection/transport audit.** Add disjoint read-only
   inspectors, the CP3/CP5 candidate-builder seam, scoped command, and metrics.

There is no stacked implementation. C2.1 merges before C2.2 touches task or
reconciler integration; C2.3 then consumes the stable execution contract.

## Authority Boundary

`WorkflowWork` owns current operational state, fencing token, lease owner/time,
retry/streak, and blocked-configuration fingerprint. `WorkflowRun` owns one
immutable attempt's contract/origin, token/owner/times, typed failure, bounded
redacted detail, provider/results, request/correlation, and rerun lineage.

`django-celery-outbox` owns package persistence, publication, broker retry,
dead letters, replay, purge, relay locks, and relay metrics. C2 may read package
dead letters in a scoped comparison and may create a new package only by
calling the existing id-only Celery task boundary inside the domain
transaction. It never updates or deletes package-owned rows.

## C2.1 Additive Data Contract

### Enums

Add these choices beside the existing workflow choices:

```text
WorkflowWorkExecutionState
  ready
  leased
  retry_wait
  blocked
  terminal_failure
  settled

WorkflowRunOrigin
  legacy
  automatic
  reconciliation
  manual

WorkflowRunFailureClass
  worker_lost
  infrastructure_transient
  provider_transient
  configuration
  invalid_input
  unexpected
```

`WorkflowWork.disposition` remains the product state. Execution state is
orthogonal: a provider outage never changes `required` to `complete`, and an
explicit manual attempt may temporarily lease already-complete work.

### WorkflowWork Fields

Migration `0035_workflow_work_execution.py` depends on the final CP1 contract
migration `0034_memory_loop_input_contract.py` and adds:

| Field | Type | Initial/default contract |
|---|---|---|
| `execution_state` | char(24), choices, default `ready` | required rows become `ready`; complete/no-op rows become `settled` |
| `fencing_token` | positive bigint | 0; incremented exactly once per successful new claim |
| `lease_owner` | char(255), blank | empty outside `leased` |
| `lease_expires_at` | nullable timestamp | non-null only while `leased` |
| `heartbeat_at` | nullable timestamp | non-null only while `leased` |
| `next_retry_at` | nullable timestamp | non-null only in `retry_wait` |
| `failure_streak` | positive integer | 0; increment on retry-scheduled/blocked/terminal failure, reset on successful product completion or changed-config resume |
| `blocked_configuration_fingerprint` | char(64), blank | lowercase SHA-256 only in `blocked` |

Database checks enforce:

- `leased` has non-blank owner, non-null heartbeat/expiry, expiry greater than
  heartbeat, null retry time, and blank blocked fingerprint;
- `retry_wait` has cleared lease fields, non-null retry time, and blank blocked
  fingerprint;
- `blocked` has cleared lease/retry fields and a lowercase 64-hex blocked
  fingerprint;
- `ready`, `terminal_failure`, and `settled` have cleared lease, retry, and
  blocked fields;
- `settled` requires product disposition other than `required`;
- `terminal_failure` requires product disposition `required`;
- `fencing_token` and `failure_streak` are non-negative.

Add indexes:

- `(organization, project, execution_state, next_retry_at)`;
- `(organization, project, work_type, execution_state)`;
- `(execution_state, lease_expires_at)`.

Existing immutable identity/snapshot fields remain immutable. Direct model
save is not the execution transition API.

### WorkflowRun Fields

The same migration adds:

| Field | Type | Contract |
|---|---|---|
| `execution_contract_version` | positive small integer | persistent database/Python default 0 for rolling compatibility; every C2 claim writes 1 |
| `origin` | char(24), choices | persistent default `legacy`; C2 code writes an explicit non-legacy value |
| `fencing_token` | nullable positive bigint | copied from current work token on claim |
| `lease_owner` | char(255), blank | immutable after claim |
| `dispatched_at` | nullable timestamp | set when C2 creates/re-signals a queued attempt |
| `lease_expires_at` | nullable timestamp | last expiry owned by this attempt |
| `heartbeat_at` | nullable timestamp | claim time, then last accepted heartbeat |
| `failure_class` | char(32), choices, blank | required for a failed v1 attempt |
| `failure_code` | char(128), blank | stable machine code, never free-form provider text |
| `configuration_fingerprint` | char(64), blank | fingerprint observed by this attempt; set for configuration failure |

`failure_reason` remains a redacted, truncated diagnostic string. Control flow
never parses it.

For `execution_contract_version=1`, checks enforce:

- queued: null token/lease/start/finish, blank failure fields, non-null
  `dispatched_at`;
- running: positive token, non-blank owner, non-null start/heartbeat/expiry,
  null finish, and blank failure fields;
- succeeded: positive token, owner/start/finish present, blank failure fields;
- failed: positive token, owner/start/finish present, and non-blank typed
  failure class/code;
- configuration fingerprint is blank unless failure class is `configuration`,
  where it is lowercase 64-hex;
- `(work, fencing_token)` is unique when both are non-null and the run is v1;
- at most one v1 `running` run exists per work.

Legacy/unlinked rows stay version 0 and remain readable. The migration does not
derive typed failures from free-form text. Activation requires zero version-0
linked queued/running rows; it fails closed rather than leasing them.

Add indexes:

- `(work, status, created_at)`;
- `(work, fencing_token)`;
- `(organization, project, failure_class, finished_at)`.

### Lease Durations And Owner Identity

The execution registry uses these v1 lease durations:

| Work type | Lease seconds |
|---|---:|
| observation processing | 120 |
| session distillation | 720 |
| daily digest | 240 |
| weekly digest | 240 |

Each exceeds the corresponding current hard task limit by at least 30 seconds.
Long adapters heartbeat before each provider stage or durable chunk. No lease
is refreshed by an untrusted payload.

The task adapter builds an owner as
`<celery-hostname>:<worker-pid>:<random-delivery-uuid>` and truncates only after
preserving the UUID suffix. Tests inject owner and time; domain services never
read process globals.

### Configuration Fingerprint

`execution_configuration_fingerprint(work)` returns SHA-256 of canonical
`execution_configuration/v1` containing only:

- work type, organization/project/team ids, and task type;
- resolved active `ModelPolicy` id, version, provider, model, and `updated_at`,
  or a deterministic sorted description of the missing/ambiguous policy rows;
- selected `ProviderSecret` id, current version, active/rotation state, and
  `updated_at`, or an explicit missing marker;
- active envelope id, version, key version, and `updated_at`, or an explicit
  missing marker;
- relevant `OrganizationSettings.updated_at` and execution contract version 1.

It excludes ciphertext, HMAC, API keys, secret fingerprints, prompts, provider
responses, and unrestricted metadata. A configuration failure stores this
fingerprint on work and run. A blocked work resumes only when a later scoped
fingerprint differs; elapsed time alone never resumes it.

### Exact Failure Classification And Action

Adapters translate failures to a typed `ClassifiedWorkFailure` at the boundary;
there is no message substring classifier.

| Class | Exact source boundary | Work action while product is required |
|---|---|---|
| `worker_lost` | lease expires before a valid finish; code `lease_expired` | immediate reclaim by a delivered task, otherwise retry wait with zero delay |
| `infrastructure_transient` | typed DB/network/timeout; codes `database_unavailable`, `dependency_timeout`, `dependency_unreachable` | retry wait, base 30 seconds, cap 1,800 seconds |
| `provider_transient` | timeout/unreachable or HTTP 408/425/429/5xx; codes `provider_timeout`, `provider_unreachable`, `provider_rate_limited`, `provider_unavailable` | retry wait, base 30 seconds, cap 1,800 seconds |
| `configuration` | missing policy/secret, scope/endpoint fault, or HTTP 401/402/403/404; codes `model_policy_unavailable`, `provider_secret_unavailable`, `policy_scope_invalid`, `provider_endpoint_invalid`, `provider_account_unavailable` | blocked with configuration fingerprint; no timed retry |
| `invalid_input` | deterministic snapshot/scope/fingerprint/provider 4xx; codes `work_contract_invalid`, `work_scope_invalid`, `work_fingerprint_mismatch`, `provider_request_invalid` | terminal operational failure; no automatic retry |
| `unexpected` | any untranslated exception; code `unexpected_exception` | retry wait, base 300 seconds, cap 21,600 seconds |

For a retrying class, increment `failure_streak` to `n` and compute:

```text
delay = min(cap_seconds, base_seconds * 2 ** min(n - 1, 16))
next_retry_at = failure_time + delay
```

Worker loss uses delay zero. There is no retry-count abandonment: the delay is
capped, not the number of durable attempts. A non-required work records the
failed explicit run but returns the work to `settled`; CP2 does not schedule a
new attempt merely to satisfy a manual rerun.

### Public Internal Interfaces

Create `apps/backend/engram/memory/work_execution.py` with these exact public
types and functions:

```python
WorkClaim(work_id: UUID, workflow_run_id: UUID, fencing_token: int,
          lease_owner: str, lease_expires_at: datetime)
ClaimOutcome = claimed | replayed | busy | not_due | blocked | terminal
WorkClaimCompletion = product_succeeded | product_no_signal | continue_required
ClaimResult(outcome: ClaimOutcome, claim: WorkClaim | None)
def claim_work(*, work_id: uuid.UUID, expected_work_type: str,
               lease_owner: str, now: datetime, lease_for: timedelta,
               workflow_run_id: uuid.UUID | None = None) -> ClaimResult: ...
def heartbeat_work(*, claim: WorkClaim, now: datetime,
                   lease_for: timedelta) -> WorkClaim: ...
def lock_work_fence(*, claim: WorkClaim, now: datetime
                    ) -> tuple[WorkflowWork, WorkflowRun]: ...
def finish_work_claim(*, claim: WorkClaim, now: datetime,
                      completion: WorkClaimCompletion,
                      result_memory_id: uuid.UUID | None = None) -> None: ...
def fail_work_claim(*, claim: WorkClaim, now: datetime,
                    failure: ClassifiedWorkFailure) -> None: ...
```

`lock_work_fence` requires an active `transaction.atomic()` and locks the work,
then the matching run. It verifies scope, v1 contract, run link, owner, token,
state, and unexpired lease. Callers must invoke it in the same short transaction
that writes durable semantic output and calls `finish_work_claim`. A stale or
expired token raises `StaleWorkFenceError` before any caller-owned write.
`continue_required` succeeds the run, clears the lease, keeps product work
required/ready, and permits `queue_work_attempt` in the same outer transaction;
it never reuses the finished run or token.

Create `apps/backend/engram/memory/work_failures.py` with
`ClassifiedWorkFailure(failure_class, code, redacted_detail,
configuration_fingerprint='')`, the explicit translator, and the backoff
function. `code` must match `^[a-z0-9_]{1,128}$`.

Create `apps/backend/engram/memory/work_dispatch.py` with:

```python
def queue_work_attempt(*, work_id: uuid.UUID, now: datetime,
                       origin: WorkflowRunOrigin) -> WorkflowRun: ...
```

It locks the scoped work, creates a linked queued v1 run when none is eligible,
calls the existing versioned task with `(work_id, run_id)` inside the same
transaction, and sets `dispatched_at`. If an existing queued reconciliation run
was signaled less than five minutes ago, it returns that run without a package.
If it is older, it re-signals the same run/task id and advances `dispatched_at`.
Package creation failure rolls back the run/timestamp. It never queries a
package row.

### Claim And Concurrency Rules

`claim_work` uses one transaction and this order:

1. require aware `now`, valid UUIDs, and non-blank bounded owner;
2. lock `WorkflowWork` by id and expected type;
3. revalidate organization/project/team and immutable fingerprint contract;
4. lock active/explicit runs in `(created_at, id)` order;
5. return `terminal` for automatic delivery of settled/terminal-failure work;
6. return `not_due` for retry wait before `next_retry_at` and `blocked` when
   the configuration fingerprint is unchanged;
7. if blocked configuration changed, clear block, reset streak, and continue;
8. if an unexpired different owner holds the lease, return `busy`;
9. if the same v1 run/owner/token is replayed, return the same claim;
10. if the old lease expired, fail the old run as
    `worker_lost/lease_expired` before continuing;
11. validate a supplied queued run, or create an automatic v1 run;
12. increment work fencing token, set both rows running/leased, and commit.

Locks are never held across provider calls. Heartbeat, failure, and completion
lock work before run and use the same owner/token predicates. Repeated finish or
failure of the same already-terminal run is idempotent only when its recorded
outcome matches. An older token always fails even if its provider result is
otherwise valid.

Versioned tasks stop calling `self.retry` for domain execution failures.
Legacy task decorators stay registered until CP1's drain gate. A v1 task
records one durable run failure and raises for task observability; the logical
reconciler creates the later attempt after `next_retry_at`.

## C2.2 Session-Work Reconciliation

Create `apps/backend/engram/memory/session_work_reconciler.py`. It does not
reuse `RetryFailedDistillations` or group unlinked runs by mutable JSON.

For each explicitly scoped, ended
`AgentSession.end_work_contract_version=1`, derive the current generation as
the maximum same-scope useful `Observation.session_sequence`; lifecycle rows
are excluded by the trusted CP1 event classification. Generation zero requires
the CP1 no-input work. A matching work must be
`session_distillation/agent_session`, contract 1, same scope/team, and have
exact lower/upper snapshot plus recomputed fingerprint.

The inspector emits these stable finding codes:

| Code | Predicate | Proposed action |
|---|---|---|
| `session_current_work_missing` | current exact generation has no matching work | create/reuse exact CP1 work; never fabricate from an unversioned session |
| `session_current_work_incomplete` | matching latest work is still required, including when older work/run succeeded | execute the latest work only |
| `work_never_claimed` | required ready work has no v1 run after five-minute grace | queue one reconciliation attempt |
| `attempt_signal_stale` | queued v1 run has `dispatched_at` older than five minutes | re-signal the same run id |
| `lease_expired` | leased work expiry is before `as_of` | reclaim/fence through `claim_work`; never update output directly |
| `logical_retry_due` | retry-wait time is at or before `as_of` | queue one new reconciliation run |
| `configuration_blocked` | blocked fingerprint is unchanged | report only |
| `configuration_changed` | blocked fingerprint differs | clear block and queue one run in the locked transaction |
| `terminal_input_failure` | latest required work is terminal operational failure | report only; preserve evidence |

An older completed generation is healthy history, not evidence for the newer
generation. A later failed run is evaluated through its linked work/token, not
hidden by `any(SUCCEEDED)`.

`inspect_session_work(organization_id, project_id, as_of)` is read-only and
returns findings. `reconcile_session_work(..., as_of)` applies only actions
whose subjects carry the exact CP1 post-cutover marker. Every mutation locks the
session, then work, then runs; recomputes the generation after locking; and
uses `create_work`/`queue_work_attempt`. Concurrent reconcilers may return the
same queued run, but create one run/package per five-minute signal window.

Retain `distillation_reconciler.py` as a compatibility adapter only until the
CP1 task/schedule cutover is complete. C2.2 removes its string failure markers,
attempt caps, and historical-success gate from production call sites. Do not
delete the file while a pre-cutover task can import it.

## C2.3 Candidate, Projection, And Transport Inspection

### Candidate Builder Boundary

C2 adds no candidate work type, subject type, marker field, migration, input
builder, or producer. CP3/CP5 own that schema. C2 only consumes this external
protocol from `candidate_work_reconciler.py`:

```python
class CandidateDecisionWorkBuilder(Protocol):
    def expected_input(self, *, candidate_id: uuid.UUID
                       ) -> CandidateDecisionWorkInput: ...
    def exact_work(self, *, value: CandidateDecisionWorkInput
                   ) -> WorkflowWork | None: ...
```

The external canonical `candidate_decision_input/v1` contains candidate id,
candidate content hash, resolved organization/project/team ids, ordered
evidence-manifest hash, and policy version; it contains kind only if the CP3/CP5
dispatch policy requires kind. C2 neither recomputes nor broadens that set,
never hashes title/body/confidence redundantly, and never stores live evidence
in a finding. A changed evidence-manifest hash is a new immutable work
generation; terminal work is never mutated or reopened.

When no builder is registered, the inspector reports
`candidate_decision_builder_unavailable` and P6 remains missing observability.
With the builder, it reports `candidate_decision_work_missing`,
`candidate_decision_work_inactive`, or `candidate_decision_work_scope_mismatch`
against the exact builder output. A canonical same-scope conflict satisfies the
candidate side of P6. The inspector never calls `CurateMemoryCandidate`,
promotes/rejects a candidate, or changes the human inbox.

### Projection Inspector

Create `projection_reconciler.py` as a read-only wrapper around the exact P7
structural predicates. It reports `current_projection_missing_or_inconsistent`
with memory/document ids only and proposed action `defer_to_cp4`. It creates no
projection work, embeddings, memory versions, or semantic transitions.

### Transport Inspector

Create `transport_work_reconciler.py`. It may read
`CeleryOutboxDeadLetter` because package v0.4.0 exposes indexed `task_id`,
`task_name`, id-only `args/kwargs`, and `dead_at`. It never selects/parses
`failure_reason` and never writes package models.

It accepts only the known versioned task-name allowlist, requires one work UUID
and optional run UUID, resolves both under explicit organization/project scope,
and emits:

- `dead_letter_unsatisfied_work` when a matching dead letter exists and the
  exact logical work remains required;
- `dead_letter_unsatisfied_attempt` when the explicit queued/running v1 run is
  still active;
- `dead_letter_already_satisfied` for terminal work/run, as an informational
  count rather than a repair;
- `dead_letter_payload_invalid` when the deterministic task id resolves a work
  in the requested scope but its id-only args are malformed, without echoing
  args; an unattributable row is omitted from project-scoped output;
- `attempt_signal_missing` for a queued v1 run with null `dispatched_at`.

A new domain attempt may be queued because work is ready/due. It is never
queued merely because no current package row exists. Dead-letter replay remains
an operator action provided by `django-celery-outbox`, outside this command.

## Unified Report And Metrics

Create `work_reconciliation.py` with:

```python
@dataclass(frozen=True, slots=True)
class ReconciliationFinding:
    invariant_id: str
    code: str
    organization_id: uuid.UUID
    project_id: uuid.UUID
    entity_type: str
    entity_id: str
    work_id: uuid.UUID | None
    workflow_run_id: uuid.UUID | None
    observed_at: datetime
    proposed_action: str
    auto_repair_eligible: bool

@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    organization_id: uuid.UUID
    project_id: uuid.UUID
    as_of: datetime
    findings: tuple[ReconciliationFinding, ...]
    counts_by_code: tuple[tuple[str, int], ...]
    work_counts_by_type_state: tuple[tuple[str, str, int], ...]
    oldest_age_seconds_by_code: tuple[tuple[str, int], ...]
```

Findings sort by `(invariant_id, code, entity_type, entity_id, work_id,
workflow_run_id)` and are capped at 20 samples per code after exact counts are
computed. Reports contain no titles, bodies, evidence, prompts, secrets,
provider output, package args, or free-form failure reasons.

Add report-only command
`engram_audit_work_reconciliation --organization-id <uuid> --project-id
<uuid> [--as-of <aware-ISO8601>] [--format text|json]`. It has no mutation
flag. Scope mismatch, naive time, or malformed id fails before all other reads.
Repeated execution at the same database snapshot/as-of is byte-stable JSON.

The command and internal callable emit one structured summary log and the
database-derived counts/oldest ages above. They do not add organization/project
labels to the process-global Prometheus endpoint in C2; a later bounded
operations surface may consume the same scoped callable.

P13 remains `missing_observability` with stable reason
`repair_run_relation_missing` and target CP2/CP10. The CP2 audit is
scoped, idempotent, and explainable, but it is not falsely labeled a resumable
historical repair run.

## Invariant Evolution

- P3 becomes exact for `end_work_contract_version=1`: every ended session's
  latest useful sequence has exact terminal work. Missing/required/operationally
  terminal latest work is a violation even if generation N succeeded before
  N+1.
- P4 becomes exact: expired `leased` work is a violation; zero expired leases
  is healthy because owner, token, heartbeat, expiry, and reclaim evidence now
  exist.
- P6 remains `missing_observability` until CP3/CP5 register the canonical
  builder. C2 makes missing/inactive/mismatched work deterministically visible;
  the builder-owning checkpoint upgrades its exact post-cutover cohort.
- P13 remains missing for historical mutation identity as described above.
- P14 remains globally missing, while every new claim/reconciler gets one
  foreign organization/project/team negative control.

No zero proxy is relabeled healthy outside its exact post-cutover cohort.

## First RED And Fault Tests

### C2.1 Schema, Claim, And Failure

1. `test_delivery_loss_before_claim_leaves_work_reclaimable` proves F7: no run
   or claim is needed for required work to remain visible and signal-eligible.
2. `test_expired_lease_is_reclaimed_and_stale_owner_is_fenced` proves F8: one
   expired run fails, token increments, late token cannot write, new token can.
3. two simultaneous automatic claims create one running run and one token;
4. replay by the exact same run/owner returns the same claim without increment;
5. heartbeat extends only current owner/token and never resurrects expiry;
6. scope/type/fingerprint mismatch fails before provider/domain execution;
7. provider transient, infrastructure transient, and unexpected failures use
   their exact first/capped delays and append distinct failed attempts;
8. configuration stays blocked at equal fingerprint and resumes once at a
   changed fingerprint without exposing secret material;
9. deterministic invalid input is terminal operationally, remains product
   required, and appears in P3/report output;
10. a task killed after claim but before provider leaves one expiring lease;
11. provider result followed by lease expiry is rejected before semantic write;
12. v0 historical run fields remain untouched and cannot be claimed as v1;
13. invalid state/field combinations and duplicate work/token/running rows fail
    at the database constraint;
14. automatic delivery of settled work is absorbed, while a valid queued manual
    run may lease it without changing its product disposition.

### C2.2 Session Reconciliation

1. ended post-cutover session with no exact work reports missing generation;
2. exact required work with no run reports `work_never_claimed` after grace;
3. stale queued attempt is re-signaled once under two concurrent reconcilers;
4. expired lease/due retry select exact linked work, while success for N never
   covers failed/required N+1 or hides a later failure (F10);
5. lifecycle-only/zero-input requires the exact no-op generation;
6. foreign-scope rows affect no count/sample and report mode writes nothing;
7. package creation failure rolls back queued run and dispatch time.

### C2.3 Candidate, Projection, Transport, And Report

1. absent/stub builder proves stable missing/inactive/scope-mismatch reporting;
2. changed evidence manifest resolves a new generation and never reopens old
   terminal work;
3. canonical conflict is recognized and ordinary proposals are not reclassified;
4. candidate content/live evidence never enters output; projection is read-only;
5. an unsatisfied allowlisted dead letter resolves stable work/run ids without
   copying package state;
6. malformed/foreign payload neither discloses scope nor drives package absence;
7. report JSON is deterministic, capped, content-free, and read-only;
8. the command rejects missing/mismatched scope, naive `as_of`, and every
    mutation-style option;
9. P3/P4 exact, P6 builder-aware, and P13 partial results match the catalog.

## Files And Ownership

### C2.1 single schema/execution owner

- modify `apps/backend/engram/core/models.py`;
- create `apps/backend/engram/core/migrations/0035_workflow_work_execution.py`;
- create `work_execution.py`, `work_failures.py`, `work_dispatch.py`, and their
  adjacent tests under `apps/backend/engram/memory/`;
- modify memory `tasks.py`/tests/tracking adapters and focused model/migrations
  tests only for the exact additive contract.

### C2.2 session reconciler owner

- create `apps/backend/engram/memory/session_work_reconciler.py` and tests;
- modify `distillation_reconciler.py` only as a temporary compatibility adapter;
- after C2.1 handoff, modify memory tasks/routing and invariant queries/tests.

### C2.3 disjoint inspectors/operations owners

- create separate candidate, projection, and transport reconciler modules/tests;
- create `work_reconciliation.py`, the report-only command, and their tests;
- modify invariant queries/tests only for builder-aware P6 and P13 reason.

Reconcilers remain separate modules. One owner at a time edits shared models,
tasks, invariant queries, or migrations. No C2 owner edits CP3 coverage, CP4
projection writers, CP5 semantic curation, package source, deployment, or
release files.

## CI-Only Development Gate

All Python/backend commands run inside the repository Compose environment.
Focused commands are sequential; no concurrent pytest or Docker launches are
permitted for this checkpoint.

Each serial slice records RED then GREEN, exact test count, and exit code for:

```text
docker compose -f deploy/compose/docker-compose.yml run --build --rm api \
  poetry run pytest <slice-owned-test-files> -q
docker compose -f deploy/compose/docker-compose.yml run --build --rm api \
  poetry run ruff check <slice-owned-python-files>
docker compose -f deploy/compose/docker-compose.yml run --build --rm api \
  poetry run ruff format --check <slice-owned-python-files>
docker compose -f deploy/compose/docker-compose.yml run --build --rm api \
  poetry run python manage.py makemigrations --check --dry-run \
  --settings=settings.test_settings
docker compose -f deploy/compose/docker-compose.yml run --build --rm api \
  poetry run python manage.py check --settings=settings.test_settings
git diff --check
```

Migration tests apply from the last CP1 migration, reverse to it, reapply, and
prove v0 rows survive byte-for-byte. Fault tests use PostgreSQL barriers and
injected aware clocks. SQLite, mocks of row locking, or a passing unit test are
not substitutes for concurrency evidence.

There is no Beat entry, production flag enablement, migration execution,
canary, SSH, D2, deployment, or dead-letter replay in this gate. A later
reviewed rollout spec must define the activation watermark and fresh-work-only
automatic reconciliation before runtime enablement.

## Rollback

- Before any claim, code may revert while additive columns remain. After v1
  claims, disable new claim/reconciliation behavior first and let
  current leases finish or expire; do not downgrade workers while they can
  commit with v1 tokens.
- Never decrement/reuse a fencing token, delete attempt history, blank a typed
  failure, or return terminal product work to required.
- Schema reversal is allowed only in isolated migration tests or before any v1
  run exists. Otherwise use a forward compatibility migration.
- Candidate schema rollback belongs to CP3/CP5; C2 only removes registration.
- No rollback mutates/replays/purges/copies package or dead-letter rows.
- Provider/configuration outage rollback changes behavior flags/code, not
  candidate or memory semantic state.

## Stop Conditions

Stop before implementation or the next serial slice if:

- any CP1 serial gate, sequence contract, versioned adapter, producer census,
  or old-worker/package drain is incomplete;
- a transition changes immutable identity/product disposition, or a writer
  cannot fence in the same transaction as durable output;
- a design locks across provider/broker calls or uses package absence as evidence;
- configuration change cannot be detected without hashing secret material;
- failure classification still depends on free-form text or has an unclassified
  exception path;
- concurrency permits two running v1 runs or an old-token commit;
- candidate reconciliation would require C2 to own the CP3/CP5 input builder,
  execute CP5 semantics, or mutate the historical backlog;
- projection reporting needs CP4 writes, or package v0.4.0 loses id-only DLQ shape;
- migration/backfill would reinterpret v0 run history;
- the first decisive test failure is unclear or a fix changes data, security,
  transport, deployment, or public API beyond this specification;
- production, SSH, deployment, D2, runtime migration, or automatic historical
  repair is requested without a separately approved rollout.

## Acceptance Gate

C2 closes only after C2.1, C2.2, and C2.3 merge serially from a completed CP1;
F7-F10 and foreign-scope controls pass in PostgreSQL; exact P3/P4 plus
builder-aware P6 and partial P13 are documented; output is content-free;
all focused container checks and independent simplicity/adversarial reviews are
green; and no runtime activation has occurred under this CI-only specification.
