# Autonomous Memory Loop Invariants

Date: 2026-07-15

Status: C4.3 current-evidence contract (versioned CP4 cohort)

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

For CP4, P7-P9 are evaluated by contract version. A version-1 row is eligible
for `healthy` only when its complete relational predicate is true; a clean
version-0 row is `missing_observability`, never `healthy`, because C4 does not
invent historical transitions, provenance, audits, or conflict history. In a
mixed cohort, a version-1 violation remains `violated`; legacy residue remains
visible as missing observability when no newer violation masks it.

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
| P7 | Every promoted memory has one coherent current version, retrieval representation, provenance set, and audit transition. | For version 1, require exactly one matching terminal candidate transition; coherent transition/current pointer/version/provenance/document/audit/work; mirrored state and exact-hash agreement. Missing embedding is allowed only when the exact document is present with its matching embedding work or stable operational reason. Any anomaly is `violated`; version-0 residue is `missing_observability`. | exact for version-1 cohort; missing for version-0 residue | Version-0 transition, provenance, audit, and authoritative-pointer history. | CP4 | Promotion atomically leaves one coherent same-scope memory, current version, retrieval representation, provenance set, audit transition, and embedding intent. |
| P8 | Every supersede, merge, refute, or conflict transition preserves source history and current-state consistency. | For version 1, every transition has a valid typed shape with exact from/to/result versions, protected links where required, preserved sources, one audit, and the authoritative current-pointer update. Zero anomalies is `healthy`; version-0 residue (or no version-1 cohort) is `missing_observability`. | exact for version-1 cohort; missing for version-0 residue | Version-0 immutable transition history and authoritative current pointer. | CP4 | Every semantic transition is atomic, reversible from preserved history, and has one authoritative current state. |
| P9 | Conflict evidence and links survive cleanup, retries, and restarts until explicit resolution. | For version 1, every conflict link has exactly one matching open conflict/transition, unresolved evidence is protected, and a resolved row names one terminal resolution transition. Zero anomalies is `healthy`; version-0-only residue is `missing_observability`. | exact for version-1 conflict cohort; missing for version-0 residue | Version-0 conflict evidence surviving cleanup and restart. | CP4/CP5 | Conflict evidence remains durable and linked until an explicit semantic resolution. |
| P10 | Every context replay is fingerprint-compatible, byte-stable, authorized, and within its declared budget. | No request fingerprint and immutable rendered snapshot contract exists, so the result is always `missing_observability`; both counts are absent. | missing | Replay fingerprint, byte hash, authorization, and budget evidence. | CP6 | An authorized request replays the exact immutable bytes for its fingerprint and declared budget. |
| P11 | No temporally ineligible memory is injected as current knowledge. | No temporal eligibility state exists, so the result is always `missing_observability`; both counts are absent. | missing | Retrieval-time temporal eligibility evidence. | CP8 | Current context excludes or clearly withholds memories whose validity is stale, unknown, refuted, or awaiting revalidation. |
| P12 | The human review inbox contains only unresolved semantic conflicts. | Count ordinary proposed candidates without a scoped `CONFLICTS_WITH` target plus reviewable low-confidence/refuted memories excluding `status=conflict`. Zero is `healthy`; nonzero is `violated`. | exact | No CP0 relation is missing for the current inbox predicate; the genuine-conflict policy completes in CP5. | CP5 | Humans see only durable, unresolved semantic conflicts; all routine uncertainty and operational failure is automatic. |
| P13 | Every repair operation is scoped, idempotent, resumable, and dry-run explainable. | The CP4 projection subset is observable only through the scoped consistency/rebuild services: one organization/project, aware `as_of`, bounded cursor/sample/batch, inert exact dry-run, report-only authoritative mismatch, exact-only rebuild, and embedding work/signal reuse. This subset does not claim global P13 health; the CP0 result remains `missing_observability` until CP10 supplies durable repair-run identity/progress. | exact for the scoped projection subset; missing globally | Durable repair-run identity, progress, and historical replay evidence. | CP4 subset / CP2+CP10 global | A scoped repair can be explained, interrupted, resumed, and replayed without duplicate effects. |
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
| P7 healthy | `promotion_chain_coherent` | none for version-1 cohort | CP4 |
| P7 violated | `promotion_chain_inconsistent` | relational promotion provenance and transition audit identity | CP4 |
| P7 missing | `legacy_transition_observability_missing` | version-0 transition, provenance, audit, and authoritative-pointer history | CP4 |
| P7 missing | `promotion_provenance_audit_relation_missing` | no version-1 promotion provenance/audit relation | CP4 |
| P8 healthy | `memory_transition_history_coherent` | none for version-1 cohort | CP4 |
| P8 violated | `memory_transition_history_invalid` | none | CP4 |
| P8 missing | `memory_transition_history_relation_missing` | version-0 immutable transition history and authoritative current pointer | CP4 |
| P9 healthy | `durable_conflict_evidence_coherent` | none for version-1 cohort | CP4/CP5 |
| P9 violated | `durable_conflict_evidence_invalid` | none | CP4/CP5 |
| P9 missing | `durable_conflict_evidence_relation_missing` | version-0 conflict evidence surviving cleanup and restart | CP4/CP5 |
| P10 missing | `replay_evidence_fields_missing` | replay fingerprint, byte hash, authorization, and budget evidence | CP6 |
| P11 missing | `temporal_eligibility_evidence_missing` | retrieval-time temporal eligibility evidence | CP8 |
| P12 healthy | `human_inbox_conflicts_only` | none | CP5 |
| P12 violated | `non_conflict_item_in_human_inbox` | none | CP5 |
| P13 missing | `repair_run_relation_missing` | global repair identity, progress, idempotency, and dry-run explanation; CP4 projection subset is separately evidenced | CP2/CP10 |
| P14 missing | `operation_scope_resolution_evidence_missing` | operation-to-resolved organization/project/team evidence | CP1+ |
| P15 missing | `repository_impact_coverage_relation_missing` | memory revision and impact-coverage revision relation | CP8 |

