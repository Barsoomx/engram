# Domain Progress And Package Transport

Date: 2026-07-10
Status: accepted

## Context

The API can commit accepted evidence and then die before a post-commit task
callback creates a package outbox row. Package delivery state also cannot prove
whether the product work was ever created or completed for an exact input.

## Decision

`django-celery-outbox` remains the sole transport authority. Engram adds one
product-domain logical-work concept, `WorkflowWork`, in Checkpoint 1. Existing
`WorkflowRun` rows retain attempt/history semantics and may link to logical work
additively.

`WorkflowWork` owns stable scoped identity, immutable input fingerprint and
snapshot, required/complete/no-op disposition, reconciliation eligibility, and
later CP2 lease/fence state. It never mirrors broker state, owns a relay, or
implements transport retry/dead-letter behavior.

Automatic tasks carry `WorkflowWork.id`. An explicit manual rerun may also
carry its queued `WorkflowRun.id`; both are stable scoped domain ids. Workers
reload scope, linkage, and subject state from PostgreSQL. Reconciliation derives
need from domain invariants and emits an id-only logical-work task through the
package-backed boundary.

## Alternatives Rejected

- Reinterpreting `WorkflowRun` would break attempt and console-rerun semantics.
- Treating package outbox rows as domain progress cannot detect never-created
  work or exact-input completion.
- Deriving everything from raw evidence cannot preserve attempt, lease, or
  completed-generation history.

## Consequences

- CP1 is additive and leaves historical runs unlinked.
- CP2 adds lease/fencing only after logical identity exists.
- Operational work, semantic candidate, and temporal-validity state remain
  separate.
- Historical repair is dry-run-first and deferred to the roadmap repair gate.
