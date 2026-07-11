# Checkpoint 7 Memory CI Temporal Foundation

Date: 2026-07-11

Status: focused parent implementation specification

Roadmap gate: Checkpoint 7, delivered as MCI-0 through MCI-4 in five serial branches and PRs

Base inspected for this specification: `79ddb15a`
## Authority And Goal

This specification extracts the executable Checkpoint 7 contract from:

- `2026-07-09-autonomous-memory-loop-roadmap.md`, “Checkpoint 7 — Memory CI Temporal Foundation”;
- `2026-07-09-memory-ci-feature-proposal.md`, especially MCI-0 through MCI-4;
- `memory-loop-invariants.md`, especially P11, P14, and P15;
- `memory-loop-fault-matrix.md`, especially F19 and the Memory CI cross-project control.

The goal is to establish a causal, project-scoped shadow loop:

    trusted default-branch revision
      -> immutable normalized change manifest
      -> exact version-scoped path/symbol anchors
      -> explainable deterministic impact plan
      -> durable memory-version/revision work
      -> deterministic shadow assertion

No production retrieval behavior changes in Checkpoint 7. It records enough evidence to measure temporal validity and to let Checkpoint 8 activate posture-aware
transitions and retrieval gating.

Each MCI slice is independently reviewable and mergeable. MCI-1 cannot begin until MCI-0 merges; each later slice depends on the immediately preceding slice. One branch
containing MCI-0 through MCI-4 is forbidden.
## Binding Invariants

The following rules are more important than implementation convenience:

1. Authorization resolves organization, project, configured repository stream, and trusted source key before a revision, artifact, anchor, plan, work item, assertion, or
   source path is read or written.
2. A received revision is not proof that any memory is current for that revision.
3. Revision coverage exposes three distinct monotonic boundaries: latest accepted canonical revision, latest complete impact-planned revision, and latest revision whose
   impacted pairs are durably accounted by an assertion or retryable logical work.
4. A complete impact plan is revision-scoped, immutable, explainable, and created at most once.
5. Every impacted memory-version/target-revision pair has one stable logical work identity. Attempts, deliveries, and evidence supplements never create a second logical
   identity.
6. Missing, partial, redacted, contradictory, or unauthenticated evidence cannot confirm, refute, supersede, revise, or mutate memory.
7. Path renames are normalized before deletion rules. An identical-content rename is not a deletion plus addition.
8. Exact path/symbol traversal is the only impact source in Checkpoint 7. Lexical and semantic recall expansion are absent.
9. Model calls are absent. Provider health cannot affect a deterministic CP7 result.
10. Shadow assertions and safe anchor-successor rows never update `Memory`,
    `MemoryVersion`, `RetrievalDocument`, context bundles, or search/context
    eligibility.
11. The same authenticated event, artifact, plan request, or validation input
    replays to the same durable rows and effect.
12. A stale lease/fence or superseded repository head cannot apply an anchor
    transition or a current-head assertion.
13. Raw revision evidence, plans, assertions, and anchor history are preserved;
    repair or rollback never deletes them to make a check pass.
14. `django-celery-outbox` remains the only transport authority. Memory CI
    owns logical progress, not package retry/dead-letter state.

P11 and P15 remain `missing_observability` through this checkpoint because production retrieval is intentionally unchanged. CP7 supplies the repository, anchor, plan, and
shadow relations that CP8 will use to make those invariants exact. P14 applies immediately to every new source-to-sink path.
## Current Code That Constrains The Design

The inspected `Project` model stores `repository_url`, `repository_root`, and `default_branch`. `engram.core.repository` canonicalizes Git URLs and resolves an authorized
project, but there is no SCM adapter, canonical revision ledger, revision ordering, manifest, or evidence attestation.

Current code-sensitive evidence is fragmented:

- `Observation.files_read` and `files_modified` retain recorded paths;
- `Memory.metadata["file_paths"]` copies candidate source paths;
- `RetrievalDocument.file_paths` and `symbols` are mutable retrieval projections;
- `IndexMemoryVersion` merges metadata, source-observation paths, and text-extracted symbols;
- `MemoryLink` has coarse `file` and `symbol` targets, is attached to a `Memory` rather than a version, and can be deleted;
- no row records anchor provenance, quality, repository revision, or temporal interval.

`authorized_retrieval_documents` currently filters by resolved organization/project/team, approved status, and coarse stale/refuted flags before exact, lexical, or
semantic ranking. `BuildContextBundle` and `SearchMemories` consume that same projection. CP7 must preserve their selection, order, rendered text, warnings, and replay
behavior.

`WorkflowWork` already provides immutable scoped identity and a stable input snapshot, but the current master implementation hard-codes observation, session, and digest
work/subject pairs. CP1 through CP6 are merge dependencies and will extend leases, fencing, attempts, and dispatch. MCI implementation must use the merged work primitive
and id-only package task adapter; it must not introduce a second queue or work table.

Current exact retrieval normalization case-folds values and permits suffix path matching. Memory CI anchors do not reuse that behavior: Git paths and code symbols are
case-sensitive exact evidence.
## Selected Architecture And Rejected Alternatives

The selected design adds one isolated `engram.memory_ci` Django domain. It owns its models, migrations, source contract, normalization, impact planning, shadow
validation, read model, and tests. Existing memory and retrieval models remain unchanged except for narrow promotion integration in MCI-2 and `WorkflowWork` enum/contract
integration in MCI-1/MCI-3.

