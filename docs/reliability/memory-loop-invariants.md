# Autonomous Memory Loop Invariants

Date: 2026-07-10

Status: Checkpoint 0 baseline contract

Authority: [Checkpoint 0 reliability contract](../superpowers/specs/2026-07-10-checkpoint-0-reliability-contract.md)
and [Autonomous Memory Loop Reliability Roadmap](../superpowers/specs/2026-07-09-autonomous-memory-loop-roadmap.md)

## Scope And Evidence Rules

The internal CP0 evaluator accepts an explicit `organization_id` and
`project_id`, resolves that pair before every other query, and fails closed
when the project does not belong to the organization. It returns P1 through
P15 in order. Samples are scoped, deterministically ordered, entity-qualified,
and capped at 20.

Evidence quality in this catalog means:

- `exact`: the current schema can count the named CP0 relational predicate;
- `proxy`: current rows are diagnostic, but cannot prove the product invariant;
- `missing`: the relation required to prove the invariant does not exist yet.

A proxy result is always `missing_observability`, including when its count is
zero. `Exact` describes the named relation, not proof of a future invariant
whose supporting model has not landed.

An empty package queue is not evidence of product completion. A package row
proves transport intent; a future `WorkflowWork` row proves that required
product work was durably created and records its product disposition. Engram
must not infer either fact from the absence of the other.

## Catalog

