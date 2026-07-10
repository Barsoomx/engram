# Autonomous Memory Loop Fault Matrix

Date: 2026-07-10

Status: Checkpoint 0 characterization and future-test contract

Authority: [Checkpoint 0 reliability contract](../superpowers/specs/2026-07-10-checkpoint-0-reliability-contract.md)

## Reading The Matrix

Checkpoint 0 documents current behavior and adds synthetic characterization
evidence only. It does not implement recovery or run dynamic crash tests. The
named future test in each row belongs to the checkpoint that introduces the
corresponding recovery mechanism.

Acceptable recovery outcomes are complete rollback, durable retryable work,
idempotent convergence to one durable effect, or automatic rebuild of a
derived projection. A manual database fix is never the target outcome.

`django-celery-outbox` remains the sole transport authority. Package rows,
relay publication, transport retries, and dead letters are not duplicated in
Engram. Logical product work is separate: an empty package queue can coexist
with never-created or incomplete work and is not proof of product progress.

## Fault Rows

| ID | Fault boundary | Durable state before fault | Current outcome | Target outcome | Invariant | Owner checkpoint | Executable evidence |
|---|---|---|---|---|---|---|---|
| F1 | Scope denied before an ingest or work-creation path | Only pre-existing foreign-scope rows; no accepted target-scope state | Focused negative tests exist, but no uniform product-work relation proves that every source-to-sink path resolved the organization/project/team first. | Fail closed before evidence, logical work, package creation, provider invocation, ranking, or packing; foreign rows remain unchanged. | P14 | CP1 | `test_foreign_project_request_creates_no_evidence_work_or_package` creates a mismatched pair and asserts zero target writes/package rows and unchanged foreign counts. |
| F2 | Database transaction rolls back after evidence/work creation begins | The pre-transaction scoped state only | The current database rows and registered post-commit callback roll back together, but there is no logical-work record to characterize required progress. | Evidence, normalized disposition, logical work, and package row all roll back; acknowledgement is not returned. | P1, P2 | CP1 | `test_forced_rollback_leaves_no_evidence_work_or_package` injects a failure after package-backed enqueue and asserts all four scoped counts are unchanged. |
| F3 | Database commits, then the API process dies before its post-commit callback runs | Raw envelope and normalized evidence are committed; no package row is yet durable | Accepted evidence can remain while the only delivery signal is lost forever. Current hook tests explicitly characterize zero package rows inside the transaction. | Evidence, required logical work, and package row are already committed atomically before the process can die. | P2 | CP1 | `test_commit_is_recoverable_before_any_post_commit_process_step` commits the request transaction, suppresses all later process activity, and asserts one scoped work row and one package row remain. |
| F4 | Relay or broker is unavailable after atomic creation | Current package row if the callback already ran; target design also has durable scoped logical work | Package transport can retry once a package exists, but current domain state cannot prove required work was created or remains incomplete. | The package remains package-owned and retryable; logical work remains required until its product disposition completes; recovery never derives completion from queue depth. | P2 | CP1 | `test_broker_failure_preserves_one_work_and_package_until_redelivery` forces publication failure, then recovery, and asserts one work identity and one eventual execution. |
| F5 | Duplicate hook delivery finds existing evidence whose required work or delivery signal is missing | Existing raw envelope/observation; missing logical work and possibly missing package row | The duplicate path can return existing evidence before repairing the lost work signal. | Reuse evidence and immutable policy inputs, idempotently ensure exactly one required work identity and delivery signal, and never reinterpret a captured policy silently. | P2 | CP1 | `test_duplicate_evidence_repairs_missing_work_once` pre-seeds evidence without work, submits the duplicate twice, and asserts one work row and one package row. |
| F6 | Idle-session sweep ends a session, then dies before dispatch | Ended session and observations are committed | The sweep returns ids and calls `.delay()` afterward; death between those steps can leave an ended useful session without a delivery signal. | Session end snapshots one immutable generation and commits its logical work plus package row in the same transaction; empty sessions get an explicit no-input disposition without a task. | P2, P3 | CP1 | `test_idle_end_commits_generation_work_and_package_atomically` kills execution immediately after the end transaction and asserts the exact generation remains recoverable. |
| F7 | Worker dies after broker delivery but before a durable domain claim | Package delivery may be in flight; no durable logical claim exists | Late acknowledgement can redeliver transport, but the product domain cannot show whether the delivered work was ever claimed. | Stable logical work remains ready until a bounded claim succeeds; redelivery or reconciliation converges on that same identity. | P4 | CP2 | `test_delivery_loss_before_claim_leaves_work_reclaimable` loses the first delivery, then replays it and asserts one logical work item reaches completion. |
| F8 | Worker dies after marking an attempt running, with no lease/fence contract | A `RUNNING` `WorkflowRun` attempt exists | The row can remain running indefinitely; age is only a proxy and no owner, expiry, heartbeat, reclaim, or stale-writer fence is recorded. | Lease expiry makes the logical work reclaimable; a new fencing token prevents the dead owner from committing late output. | P4 | CP2 | `test_expired_lease_is_reclaimed_and_stale_owner_is_fenced` advances the clock, reclaims once, rejects the stale token, and accepts only the new owner. |
| F9 | Provider outage continues beyond current task/reconciler retry limits | Evidence and failed attempt history exist | Bounded current retries may end in an abandoned failed-session state; no durable logical work records a future automatic retry, and semantic state must not be treated as rejected. | The attempt ends, logical work remains visible in bounded backoff or operationally blocked state, and recovery automatically schedules another attempt without semantic rejection. | P4, P13 | CP2 | `test_provider_outage_past_task_retry_budget_keeps_logical_work_scheduled` exhausts attempt retries, restores the dependency, and proves reconciliation resumes the same work identity. |
| F10 | A later input generation fails after an older historical success | One successful historical run plus newer accepted input and a later failure | Current reconciliation treats any historical success for the session as sufficient, so newer failed input can become invisible. | Completion is tied to an exact immutable input generation; success for generation N never satisfies N+1. | P3 | CP2/CP3 | `test_success_for_generation_n_does_not_cover_failed_generation_n_plus_1` creates both generations and asserts N+1 remains incomplete and is rescheduled. |
| F11 | Provider returns a response, then the worker dies before durable semantic output | Evidence, claimed logical work, and provider-call provenance available up to the durable stage boundary | The returned result may be lost; replay can repeat the call and current rows cannot prove one durable output/disposition for the covered input. | The work remains incomplete and retryable; replay converges to one candidate, memory, or explicit no-signal disposition with durable stage provenance. | P5, P6 | CP3 | `test_crash_after_provider_response_replays_to_one_durable_decision` injects the fault, retries the same work, and asserts one covered disposition. |
| F12 | Oversized session stops after a partial chunk or maximum-chunk cutoff | Session observations and any already committed derived outputs | No coverage ledger proves which observations were processed, so partial output can be mistaken for complete session distillation. | Deterministic chunks are subranges of one immutable generation; only fully covered input can complete, and uncovered chunks resume automatically. | P3, P5 | CP3 | `test_partial_oversized_session_resumes_uncovered_chunks` faults after the first chunk of 101 observations and asserts complete, non-overlapping coverage after resume. |
| F13 | Candidate is committed before durable automatic decision work | Proposed candidate and source evidence | An ordinary proposed candidate can remain orphaned; current proposed count is only a proxy for missing decision work. | Candidate creation and decision-work identity are linked durably; reconciliation restores missing decision work, and only genuine conflicts enter the human inbox. | P6, P12 | CP3 | `test_orphan_candidate_gets_decision_work_and_terminal_disposition` faults before decision enqueue, reconciles, and asserts automatic completion with no ordinary inbox item. |
| F14 | Transaction fails between candidate promotion, memory creation, and version creation | Proposed candidate and source evidence | The main promotion service currently groups candidate, memory, and version writes in one transaction, but CP0 has no uniform transition identity/provenance contract or fault evidence for every promotion path. | Candidate status, memory, current version, provenance, and transition identity commit atomically or all roll back. | P7, P8 | CP4 | `test_promotion_fault_rolls_back_candidate_memory_and_version` injects a failure at each write boundary and asserts no partial promoted chain survives. |
| F15 | Semantic state commits before its exact retrieval representation or audit transition | Candidate/memory/version state may already be durable | A promoted memory can lack its current retrieval document or auditable transition and therefore be inconsistent or invisible to retrieval. | The semantic transition, exact retrieval representation, and audit identity share an atomic boundary; no current pointer advances alone. | P7, P8 | CP4 | `test_projection_or_audit_fault_cannot_advance_current_memory` injects each failure and asserts either the prior state or one complete new transition. |
| F16 | Embedding generation fails after the exact retrieval document exists | Coherent memory/version and exact same-scope retrieval document | Projection embedding can remain missing or stale; current flags are observable but not a durable automatic recovery contract. | Exact text projection remains rebuildable; embedding failure records retryable projection work and cannot corrupt or repromote semantic state. | P7 | CP4 | `test_embedding_failure_preserves_exact_document_and_retries_projection` restores embedding generation and asserts one current document without a second promotion. |
| F17 | Process dies between promotion and merge/supersession writes | Existing source memories, versions, and candidate evidence | Split semantic writes can leave successor and predecessor current-state or lineage pointers inconsistent. | One immutable transition identity atomically preserves source history, successor state, and authoritative current pointers. | P8 | CP4 | `test_supersession_fault_preserves_one_coherent_lineage_transition` injects every boundary and asserts either the old state or the complete linked transition. |
| F18 | Process dies or mutable state changes during context snapshot/replay | Authorized retrieval inputs and any partially assembled context | No immutable request fingerprint, rendered-byte hash, authorization snapshot, or strict-budget evidence proves compatible replay. | Snapshot creation is immutable and authorized; a compatible replay is byte-stable and within the declared budget, while incompatible state creates a distinct identity. | P10, P14 | CP6 | `test_context_replay_is_byte_stable_authorized_and_budget_exact` faults during packing, retries, and compares identity, bytes, authorization, and byte budget. |
| F19 | Temporal revalidation fails after repository state advances | Durable memory and evidence anchors for an older repository revision | Semantic similarity can still shortlist the memory, while current schema cannot prove its temporal eligibility for the new revision. | Validity becomes unknown/revalidating or stale, the memory is withheld as current knowledge, and automatic revalidation resumes when dependencies recover. | P11, P15 | CP8 | `test_failed_temporal_revalidation_withholds_memory_at_new_revision` advances the revision, fails revalidation, and proves context exclusion until impact coverage and validation succeed. |