The first trusted source is an authenticated project-bound CI artifact carrying normalized change structure and bounded SHA-256 fingerprints. This is selected because it
is provider-neutral, requires no server-side repository credential, fetches no submitted URL, stores no source blob, and can prove unchanged, rename, change, deletion,
and symbol-signature cases.

Rejected alternatives:

- Reuse `RetrievalDocument.file_paths/symbols` as canonical anchors. They are a mutable retrieval projection, lack provenance, and are not version-scoped.
- Extend `MemoryLink` into the temporal ledger. It is memory-scoped, manually mutable, and cannot represent fingerprint or revision intervals.
- Build a generalized graph or AST/indexing service. Exact PostgreSQL rows and a bounded JSON manifest prove the first causal loop with much less state.
- Let workers clone or execute a submitted repository. That crosses the trust boundary and turns Memory CI into a CI runner.
- Use semantic similarity to expand impact. It would obscure the exact precision/recall baseline and cannot authorize state.
- Mutate stale/refuted flags during shadow validation. That would silently activate CP8 retrieval behavior inside CP7.
## Checkpoint Boundary

In scope:

- one canonical repository stream and default branch per project;
- one configured, project-bound trusted CI API key;
- fast-forward revision edges from a configured baseline;
- out-of-order delivery of edges from the same non-branching chain;
- append-only evidence supplements for the same revision;
- exact repository-relative paths and qualified symbol names;
- deterministic changed, added/restored, renamed, and deleted evidence;
- deterministic content/signature fingerprint comparison;
- shadow posture-aware result codes;
- dry-run/resumable anchor backfill;
- scoped status, lag, coverage, and evaluation reports.

Out of scope:

- active temporal search/context filtering or warnings;
- automatic memory confirm/revise/merge/supersede/refute transitions;
- model adjudication, semantic impact expansion, or provider calls;
- configuration, dependency, migration, test, environment, command, runtime, issue, PR, or cross-repository anchor types;
- feature-branch/PR previews and branch-specific memory universes;
- force-push reconciliation, arbitrary DAG forks, or multiple canonical repositories per project;
- source blobs, snippets, repository checkout, arbitrary command execution, or server-side build/test execution;
- console/frontend product surfaces;
- production enablement, SSH, deploy changes, D2 work, production repair, or historical corpus mutation;
- retention deletion, generalized repair, and active rollout beyond a project-scoped shadow switch.
## Common Vocabulary
### Claim posture

Every profile uses exactly one posture:

- `descriptive`: claims what current code does;
- `prescriptive`: states an intended rule or decision;
- `historical`: records a past state or incident;
- `unknown`: safe migration state when existing data cannot be classified deterministically.

The deterministic default mapping is:

| Existing kind | Default posture |
|---|---|
| `decision`, `convention` | `prescriptive` |
| `incident`, `digest` | `historical` |
| `architecture` | `descriptive` |
| `gotcha` or blank | `unknown` |

An explicit trusted classification may replace `unknown`. A model-only classification is recorded as unresolved and cannot authorize anchor transitions. Prescriptive
drift is never mapped to refutation. Historical claims are excluded from current-code impact planning.
### Code sensitivity and anchor disposition

`code_sensitivity` is `code_sensitive`, `not_code_sensitive`, or `unknown`. `anchor_disposition` is:

- `anchored`: at least one active exact path/symbol anchor;
- `unanchored`: code-sensitive but exact provenance is absent;
- `not_required`: historical or explicitly not code-sensitive;
- `unknown`: posture/sensitivity is unresolved.

`unanchored_reason` is blank for `anchored/not_required` and otherwise one of `no_exact_provenance`, `redacted_provenance`, `ambiguous_symbol_without_path`, or
`unknown_claim_posture`.
### Evidence completeness

Every artifact is `complete`, `partial`, or `redacted`. Completeness is checked per impacted anchor, not trusted from the label alone. An effective evidence view is
complete only when every required before/after fingerprint is present, syntactically valid, and consistent across all append-only supplements.

Contradictory nonblank fingerprints are an `evidence_conflict`. They block validation for that revision; later delivery cannot silently overwrite either artifact.
### Coverage boundaries

`CanonicalRepositoryStream` stores:

- `latest_accepted_revision`;
- `latest_impact_planned_revision`;
- `latest_revalidation_accounted_revision`.

The derived read-model state is:

| State | Meaning |
|---|---|
| `uninitialized` | no accepted revision after the configured baseline |
| `impact_pending` | accepted head is newer than complete plan coverage |
| `accounting_pending` | a complete plan lacks durable work/assertion for at least one item |
| `covered_pending` | all pairs are durable; at least one work item is active/retrying |
| `covered_unknown` | all pairs are durable; latest assertions include unknown evidence |
| `covered_settled` | every impacted pair has a terminal deterministic shadow assertion |

`latest_revalidation_accounted_revision` may advance when every impacted pair has durable conservative pending work. It does not assert semantic validity. Zero-item plans
advance both planned and accounted boundaries atomically.
## Canonical Wire Contracts
### Revision envelope v1

The trusted source endpoint `POST /v1/memory-ci/revisions` accepts one bounded object:

```json
{
  "schema": "memory_ci_revision/v1",
  "project_id": "uuid",
  "repository_identity": "git@host:owner/repo.git",
  "default_branch": "main",
  "base_revision": "40-or-64-char-lowercase-hex",
  "head_revision": "40-or-64-char-lowercase-hex",
  "external_event_id": "stable-source-id",
  "external_artifact_id": "stable-artifact-id",
  "occurred_at": "UTC-RFC3339",
  "changes": [
    {
      "id": "c0001",
      "kind": "modified",
      "old_path": "apps/service.py",
      "new_path": "apps/service.py",
      "symbols": [
        {
          "id": "s0001",
          "kind": "modified",
          "old_name": "pkg.Service.run",
          "new_name": "pkg.Service.run"
        }
      ]
    }
  ],
  "evidence": {
    "completeness": "complete",
    "changes": [
      {
        "change_id": "c0001",
        "before_sha256": "64-char-lowercase-hex",
        "after_sha256": "64-char-lowercase-hex",
        "symbols": [
          {
            "symbol_change_id": "s0001",
            "before_sha256": "64-char-lowercase-hex",
            "after_sha256": "64-char-lowercase-hex"
          }
        ]
      }
    ]
  }
}
```

Allowed path `kind` values are `added`, `modified`, `deleted`, and `renamed`. Allowed symbol `kind` values are the same. `added` requires only a new target, `deleted`
only an old target, `modified` equal old/new target, and `renamed` distinct old/new target. Evidence uses null for a side that cannot exist.

The endpoint limits the serialized body to 1 MiB, changes to 5,000, symbols per change to 200, path strings to 1,024 characters, symbol strings to 512 characters, and
external ids to 255 characters. It rejects unknown keys, duplicate change/symbol ids, duplicate normalized transitions, and `base_revision == head_revision`.
### Normalization and hashing

Revision ids are lowercase hexadecimal SHA-1 or SHA-256 strings. Paths are Unicode NFC, use `/`, are repository-relative, contain no empty, `.`, or `..` segment, and
preserve case. Symbols are trimmed Unicode NFC, nonempty, and case-sensitive. A rename is normalized before any delete/add interpretation.

All dictionaries use sorted-key compact UTF-8 canonical JSON through the merged `canonical_json_bytes` primitive. Manifest, evidence, plan, reason, and assertion hashes
are lowercase SHA-256 of canonical bytes. Input ordering never changes a hash; persisted arrays are sorted by stable ids.
### Trusted evidence boundary

The endpoint requires:

1. an existing project id; repository URL auto-creation is forbidden;
2. `projects:agent` on a project-bound API key;
3. exact equality between the resolved API key id and the stream’s configured `trusted_api_key_id`;
4. exact canonical repository identity and default branch match;
5. `ENGRAM_MEMORY_CI_SHADOW_ENABLED=true` (default is false) and stream mode `shadow`.

The server never follows `repository_identity`, fetches a URL, clones a repository, stores a credential from the body, accepts a feature branch, or executes source.
Paths, symbols, fingerprints, source ids, and timestamps are data only. Server receipt time is authoritative for audit; edge continuity is authoritative for canonical
order.
### Idempotent response

`ingest_revision` returns:

```python
@dataclass(frozen=True, slots=True)
class IngestRevisionResult:
    revision_id: UUID
    artifact_id: UUID
    ledger_state: Literal["waiting_for_base", "accepted"]
    revision_created: bool
    artifact_created: bool
    accepted_revision_ids: tuple[UUID, ...]
```

An identical event/artifact replay returns the existing ids and `created=False`. The same external event id or head revision with a different structural manifest returns
`revision_identity_conflict`. The same artifact id with different evidence returns `evidence_identity_conflict`. A new evidence supplement for the same structural
revision appends a new artifact. Any contradictory known fingerprint returns `evidence_conflict` and changes no accepted evidence projection.
## Minimal Persistence Schema

All models live in `engram.memory_ci.models`, inherit `TimestampedModel`, carry organization/project scope, implement same-scope `clean()` checks, and use UUID foreign
keys. JSON fields accept only the versioned canonical contracts above.
### CanonicalRepositoryStream

Fields:

- organization and one-to-one project;
- immutable canonical `repository_identity` and `default_branch`;
- immutable `baseline_revision`;
- protected `trusted_api_key`;
- `mode`: `disabled` or `shadow`;
- nullable latest accepted, impact-planned, and revalidation-accounted revision foreign keys.

The project, repository identity, branch, and baseline are frozen when the stream is configured. Rebinding requires disabling the old stream and a later explicit
migration design; CP7 does not rewrite history.
### RepositoryRevision

Fields:

- stream, organization, and project;
- `base_revision`, `head_revision`, `external_event_id`, `occurred_at`, and server-authored `received_at`;
- immutable normalized `change_manifest` and `change_manifest_hash`;
- `ledger_state`: `waiting_for_base` or `accepted`;
- nullable positive `accepted_order` and `accepted_at`.

Unique constraints are stream/external-event-id, stream/head-revision, and stream/accepted-order when non-null. Accepted order is assigned under the stream row lock only
when `base_revision` equals the current canonical cursor: the configured baseline for the first edge, otherwise the accepted head.

After accepting an edge, the same transaction repeatedly accepts the unique waiting edge whose base equals the new cursor. Two waiting successors for one base fail closed
as `non_linear_revision_chain`. Out-of-order delivery is therefore durable and deterministic without trusting client timestamps.
### RepositoryEvidenceArtifact

