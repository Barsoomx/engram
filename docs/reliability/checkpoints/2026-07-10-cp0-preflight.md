# Checkpoint 0 Preflight Evidence

Date: 2026-07-10

Status: sanitized, read-only campaign baseline

## Scope And Safety

This report records the bounded Checkpoint 0 baseline captured by the
integration owner. Production inspection was read-only. No production repair,
backfill, mutation, deployment, restart, fault injection, or configuration
change ran. The documentation task did not reconnect to production or rerun
the inventory.

Only aggregate counts are transcribed. This artifact contains no production
tenant grouping, identifiers, names, slugs, memory text, payloads, repository
paths, individual-row timestamps, failure strings, provider/model names,
hostnames, endpoints, DSNs, credentials, or task arguments.

`Exact` below means the named current relation or flag was counted exactly. It
does not mean that the count proves a future product invariant. `Proxy` means
the current schema cannot prove the intended domain property. A fact without a
supporting current relation is `UNOBSERVABLE`, never inferred as zero.

## Revision Baseline

| Revision | SHA | Evidence |
|---|---|---|
| Local `master` | `e4f68eeac2e571e7b1d8442bf61c54f06221070c` | `git rev-parse HEAD`, exit 0 |
| `origin/master` | `e4f68eeac2e571e7b1d8442bf61c54f06221070c` | `git rev-parse origin/master`, exit 0 |
| Deployed backend image label | `e4f68eeac2e571e7b1d8442bf61c54f06221070c` | Bounded read-only label inspection, exit 0 |
| Deployed frontend image label | `e4f68eeac2e571e7b1d8442bf61c54f06221070c` | Bounded read-only label inspection, exit 0 |
| `upstream` | `3fe0725a97e18b5edf3e61cde60e181ab2b6c997` | `git rev-parse upstream`, exit 0 |
| Roadmap and Memory CI design commit | `1e0e7517a4fea6983f98de524f3965d1a9d51055` | Branch design SHA |
| Accepted focused CP0 contract | `23a40d26b1d23890b1ddb79add51c088dcf0de60` | Reviewed tracked spec commit |
| Documentation task start | `4377f54090bf16ee7eafc5050d8a4b349f165111` | Task 1 contract commit and branch handoff |

The production snapshot was captured at `2026-07-10T01:30:43Z`. The SHA-256 of
the canonical sorted `query_id=count` aggregate snapshot is:

```text
35267a66917d463a16efbcbf68cb18362c6f5b0857557855dde23ba0d2e9602b
```

The remote inspection command is intentionally not reproduced because its
connection target is excluded from tracked artifacts. The command only read
revision labels and aggregate database state; its recorded exit code was 0.

## Repository Verification

The integration owner recorded these commands before creating the isolated
checkpoint worktree:

| Command | Exit | Recorded outcome |
|---|---:|---|
| `git status --short --branch` | 0 | Local `master` tracked `origin/master`; two untracked roadmap documents were isolated and then committed on the checkpoint branch. |
| `git rev-parse HEAD` | 0 | `e4f68eeac2e571e7b1d8442bf61c54f06221070c` |
| `git rev-parse origin/master` | 0 | `e4f68eeac2e571e7b1d8442bf61c54f06221070c` |
| `git rev-parse upstream` | 0 | `3fe0725a97e18b5edf3e61cde60e181ab2b6c997` |
| `git worktree list --porcelain` | 0 | Existing sibling worktrees were enumerated; none were removed, reused, or modified. |

## Sanitized Production Inventory

| Aggregate | Count | Evidence quality |
|---|---:|---|
| Raw event envelopes | 44,440 | Exact |
| Observations | 42,710 | Exact |
| Raw events without `Observation.raw_event` reverse relation | 1,730 | Proxy for P1; not the required `ObservationSource` predicate |
| Sessions: active / ended / errored | 1 / 233 / 47 | Exact status counts |
| Ended sessions with observations | 225 | Exact relational count |
| Ended sessions without any historically successful distillation | 131 | Proxy for P3 |
| Ended sessions currently running without prior success | 0 | Proxy diagnostic |
| Session-distillation runs: failed / running / succeeded | 35 / 20 / 160 | Exact status counts |
| Running session-distillation rows older than 30 minutes / 6 hours | 20 / 20 | Exact age counts; proxy for P4 |
| Daily digest runs: failed / succeeded | 1 / 24 | Exact status counts |
| Weekly digest runs: succeeded | 4 | Exact status count |
| Candidates: proposed / promoted / rejected | 1,273 / 10,533 / 19,156 | Exact status counts |
| Proposed candidates older than 1 day / 7 days | 1,245 / 0 | Exact age counts; proxy for P6 |
| Promoted candidates without memory | 0 | Exact current structural count |
| Approved memories | 10,566 | Exact status count |
| Memory versions / retrieval documents | 10,563 / 10,563 | Exact row counts |
| Memories missing any version, declared current version, or current retrieval document | 3 | Exact current structural aggregate |
| Stale memories / stale retrieval documents | 2,079 / 1,848 | Exact flag counts |
| Memory/retrieval state mismatch | 231 | Proxy aggregate; CP0 evaluator supplies scoped relational checks |
| Context bundles: total / empty-query / nonempty-query | 703 / 527 / 176 | Exact current row counts |
| Audit events | 49,332 | Exact row count |
| Package outbox rows | 0 | Exact transport count; not evidence of product completion |