## Negative-Scope Control Allocation

Foreign-scope controls are attached once to each distinct evaluator or
source-to-sink trust boundary rather than repeated mechanically for every
worker kill:

| Boundary | Required negative control | Owner |
|---|---|---|
| CP0 invariant evaluator | Every synthetic scenario materializes the same anomaly in target and foreign scopes; only target rows affect counts or samples. | CP0 |
| Ingest and logical-work creation | F1 proves a mismatched organization/project pair creates no evidence, logical work, or package row. | CP1 |
| Worker claim and provider stage | `test_worker_rejects_cross_scope_subject_before_provider_call` proves a foreign subject cannot be claimed or sent to a provider. | CP2/CP3 |
| Semantic transition and retrieval projection | `test_foreign_projection_cannot_satisfy_promotion_chain` proves foreign memory/version/document rows cannot satisfy target coherence. | CP4 |
| Context snapshot and replay | `test_context_snapshot_never_packs_or_replays_foreign_memory` proves authorization precedes ranking and packing. | CP6 |
| Memory CI and temporal revalidation | `test_foreign_repository_revision_cannot_change_target_eligibility` proves impact state is project-scoped. | CP8 |
| Historical repair | `test_repair_dry_run_never_reads_or_mutates_foreign_scope` proves repair identity and batches remain narrowed. | CP10 |

## Checkpoint Ownership Summary

- CP1 owns F1–F6: scope resolution, atomic commit/delivery signal, duplicates,
  and lifecycle work creation.
- CP2 owns F7–F10 recovery mechanics; CP3 completes generation coverage for
  F10 and owns F11–F13.
- CP4 owns F14–F17 semantic transition and rebuildable projection behavior.
- CP6 owns F18 immutable context snapshot and replay.
- CP8 owns F19 temporal revalidation and repository-impact coverage.

No production fault was injected and no repair was run in CP0.