Fields:

- stream, revision, organization, and project;
- immutable `external_artifact_id`;
- normalized `evidence_manifest` and `evidence_hash`;
- declared `completeness` and server-authored `received_at`.

Unique constraints are stream/external-artifact-id and revision/evidence-hash. Artifacts are append-only. `effective_evidence` merges only consistent nonblank values by
change id and symbol-change id; it reports missing/redacted/conflicting fields explicitly.
### MemoryClaimProfile

Fields:

- one-to-one memory version, organization, and project;
- `claim_posture`, `code_sensitivity`, `anchor_disposition`, and `unanchored_reason`;
- `classification_source`: `kind_default`, `explicit`, or `backfill`.

This profile does not alter the current memory projection. The one-to-one relation makes posture and anchoring version-specific and keeps old versions reconstructable.
### MemoryEvidenceAnchor

Fields:

- memory version, organization, and project;
- `anchor_type`: `path` or `symbol`;
- normalized `path` and optional `symbol`;
- indexed `target_key` (`path:<path>` or `symbol:<optional-path>:<qualified-name>`);
- `quality`: `exact` or `unresolved`;
- canonical `provenance` array;
- optional lowercase `content_fingerprint`;
- `active`, nullable `supersedes` self-link, nullable `valid_from_revision`, and nullable `valid_until_revision`.

A path anchor requires path and forbids symbol. A symbol anchor requires symbol and may carry a path. One active row per memory version/target key is allowed. Closing an
anchor and creating its successor occurs atomically; old rows are never edited except to set `active=False`, `valid_until_revision`, and `updated_at` once.
### RevisionImpactPlan

Fields:

- one-to-one target revision, organization, and project;
- ordered `included_revision_ids` from the prior planned cursor to target;
- `coalesced_manifest` and its fingerprint;
- `anchor_inventory_fingerprint` and combined `input_fingerprint`;
- `item_count`, `plan_fingerprint`, and `completed_at`.

There is no persisted “building” plan. The planner computes in memory, then commits the immutable plan, all items, their logical work, package signals, and coverage
pointer in one transaction. Row existence therefore means complete.
### RevisionImpactItem

Fields:

- plan, memory version, claim profile, organization, and project;
- canonical ordered `reasons` and `reasons_hash`;
- nullable `revalidation_work` foreign key filled before plan commit.

The unique key is plan/memory-version. Every reason contains exactly `code`, `anchor_id`, `change_id`, optional `symbol_change_id`, `old_target`, and `new_target`. Reason
codes are `path_added`, `path_modified`, `path_renamed`, `path_deleted`, `symbol_added`, `symbol_modified`, `symbol_renamed`, and `symbol_deleted`.
### ShadowValidationAssertion

Fields:

- impact item, organization, and project;
- `validator_contract_version=1`;
- ordered evidence artifact ids and `input_fingerprint`;
- `outcome`, ordered `reason_codes`, canonical `evidence_snapshot`, and canonical `anchor_transitions`;
- nullable `supersedes` prior assertion and `evaluated_at`.

The unique key is impact-item/contract-version/input-fingerprint. A supplement that changes effective evidence creates a successor assertion; replay of the same effective
input reuses the existing assertion.

Allowed outcomes are `confirmed_unchanged`, `confirmed_renamed`, `restored_equivalent`, `change_detected`, `deletion_detected`, `prescriptive_drift`,
`unknown_missing_evidence`, `unknown_evidence_conflict`, and `superseded_target`. These are shadow facts, not `Memory` status values.
## Work And Task Interfaces

MCI adds these logical identities to the merged CP1/CP2 work contract:

| Work type | Subject type | Subject id | Occurrence key |
|---|---|---|---|
| `repository_impact_plan` | `repository_revision` | revision UUID | empty |
| `memory_revalidation` | `memory_version` | memory-version UUID | target revision UUID |

The immutable snapshots are:

```json
{
  "schema": "repository_impact_plan_input/v1",
  "repository_revision_id": "uuid"
}
```

```json
{
  "schema": "memory_revalidation_input/v1",
  "impact_item_id": "uuid",
  "memory_version_id": "uuid",
  "target_revision_id": "uuid"
}
```

The identity projection for revalidation is memory-version id plus target revision id. Evidence artifact ids are deliberately excluded so a later supplement resumes the
same logical work. Each attempt records the effective evidence fingerprint it evaluated.

Only these id-only package tasks are permitted:

```python
plan_repository_revision_work_v1(work_id: str) -> None
validate_memory_revision_work_v1(work_id: str) -> None
```

The task reloads work, scope, subject, plan/item, lease, and fencing token. A payload containing a project id, revision hash, path, memory id, evidence, or decision is
rejected. Dispatch occurs only when logical work is newly created and uses the merged deterministic `workflow-work:<UUID>` task id adapter.

MCI-1 acceptance and impact-planning work/package creation share one database transaction. MCI-3 plan/items/revalidation work/packages share one transaction. Unknown
evidence leaves revalidation work retryable under CP2 backoff. A newer accepted head resolves obsolete target work as `superseded_revision` only after a replacement
planning work identity exists.
## MCI-0 — Evaluation Contract And Baseline
### Owned files