| ID | Invariant statement | CP0 evaluator and result contract | Current evidence quality | Missing relation | Owner | Healthy target |
|---|---|---|---|---|---|---|
| P1 | Every acknowledged event has one durable raw envelope and exactly one valid normalized disposition. | Count scoped raw envelopes whose total `ObservationSource.raw_event` link cardinality is not one or whose same-scope link to a same-scope observation cardinality is not one. Zero is `healthy`; nonzero is `violated`. | exact | Explicit non-observation disposition is not represented. | CP1 | Every acknowledged raw envelope has exactly one same-scope normalized observation or explicit no-op disposition. |
| P2 | Every acknowledged event or lifecycle transition that requires async work has durable logical work committed with it. | No logical-work relation exists, so the result is always `missing_observability`; both counts are absent. | missing | Durable logical-work-intent relation tied to the source transition. | CP1 | Evidence or lifecycle state, scoped logical work, and its package delivery signal commit or roll back together. |
| P3 | Every ended session with useful observations has a complete distillation disposition for its latest input watermark. | CP3 sessions (`end_work_contract_version=1`) are evaluated against the exact latest useful upper sequence, matching immutable work fingerprint, window, complete stages, and settled finalization. Older completed generations never mask a newer incomplete one. Legacy sessions remain `missing_observability`. | exact for CP3 cohort; missing for legacy | Exact latest and completed input watermarks for legacy sessions. | CP2/CP3 | The exact latest immutable generation is complete or has an explicit terminal no-input disposition. |
| P4 | No logical work remains leased past its recovery window without being reclaimed. | Proxy `RUNNING` workflow attempts where `Coalesce(started_at, created_at)` is more than 30 minutes before aware `as_of`. The result is always `missing_observability`. | proxy | Lease expiry, owner, heartbeat, and reclaim evidence. | CP2 | An expired lease is reclaimable, stale owners are fenced, and logical work remains visible until complete. |
| P5 | Every input observation in a completed distillation window has a candidate, promoted memory, or explicit no-signal disposition. | Every completed CP3 root is checked against immutable chunk manifests, coverage rows, and candidate-source rows. Missing/extra/duplicate coverage, sequence or digest mismatch, foreign scope/reference, signal/source inverse violations, stage/target/cardinality anomalies, and a root finalized before all stages are violations. Legacy cohorts remain `missing_observability`. | exact for completed CP3 windows; missing for legacy | Completed-window observation coverage and candidate-source relations for legacy cohorts. | CP3 | Every observation in the immutable completed window has one durable, inspectable disposition. |
| P6 | Every proposed candidate has active automatic decision work or is a genuine conflict. | The registered CP3 candidate-decision builder recomputes the current source manifest and compares exact same-scope work identity, snapshot, and CP2 execution state. The result reports exact missing/inactive/mismatched findings as a bounded proxy and remains `missing_observability` until CP5 supplies canonical conflict-only classification and terminal convergence. | exact candidate-to-work proxy; global health deferred | Candidate-to-active-decision-work and canonical conflict relation. | CP2/CP3/CP5 | Ordinary candidates progress autonomously; only unresolved semantic conflicts remain for humans. |
| P7 | Every promoted memory has one coherent current version, retrieval representation, provenance set, and audit transition. | Count four guarded anomaly relations: promoted candidate without same-scope memory; missing declared current version; current-version body mismatch; and missing or inconsistent current retrieval document. A nonzero sum is `violated`; zero remains `missing_observability` because uniform provenance/audit identity is absent. | exact structural checks; missing provenance | Relational promotion provenance and transition audit identity. | CP4 | Promotion atomically leaves one coherent same-scope memory, current version, retrieval representation, provenance set, and audit transition. |
| P8 | Every supersede, merge, refute, or conflict transition preserves source history and current-state consistency. | No uniform atomic lineage-transition identity exists, so the result is always `missing_observability`; both counts are absent. | missing | Immutable transition history and authoritative current pointer. | CP4 | Every semantic transition is atomic, reversible from preserved history, and has one authoritative current state. |
| P9 | Conflict evidence and links survive cleanup, retries, and restarts until explicit resolution. | Static conflict links exist, but their durability across cleanup and restart cannot be proved. The result is always `missing_observability`. | missing | Conflict evidence surviving cleanup and restart. | CP4/CP5 | Conflict evidence remains durable and linked until an explicit semantic resolution. |
| P10 | Every context replay is fingerprint-compatible, byte-stable, authorized, and within its declared budget. | No request fingerprint and immutable rendered snapshot contract exists, so the result is always `missing_observability`; both counts are absent. | missing | Replay fingerprint, byte hash, authorization, and budget evidence. | CP6 | An authorized request replays the exact immutable bytes for its fingerprint and declared budget. |
| P11 | No temporally ineligible memory is injected as current knowledge. | No temporal eligibility state exists, so the result is always `missing_observability`; both counts are absent. | missing | Retrieval-time temporal eligibility evidence. | CP8 | Current context excludes or clearly withholds memories whose validity is stale, unknown, refuted, or awaiting revalidation. |
| P12 | The human review inbox contains only unresolved semantic conflicts. | Count ordinary proposed candidates without a scoped `CONFLICTS_WITH` target plus reviewable low-confidence/refuted memories excluding `status=conflict`. Zero is `healthy`; nonzero is `violated`. | exact | No CP0 relation is missing for the current inbox predicate; the genuine-conflict policy completes in CP5. | CP5 | Humans see only durable, unresolved semantic conflicts; all routine uncertainty and operational failure is automatic. |
| P13 | Every repair operation is scoped, idempotent, resumable, and dry-run explainable. | No resumable repair identity exists, so the result is always `missing_observability`; both counts are absent. | missing | Repair identity, progress, idempotency, and dry-run explanation. | CP2/CP10 | A scoped repair can be explained, interrupted, resumed, and replayed without duplicate effects. |
| P14 | All reads, work creation, provider calls, repair, and retrieval begin from resolved organization/project/team scope. | Focused scope tests exist, but no single runtime relation proves all source-to-sink paths. The result is always `missing_observability`. | missing | Operation-to-resolved organization/project/team evidence. | CP1+ | Every source-to-sink operation resolves and narrows scope before reads, writes, ranking, packing, calls, or dispatch. |
| P15 | A request for repository state R cannot present code-sensitive memory as current until impact processing covers R. | No accepted-versus-impact-processed repository revision relation exists, so the result is always `missing_observability`; both counts are absent. | missing | Memory revision and impact-coverage revision relation. | CP8 | Current code-sensitive context is gated on impact coverage for the requested repository revision. |