The 1,730-row P1 proxy was captured through the legacy
`RawEventEnvelope.observations__isnull` relation, not through the accepted P1
`ObservationSource` cardinality predicate. Its aggregate event-type breakdown
was:

| Event type | Proxy rows |
|---|---:|
| `claude_mem.user_prompt` | 1,640 |
| `claude_mem.post_tool_use` | 86 |
| `claude_mem.session_start` | 2 |
| `claude_mem.user_prompt_submit` | 2 |

These four rows sum to 1,730. They characterize only the old reverse-relation
proxy and must not be presented as P1 violations. In particular, the exact
current structural count of zero promoted candidates without memory does not
prove complete promotion provenance, and zero package outbox rows does not
prove that all required product work was created or completed.

## Facts Not Observable In The Snapshot

| Product fact | Baseline value | Missing evidence |
|---|---|---|
| Required logical work was created for every acknowledged transition | UNOBSERVABLE | `WorkflowWork` relation tied atomically to evidence/lifecycle state |
| An ended session's latest immutable input generation completed | UNOBSERVABLE | Latest and completed input watermarks |
| A running logical work lease can expire and be reclaimed safely | UNOBSERVABLE | Lease owner, expiry, heartbeat, and fencing evidence |
| Every observation in a completed window has a disposition | UNOBSERVABLE | Observation coverage ledger |
| Every proposed candidate has active decision work or a durable genuine conflict | UNOBSERVABLE | Candidate-to-work and canonical durable conflict relations |
| Every coherent promotion has uniform provenance and one audit transition | UNOBSERVABLE | Promotion provenance and transition identity |
| Conflict evidence survives cleanup and restart | UNOBSERVABLE | Durability evidence across cleanup/restart boundaries |
| Context replay is byte-stable, authorized, and budget-exact | UNOBSERVABLE | Request fingerprint, immutable snapshot hash, authorization, and budget evidence |
| Retrieval enforced temporal eligibility for the requested repository revision | UNOBSERVABLE | Temporal eligibility and repository-impact coverage revisions |
| Historical repair is scoped, resumable, idempotent, and explainable | UNOBSERVABLE | Durable repair-run identity and progress |

## Isolated Container Baseline

The baseline used the isolated Compose project `engram-cp0-e4f68ee`.

### Compose Configuration

The first configuration check was:

```bash
docker compose -p engram-cp0-e4f68ee \
  -f deploy/compose/docker-compose.yml config --quiet
```

It exited 1 because the isolated worktree did not yet have its ignored `.env`.
A fresh development-only `.env` was generated from `.env.example` without
copying secrets. The same command was then rerun and exited 0.

### Migrations, Model Freshness, And System Check

The captured command was:

```bash
docker compose -p engram-cp0-e4f68ee \
  -f deploy/compose/docker-compose.yml run --build --rm api sh -ec \
  "poetry install --no-interaction --no-root --with dev &&
   python manage.py migrate --noinput &&
   python manage.py makemigrations --check --dry-run &&
   python manage.py check"
```

It exited 0: all migrations were applied, model freshness reported
`No changes detected`, and the Django system check reported no issues.

### Focused Memory-Loop Tests

The captured command was:

```bash
docker compose -p engram-cp0-e4f68ee \
  -f deploy/compose/docker-compose.yml run --rm api sh -ec \
  "poetry install --no-interaction --no-root --with dev &&
   pytest -q \
     engram/core/celery_foundation_tests.py \
     engram/hooks/hook_ingest_tests.py \
     engram/memory/workflow_run_tracking_tests.py \
     engram/memory/session_sweep_tests.py \
     engram/memory/distillation_reconciler_tests.py \
     engram/memory/memory_worker_tests.py"
```

It exited 0 with 120 tests passed in 8.64 seconds.

## Baseline Interpretation

The green container baseline shows that the captured revision is internally
consistent under the existing contract. It does not close the documented
post-commit callback loss window and does not make missing domain relations
observable. Likewise, a healthy API, green tests, exact row counts, and an
empty package queue can coexist with lost or never-created product work.

This checkpoint records that gap before behavior changes. Production remained
read-only throughout the capture, and no repair ran.