- `apps/backend/engram/memory_ci/__init__.py`;
- `apps/backend/engram/memory_ci/evaluation.py`;
- `apps/backend/engram/memory_ci/evaluation_tests.py`;
- `apps/backend/engram/memory_ci/fixtures/memory_ci_v1.json`;
- `docs/reliability/memory-ci-evaluation-contract.md`;
- `docs/reliability/checkpoints/2026-07-11-mci0-baseline.json`.

MCI-0 does not add the app to `INSTALLED_APPS`, add a migration, or change a runtime import. Evaluation code is test/report-only.
### Corpus contract

Each sanitized case declares:

- before/after repository tree fingerprints;
- ordered revision envelopes and delivery order;
- memory versions with posture/sensitivity;
- exact and unresolved anchors with provenance;
- expected impact memory-version ids and ordered reasons;
- expected assertion and anchor transition;
- expected logical work count;
- expected search/context delta, always empty through MCI-4;
- a foreign project control with analogous identifiers.

Required cases include unrelated edit, content-preserving edit, content change, exact file rename, changed rename, file deletion, file restoration/revert, unchanged
symbol, signature change, symbol deletion, prescriptive drift, historical incident, missing evidence, redacted evidence, contradictory supplement, duplicate
event/artifact, out-of-order rapid revisions, planner replay, validator replay, stale-head fencing, rollback, and cross-project isolation.
### Metrics and thresholds

The committed baseline records numeric or explicitly `not_observable` values for exact impact precision/recall, false-stale rate, stale-injection rate,
destructive-decision rate, deterministic assertion accuracy, duplicate effect count, and cross-project leak count.

CP7 acceptance thresholds are fixed:

- exact corpus impact precision = 1.000;
- exact corpus impact recall = 1.000;
- deterministic unchanged/rename/delete/change classification = 1.000;
- missing/redacted/conflicting evidence mapped to an unknown result = 1.000;
- duplicate revision, plan, work, assertion, and anchor effects = 0;
- cross-project reads/writes/selections = 0;
- `Memory`/`MemoryVersion`/`RetrievalDocument` semantic mutations = 0;
- active search/context item ids, ordering, flags, warnings, and rendered bytes are unchanged from the MCI-0 baseline.
### RED and gate

The first test asserts the full manifest schema, required cases, foreign controls, and current `not_observable` temporal relations. It fails if a case lacks an exact
expected outcome or contains source content, credentials, real tenant ids, or absolute paths.

MCI-0 merges only when the evaluator reproduces the committed baseline byte-for-byte and the focused contract contains numeric thresholds above.
## MCI-1 — Canonical Revision Ledger
### Owned files

- `apps/backend/engram/memory_ci/apps.py`, `models.py`, and `migrations/0001_initial.py`;
- `apps/backend/engram/memory_ci/contracts.py` and `normalization.py` with focused tests;
- `apps/backend/engram/memory_ci/revision_ingest.py` and tests;
- `apps/backend/engram/memory_ci/serializers.py`, `views.py`, `urls.py`, and API tests;
- `apps/backend/engram/memory_ci/tasks.py` and work/task tests;
- `apps/backend/engram/memory_ci/management/commands/engram_memory_ci_stream.py` and tests;
- narrow `settings/settings.py` and `settings/urls.py` registration;
- the merged `WorkflowWork` enum, validation, and migration files required for `repository_impact_plan/repository_revision` only.

One schema owner makes every model/migration/work-enum change. Adapter, API, and test work begins only after that commit freezes the revision contract.
### Stream configuration

`engram_memory_ci_stream --project-id P --mode shadow --baseline-revision R --trusted-api-key-id K` resolves one project and one project-bound API key, requires a
canonical repository URL and nonblank default branch, writes no secret, and audits the configuration. `--mode disabled` stops new acceptance and dispatch but preserves
the ledger.
### Acceptance transaction

1. Resolve API-key scope and exact stream before parsing paths/symbols.
2. Enforce body bounds and normalize/hash structural and evidence manifests.
3. Lock the stream.
4. Create/reuse the immutable revision edge and evidence artifact; reject any identity/evidence collision without partial writes.
5. Starting at the canonical cursor, accept every unique contiguous waiting edge in order.
6. For each newly accepted edge, create/reuse one impact-planning `WorkflowWork` and its id-only package signal.
7. Advance `latest_accepted_revision` only after all accepted edges and their durable work/package rows exist.
8. Commit and return all revision ids accepted by this request.

An API/process failure at any numbered write boundary rolls back that boundary. A waiting edge remains durable but does not move coverage or create planning work until
its base becomes canonical.
### RED tests and gate

Required tests:

- `test_revision_acceptance_rolls_back_event_work_and_package_together`;
- `test_duplicate_revision_and_artifact_replay_one_effect`;
- `test_same_event_or_head_with_different_manifest_conflicts`;
- `test_same_artifact_id_with_different_evidence_conflicts`;
- `test_out_of_order_chain_accepts_when_missing_base_arrives`;
- `test_two_successors_for_one_base_fail_closed`;
- `test_rapid_chain_creates_one_work_per_accepted_edge`;
- `test_partial_and_redacted_evidence_remain_explicit`;
- `test_consistent_evidence_supplement_is_append_only`;
- `test_contradictory_evidence_supplement_changes_no_projection`;
- `test_untrusted_unbound_branch_or_repository_source_writes_nothing`;
- `test_foreign_repository_revision_cannot_read_or_write_target_scope`;
- `test_task_payload_is_work_id_only`;
- `test_worker_crash_replays_same_planning_work`.