## Stable Result Reasons

| ID and state | Reason | Missing evidence | Target checkpoint |
|---|---|---|---|
| P1 healthy | `scoped_raw_events_normalized` | none | CP1 |
| P1 violated | `raw_event_normalization_cardinality_invalid` | none | CP1 |
| P2 missing | `logical_work_intent_relation_missing` | durable logical-work-intent relation tied to the source transition | CP1 |
| P4 missing | `work_lease_and_reclaim_evidence_missing` | lease expiry, owner, heartbeat, and reclaim evidence | CP2 |
| P3 healthy | `latest_distillation_window_complete` | none | CP3 |
| P3 violated | `latest_distillation_window_incomplete` | none | CP3 |
| P3 missing | `legacy_distillation_window_unobservable` | exact latest and completed input watermarks for legacy sessions | CP2/CP3 |
| P5 healthy | `completed_window_observations_disposed` | none | CP3 |
| P5 violated | `completed_window_coverage_invalid` | none | CP3 |
| P5 missing | `legacy_observation_coverage_unobservable` | completed CP3 observation coverage and source relations for legacy cohorts | CP3 |
| P6 missing | `candidate_decision_work_relation_missing` | candidate-to-active-decision-work and canonical conflict relation; global health remains deferred to CP5 | CP2/CP3/CP5 |
| P7 violated | `promotion_chain_inconsistent` | relational promotion provenance and transition audit identity | CP4 |
| P7 missing | `promotion_provenance_audit_relation_missing` | relational promotion provenance and transition audit identity | CP4 |
| P8 missing | `memory_transition_history_relation_missing` | immutable transition history and authoritative current pointer | CP4 |
| P9 missing | `durable_conflict_evidence_relation_missing` | conflict evidence surviving cleanup and restart | CP4/CP5 |
| P10 missing | `replay_evidence_fields_missing` | replay fingerprint, byte hash, authorization, and budget evidence | CP6 |
| P11 missing | `temporal_eligibility_evidence_missing` | retrieval-time temporal eligibility evidence | CP8 |
| P12 healthy | `human_inbox_conflicts_only` | none | CP5 |
| P12 violated | `non_conflict_item_in_human_inbox` | none | CP5 |
| P13 missing | `repair_run_relation_missing` | repair identity, progress, idempotency, and dry-run explanation | CP2/CP10 |
| P14 missing | `operation_scope_resolution_evidence_missing` | operation-to-resolved organization/project/team evidence | CP1+ |
| P15 missing | `repository_impact_coverage_relation_missing` | memory revision and impact-coverage revision relation | CP8 |

## Count And Sampling Semantics

- P1 joins through `ObservationSource`, not the legacy
  `Observation.raw_event` reverse-relation proxy. Both total source-link count
  and same-scope valid-link count must equal one.
- P3 excludes lifecycle-only observations, derives the latest useful sequence
  for each CP3 session, and requires the exact matching work/window/stages and
  settled finalization. A prior successful upper sequence never covers a later
  failed generation. Legacy sessions retain `missing_observability`.
- P4 uses an aware effective time. A naive caller-supplied `as_of` is rejected.
- P7 sums the four anomaly-relation counts. One memory may contribute more than
  once, while samples are deduplicated. Body equality is compared in the
  database; content is never loaded into the report.
- P12 recognizes only a same-scope link from a same-scope memory whose target
  is `candidate:<uuid>` as a candidate conflict, and recognizes
  `Memory.status=conflict` as a memory conflict.
- P5 compares every completed CP3 window; samples contain only bounded typed
  entity ids and never content, anchors, prompts, or provider output. P6 uses
  only the registered exact source-manifest builder and intentionally does not
  claim global health before CP5.
- A zero `proxy_count` never turns `missing_observability` into `healthy`.

The CP0 evaluator is read-only. It does not expose an API, command, scheduler,
repair path, or cross-project aggregate mode.