### CP4 Scoped P13 Subset

The projection-repair subset is a bounded CP4 evidence contract, not global
P13 closure. For one resolved organization/project and aware `as_of`, it is
healthy when exact dry-run is inert, authoritative mismatches remain
`report_only`, exact apply changes only deterministic document fields, and
embedding apply reuses one hash-fenced work/signal. Cursor, sample, and batch
limits are enforced. Dry-run reports repairable rows as `changed` (would
change) without writing them; apply reports reused embedding work as `skipped`.
Rerunning the same input converges to zero additional changes. Structured
command results are the bounded evidence; no cross-process Prometheus repair
counter is claimed. Durable repair-run identity, multi-batch progress, and
process-loss replay remain CP10 evidence and keep global P13
`missing_observability`.

## Count And Sampling Semantics

- P1 joins through `ObservationSource`, not the legacy
  `Observation.raw_event` reverse-relation proxy. Both total source-link count
  and same-scope valid-link count must equal one.
- P3 excludes lifecycle-only observations, derives the latest useful sequence
  for each CP3 session, and requires the exact matching work/window/stages and
  settled finalization. A prior successful upper sequence never covers a later
  failed generation. Legacy sessions retain `missing_observability`.
- P4 uses an aware effective time. A naive caller-supplied `as_of` is rejected.
- P7 evaluates only version-1 coherent-chain predicates for health. One memory
  may contribute more than once, while samples are deduplicated. Body and exact
  projection equality are compared in the database; content is never loaded
  into the report. Embedding absence is a work/operational-state check, not a
  semantic promotion violation.
- P8 and P9 are healthy only for a complete version-1 cohort. Version-0 rows
  are retained and reported as `missing_observability`; they are never inferred
  healthy from structural proxies or from a clean version-1 neighbor.
- P12 recognizes only a same-scope link from a same-scope memory whose target
  is `candidate:<uuid>` as a candidate conflict, and recognizes
  `Memory.status=conflict` as a memory conflict.
- P5 compares every completed CP3 window; samples contain only bounded typed
  entity ids and never content, anchors, prompts, or provider output. P6 uses
  only the registered exact source-manifest builder and intentionally does not
  claim global health before CP5.
- A zero `proxy_count` never turns `missing_observability` into `healthy`.
- The CP4 P13 projection subset is not a new global invariant result: exact
  rebuild and embedding enqueue outcomes are bounded, scoped evidence while
  historical repair-run observability remains missing until CP10.

The CP0 evaluator is read-only. It does not expose an API, command, scheduler,
repair path, or cross-project aggregate mode.