The first RED run fails because the ledger relation and work pair do not exist. The gate requires all tests above, migration forward/reverse on an empty test database, no
model drift, and no import from SCM/provider/command-execution libraries.
## MCI-2 — Typed Path And Symbol Anchors
### Owned files

- `apps/backend/engram/memory_ci/migrations/0002_claim_profiles_and_anchors.py`;
- serial additions to `memory_ci/models.py`;
- `apps/backend/engram/memory_ci/anchors.py` and `anchors_tests.py`;
- `apps/backend/engram/memory_ci/management/commands/engram_memory_ci_backfill_anchors.py` and tests;
- narrow profile/anchor creation integration in `apps/backend/engram/memory/services.py` and focused promotion tests;
- MCI-0 corpus/score updates produced by the same evaluator.
### Exact provenance policy

Backfill sources are evaluated in this order:

1. valid file/symbol `MemoryLink` snapshots;
2. the version’s source observation `files_read/files_modified`;
3. exact file paths in memory metadata that equal source-observation paths;
4. retrieval projection values only when they can be traced to item 1–3.

`RetrievalDocument.symbols` extracted from prose and untraceable metadata symbols become `unresolved` inventory, not exact anchors. Invalid paths, redacted strings,
absolute paths, ambiguous symbols, and model-only values do not become actionable. Provenance objects name source type/id and extraction rule without copying source text.

For a new promoted memory version, profile plus exact anchors or explicit unanchored disposition commit in the same CP4 semantic-transition transaction. Failure cannot
leave a current version without one of those dispositions. Historical/not-code-sensitive versions record `not_required`.
### Backfill contract

The command requires organization and project, supports `--dry-run`, `--resume-after <memory-version-uuid>`, and `--batch-size` from 1 through 500. It orders UUIDs,
reports created/reused/unresolved/unanchored counts, and never changes memory/retrieval rows. Re-running any prefix or resuming after a crash converges on the same active
anchors and provenance arrays.
### RED tests and gate

Required tests:

- `test_path_normalization_rejects_absolute_parent_and_case_collisions`;
- `test_symbol_identity_is_case_sensitive_and_optionally_path_scoped`;
- `test_exact_sources_merge_into_one_anchor_with_all_provenance`;
- `test_extracted_or_model_only_symbol_is_unresolved`;
- `test_new_code_sensitive_version_is_anchored_or_explicitly_unanchored`;
- `test_historical_version_requires_no_code_anchor`;
- `test_anchor_backfill_dry_run_writes_nothing`;
- `test_anchor_backfill_resume_and_replay_are_idempotent`;
- `test_anchor_backfill_rollback_leaves_no_partial_batch`;
- `test_foreign_provenance_cannot_create_target_anchor`;
- `test_anchor_creation_does_not_change_retrieval_results_or_flags`.

The first RED run fails on the absent profile/anchor relation. The gate requires 100% classification of new fixture versions into anchored, unanchored, not-required, or
unknown; zero model-only exact anchors; and the MCI-0 retrieval parity baseline unchanged.
## MCI-3 — Deterministic Shadow Impact Planning
### Owned files

- `apps/backend/engram/memory_ci/migrations/0003_impact_plans.py`;
- serial additions to `memory_ci/models.py`;
- `apps/backend/engram/memory_ci/impact.py` and `impact_tests.py`;
- impact-planning task adapter/tests in `memory_ci/tasks.py`;
- the merged `WorkflowWork` enum, validation, and migration files required for `memory_revalidation/memory_version`;
- MCI-0 evaluation report updates.
### Coalescing

Under the stream lock, a planner chooses the newest contiguous accepted, unplanned head. It folds all edges after `latest_impact_planned_revision` (or the baseline) into
one deterministic manifest:

- rename chains collapse old A to final C while retaining all source change ids;
- modify sequences retain first before and final after fingerprints;
- delete then add at the same path becomes a replacement/restoration;
- add then delete inside the window cancels when no pre-window anchor could reference it;
- an exact revert remains a changed path whose final fingerprint may equal the stored anchor;
- evidence conflicts remain explicit and are never collapsed away.

Older planning work is resolved as superseded only in the transaction that creates the target plan and accounts for the complete accumulated change.
### Exact traversal

The planner loads only active `quality=exact` anchors for the resolved project and code-sensitive descriptive/prescriptive profiles.

- added/modified/deleted paths match exact anchor paths;
- rename matches old-path anchors and exact new-path anchors;
- symbol changes match exact qualified names; a path-scoped symbol requires both path and name;
- historical, not-code-sensitive, unresolved, and foreign anchors are excluded;
- every matched anchor/change pair produces one stable reason;
- reasons and items are sorted before hashing;
- no match produces a valid zero-item complete plan.

The anchor inventory fingerprint covers every eligible anchor id, target key, quality, content fingerprint, and active interval used. A completed plan never changes when
later anchors arrive.
### Plan transaction

1. Recheck stream head, prior planned cursor, and planning fence.
2. Recompute normalized input fingerprints and reuse an identical existing plan or fail closed on collision.
3. Create the plan and one item per distinct memory version.
4. Create/reuse one revalidation work identity per item/target revision.
5. Dispatch an id-only task only for newly created work.
6. Set each item’s work reference.
7. Advance planned and accounted coverage only when every item has durable work; a zero-item plan advances both.
8. Commit atomically.
### RED tests and gate

Required tests:

- `test_unrelated_change_produces_complete_zero_item_plan`;
- `test_changed_path_selects_only_exact_anchored_versions`;
- `test_exact_rename_is_one_reason_not_delete_plus_add`;
- `test_changed_rename_and_delete_select_the_old_anchor`;
- `test_added_restored_path_selects_existing_anchor`;
- `test_existing_symbol_change_requires_exact_name_and_optional_path`;
- `test_unresolved_and_historical_anchors_are_not_selected`;
- `test_rapid_revisions_coalesce_without_losing_intermediate_reasons`;
- `test_duplicate_revision_and_concurrent_planners_create_one_plan_and_work`;
- `test_plan_rollback_leaves_no_plan_item_work_package_or_coverage_advance`;
- `test_replay_returns_the_immutable_plan`;
- `test_foreign_anchor_never_enters_target_plan`;
- `test_planning_creates_no_memory_retrieval_or_context_mutation`.

The first RED run fails on absent plan relations. The gate requires impact precision and recall of 1.000 on the exact corpus, one plan per target, one work per impacted
pair, no global revalidation, and no semantic/provider activity.
## MCI-4 — Deterministic Shadow Validation
### Owned files

- `apps/backend/engram/memory_ci/migrations/0004_shadow_assertions.py`;
- serial additions to `memory_ci/models.py`;
- `apps/backend/engram/memory_ci/validation.py` and `validation_tests.py`;
- validation task adapter/tests in `memory_ci/tasks.py`;
- scoped coverage/status additions in `memory_ci/read_models.py` and tests;
- MCI-0 evaluation report updates.
### Validator order

For one impact item:

1. Reload scoped work, item, plan, target revision, current stream head, version/profile, active anchors, lease, and fence.
2. If target is no longer current, persist/reuse `superseded_target` without anchor mutation; replacement planning work must already exist.
3. Resolve the latest consistent effective evidence and compute the exact input fingerprint.
4. Missing/redacted required fields yield `unknown_missing_evidence`; contradictions yield `unknown_evidence_conflict`. Work remains retryable.
5. Compare symbol signature fingerprints before file fingerprints when a path-scoped symbol anchor is the impact reason.
6. Classify each exact reason, then aggregate posture-aware outcome.
7. Recheck stream head, active anchor ids, lease, fence, and input fingerprint inside one transaction.
8. Persist/reuse the assertion and any safe equivalent-evidence anchor successors.
9. Resolve terminal work or retain retry state, then refresh only the Memory CI coverage read model.
### Deterministic rules

- Equal before/after fingerprint for the impacted anchor is `confirmed_unchanged`.
- A rename with matching before/after fingerprint is `confirmed_renamed` and closes the old anchor while creating an exact successor at the new target.
- A stored anchor fingerprint equal to the final after fingerprint after an intervening change/delete is `restored_equivalent`.
- Unequal known fingerprints are `change_detected`.
- A known before fingerprint with an absent after side is `deletion_detected`.
- If any impacted reason is changed/deleted and posture is prescriptive, the aggregate result is `prescriptive_drift`; the rule remains intact.
- All impacted exact reasons must be equivalent for aggregate confirmation.
- A newly learned equivalent fingerprint may create a successor anchor state, but cannot certify the pre-baseline claim or mutate memory.
- Historical/unknown-posture input cannot produce a destructive or confirmed current claim; it yields a conservative unknown assertion if encountered.
- No rule produces revise, supersede, refute, merge, conflict, or retrieval eligibility.

Anchor transitions are idempotent on old-anchor/target-revision/new-target. The old row and successor, assertion, and work disposition commit or roll back together.
### RED tests and gate

Required tests:

- `test_equal_file_fingerprints_confirm_without_model_call`;
- `test_equal_symbol_signature_confirms_despite_unrelated_file_change`;
- `test_identical_rename_creates_one_anchor_successor`;
- `test_changed_rename_does_not_move_anchor`;
- `test_changed_and_deleted_descriptive_evidence_are_signals_only`;
- `test_prescriptive_change_records_drift_and_never_refutes_rule`;
- `test_revert_to_stored_fingerprint_records_restored_equivalent`;
- `test_missing_redacted_or_conflicting_evidence_is_unknown_and_retryable`;
- `test_new_evidence_supplement_reuses_work_and_supersedes_unknown_assertion`;
- `test_duplicate_and_concurrent_validation_create_one_assertion_effect`;
- `test_newer_head_and_stale_fence_block_anchor_transition`;
- `test_validation_rollback_preserves_old_anchor_and_required_work`;
- `test_foreign_revision_cannot_change_target_anchor_or_assertion`;
- `test_shadow_validation_preserves_search_and_context_byte_parity`.

The first RED run fails on the absent assertion/validator. The gate requires deterministic classification accuracy 1.000, no model/provider calls, safe rename transition
replay, unknown-on-missing behavior 1.000, and exact MCI-0 retrieval parity.
## File Ownership And Merge Discipline

| Slice | Serial owner | Files that no parallel worker may touch |
|---|---|---|
| MCI-0 | evaluation owner | fixture, evaluator, baseline, evaluation contract |
| MCI-1 | revision/schema owner | `memory_ci/models.py`, initial migration, work enums, settings/URLs |
| MCI-2 | anchor/schema owner | `memory_ci/models.py`, anchor migration, promotion integration |
| MCI-3 | plan/schema owner | `memory_ci/models.py`, plan migration, work enums/tasks |
| MCI-4 | validation/schema owner | `memory_ci/models.py`, assertion migration, tasks/read model |

Within one slice, adapters/tests may proceed after the shared contract commit, but models, migrations, work identities, and `tasks.py` always have one owner. The
campaign’s main agent remains the only Git owner. Later specs may be refreshed early, but no CP7 code merges before CP6 and no MCI slice merges out of order.

At each implementation start, compare the named integration points with the merged CP1–CP6 interfaces. Adapt import locations, never the identities, transactions, trust
rules, or gates frozen here.
## CI And Verification Gates

Every slice records one focused RED command and decisive failing assertion, then runs focused GREEN tests in Compose. Python, backend, CLI, plugin, and E2E tests do not
run on the host.

Focused command shape:

```sh
docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && pytest -q engram/memory_ci"
```

MCI-1 through MCI-4 additionally run adjacent contracts:

```sh
docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && python manage.py migrate --noinput && python manage.py makemigrations --check --dry-run && python manage.py check && pytest -q engram/memory_ci engram/core/repository_tests.py engram/memory/workflow_work_tests.py engram/memory/memory_links_tests.py engram/memory/memory_versioning_tests.py engram/context/services_tests.py engram/context/context_api_tests.py engram/search/services_tests.py engram/search/search_api_tests.py && ruff check engram/memory_ci engram/core/repository.py engram/memory/workflow_work.py && ruff format --check engram/memory_ci"
```

The authoritative PR gate runs the full existing backend Compose/CI suite serially, migration checks, Ruff check/format, and the MCI-0 evaluator. No pytest command uses
more than two workers; these commands use one.

Each PR must prove:

- exact test count, command, exit code, and migration state;
- no unowned file changes;
- no source blob, credential, absolute path, or real tenant data in fixtures, logs, audit metadata, or responses;
- no new network/SCM/command-execution dependency;
- no write to production, SSH, deployment, or D2 state;
- retrieval parity and zero semantic mutation;
- one correctness review and one Karpathy simplicity review.
## Observability

The internal project-scoped read model returns:

- configured baseline and current accepted/planned/accounted heads;
- derived coverage state;
- waiting-edge count and oldest age;
- accepted-to-plan and plan-to-assertion lag;
- complete/partial/redacted/conflicting evidence counts;
- exact/unresolved/unanchored version counts;
- plan item and active/retry/unknown/settled work counts;
- assertion outcome counts;
- duplicate/collision counters.

All queries begin with exact organization/project/stream scope. Samples are deterministically ordered, entity-qualified, and capped at 20. No cross-project aggregate,
console page, public evidence browser, source snippet, or credential identifier is introduced in CP7.
## Retention, Disable, And Rollback

Revision edges, evidence artifacts, completed plans/items, assertions, and closed anchors are retained for the project’s lifetime and cascade only with an authorized
project deletion. Fingerprints are retained; source blobs are never accepted. A stream disable does not delete or rewrite history.

The operational rollback is project-scoped first and global second:

1. set the stream to `disabled` to reject new events and stop new dispatch;
2. disable the global Memory CI shadow flag to stop all streams;
3. allow already claimed tasks to fail their mode/head/fence recheck without applying anchor transitions;
4. leave work, artifacts, plans, assertions, and anchor history inspectable;
5. resume by re-enabling the same stream; idempotent identities continue from the preserved coverage boundary.

All four schema migrations are additive. A migration reverse is permitted only on an empty non-production database before the next MCI slice is applied. Once evidence
exists, rollback means disable/inert preservation, not dropping audit state. MCI-2 backfill can be restarted from its cursor; it has no destructive undo. Current
search/context behavior needs no rollback because it never changes.
## Checkpoint 7 Definition Of Done

Checkpoint 7 is complete only when:

- all five serial PRs merged in order after CP6;
- the trusted CI source can record and replay an out-of-order canonical chain without duplicate event/work/package effects;
- incomplete evidence remains explicit and supplementable;
- every new version has a posture/sensitivity profile and exact anchors or an explicit non-actionable disposition;
- path/symbol backfill is dry-run, scoped, resumable, and idempotent;
- deterministic exact impact precision and recall are both 1.000;
- every complete plan is immutable, explainable, revision-scoped, and atomically paired with durable revalidation work;
- unchanged, rename, change, delete, restoration, prescriptive drift, and unknown evidence produce the exact shadow outcomes above;
- stale heads and stale workers cannot apply anchor transitions;
- duplicate/concurrent/replay/rollback and foreign-scope tests pass;
- no provider call, source execution, semantic transition, retrieval mutation, production operation, or deployment change exists;
- search/context parity with the MCI-0 baseline is exact;
- the accepted/planned/accounted boundaries and pending/unknown lag are observable by project;
- data retention and disable/resume behavior are tested and documented.

MCI-5 may begin only after this gate is reviewed and the measured MCI-0 threshold report remains green. Active temporal retrieval remains blocked until MCI-6.
