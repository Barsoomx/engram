# S4 memory-propose — Agent-authored memory channel through curation

Slice: S4 (backend + client). One coherent slice: a new data-plane endpoint
`POST /v1/memories/propose` that lets an agent deliberately record a durable
fact, routed through the existing candidate-decision curation pipeline, plus the
MCP tool + CLI + wizard changes that expose it.

## Problem and Evidence

The capability `memories:propose` is seeded everywhere but **no view requires
it**, so an agent has no deliberate write path to durable memory. Verified:

- Granted to the bootstrap admin key:
  `apps/backend/engram/core/management/commands/engram_bootstrap_admin.py:46`
  (`'memories:propose'` inside the admin capability tuple).
- Granted to the `admin` and `developer` roles:
  `apps/backend/engram/access/migrations/0002_seed_default_roles.py:16` (defined),
  `:39` (admin), `:54` (developer).
- Declared in the default role capability set assertion:
  `apps/backend/engram/access/access_scope_tests.py:132,143`.
- `grep -rn 'memories:propose'` over `apps/backend` shows **only** seed data and
  tests — **zero** `resolve_request_scope(..., required_capability='memories:propose')`
  call sites. Every existing memory write path uses a different capability:
  feedback/version → `memories:review`
  (`apps/backend/engram/memory/views.py:62,152`), imports/admin →
  `memories:admin` (`apps/backend/engram/console/views/import_cancel.py:21`).

So today durable memory is reachable only by passive capture → distillation, or
the privileged operator import API. There is no first-class "the agent verified a
durable fact, record it" channel.

Key implementation constraints discovered while verifying (these shape the
design, not just the endpoint):

1. `MemoryCandidate.body` is a non-blank `TextField` and `TimestampedModel.save`
   runs `full_clean` — `apps/backend/engram/core/models.py:657-658`. An empty
   body was a real prod crash (see MEMORY note "empty-body candidate crash"). The
   propose path must guard against blank title/body **after redaction**.
2. `clamp_memory_kind` — `apps/backend/engram/core/models.py:137-141` — returns
   `''` for `'digest'` and for anything outside `MEMORY_KINDS`
   (`:134`). Agent-supplied `kind` must be clamped, never trusted.
3. `MemoryCandidateSource` — `apps/backend/engram/core/models.py:2220-2360` — has
   a check constraint `core_candidate_source_shape_ck` (`:2288-2304`) that is
   exactly **distillation XOR import**, and `observation` is a **non-null** FK
   (`:2244`). There is no shape for an agent proposal (no window/stage/observation).
4. **The candidate-decision path is pervasively distillation-shaped, far beyond
   the two evidence-manifest functions.** A proposed candidate gets
   `decision_work_contract_version=1` (candidate decisions enabled by default,
   #265) and runs the full v1 orchestrator: deterministic gates → shortlist →
   judge → transition. Every stage dereferences `source.observation` and/or
   rejects non-distillation/non-import source kinds. An `agent_proposal` source
   (null observation, no window/stage) crashes or is rejected at **every** stage
   unless each is given an explicit agent branch. The complete verified blast
   radius (all must change — findings 1 and 2):

   **Evidence manifest (2 fns):**
   - `evidence_manifest` / `_source_value`
     `candidate_decision_work.py:81-148` — derefs `source.window.input_hash`,
     `source.observation.session_sequence`, `source.stage.stage_key`.
   - `candidate_evidence_manifest` `import_provenance.py:161-187` — raises on any
     kind that is not pure distillation or pure import.

   **Deterministic gates (`deterministic_gates.py`):**
   - `_validate_sources` (`:257-285`) derefs `source.observation` at `:265-267`
     **before** the kind check, and its trailing `else` (`:284-285`) raises
     `'unsupported candidate source kind'` for any kind that is not distillation
     or import. An agent source therefore raises → the worker surfaces
     `stale_decision`/scope error before shortlist. (Finding 1.)
   - the NOISE_LIFECYCLE_ONLY gate (`:520-525`) derefs
     `source.observation.observation_type` for every source. Null observation →
     crash. (Finding 1.)

   **Judge evidence tiering (`curation_judge.py`):**
   - `_eligible_group_hash` (`:196-215`) returns `None` for any non-distillation
     source, so an agent-only candidate accumulates zero group hashes →
     `_claim_evidence` (`:165-178`) assigns tier `none`.
   - `_apply_evidence_policy` (`:369-434`) allows `publish_new`/`merge_evidence`
     only when `candidate_tier in _SUPPORTED_TIERS` (`{'supported',
     'corroborated'}`); tier `none` permits **only** `reject_candidate` with
     relation `unsupported`. A novel agent proposal can therefore never be
     promoted or merged. (Finding 2 — judge side.)
   - `_source_evidence_time` (`:181-184`) derefs `source.observation`.

   **Transition state machine (`transitions.py`):**
   - `_source_rows` (`:396-440`), called by **all six** transitions (promote
     `:1409`, merge `:1602`, revise `:1916`, supersede `:2104`, conflict `:2385`,
     reject `:2544`), hydrates `source.observation = observations[
     source.observation_id]` (`:419`) → `KeyError` on a null observation_id, and
     its `sort_key` derefs `observation`/`window`/`stage`.
   - `_candidate_fence` (`:288-323`) rejects sources whose kind ≠ the passed
     `allowed_source_kind` (`:299`) and, on the non-import branch, reconstructs
     the canonical content hash from an observation session (`:308-317`) that an
     agent proposal lacks → `stale_decision`.
   - every transition call site passes `allowed_source_kind=DISTILLATION`
     (or IMPORT) hardcoded (`:1422-1423,1607,1927,2115,2408,2570`).
   - `_promotion_uses_import_source` (`:1311-1332`) raises `'candidate provenance
     has mixed source kinds'` for any non-import, non-distillation kind (`:1328`).
     (Finding 2 — promotion side.)

   All are on the live v1 path (`DecideMemoryCandidate.execute` `curation.py:1067`
   → gates/shortlist/judge/transition). See **Curation-path changes** below for
   the required agent branch in each.
5. Live dispatch pattern for a freshly created candidate (mirror it):
   `apps/backend/engram/memory/distillation.py:574-594` — inside the finalize
   transaction, `ensure_candidate_decision_work_locked(candidate)` then
   `queue_work_attempt(...)` for newly created work, then set
   `candidate.decision_work_contract_version = 1`.
6. The candidate-work reconciler safety net today (a) only repairs candidates that
   have a **distillation** source (`Exists(durable_sources)` filtered on
   `source_kind=DISTILLATION` — `apps/backend/engram/memory/candidate_work_reconciler.py:159-164,267-278`)
   and (b) in `_repair_candidate` returns early on **any** live `QUEUED`/`RUNNING`
   run (`:282-286`), so it re-dispatches only work with **no** active run
   (`_requeue_eligible`, `:280`) and **never re-signals a stale `QUEUED` run** — the
   exact state a lost broker delivery leaves behind. The session reconciler already
   has this missing path (`session_work_reconciler._classify_queued` `:138-147`
   classifies a stale `QUEUED` run as `ATTEMPT_SIGNAL_STALE` and calls
   `queue_work_attempt` to re-signal it). This slice therefore **extends** the
   candidate reconciler to cover single-`agent_proposal` candidates **and** adds the
   stale-`QUEUED` re-signal (Curation-path changes §6), because in-transaction
   (outbox) dispatch alone does not survive a stalled outbox relay or a lost broker
   delivery. The reconciler plus atomic outbox dispatch are the two layers that
   guarantee the work runs.
7. Scope/actor fields available for provenance:
   `EffectiveScope` exposes `api_key_id`, `actor_type`, `actor_id`
   (`apps/backend/engram/access/services.py:43-51`; for a bearer key
   `actor_type='api_key'`, `actor_id=str(key.id)` — `:208-214`).
8. `AccessDeniedError` is a `DomainError` with `status_code`
   (`apps/backend/engram/access/services.py:74-79`); `missing_capability` → 403
   (`:61`). It is rendered by `engram.core.middlewares.custom_exception_handler`
   (`apps/backend/settings/settings.py:276`) — the view does not catch it.
9. Client: MCP tools are declared in `packages/cli/engram_cli/mcp_server.py`
   (feedback tool at `:184-201`) and handled in
   `packages/cli/engram_cli/mcp_tools.py` (`submit_memory_feedback` `:333-372`,
   `_scope_payload` `:382-391`, `_new_request_id` `:394-397`, registry `bind`
   map `:131`). CLI subcommands are wired in
   `packages/cli/engram_cli/main.py:65-71,171-199` and implemented in
   `packages/cli/engram_cli/commands.py` (`run_memory_version` `:2095`). The
   connect wizard's requested capabilities are
   `WIZARD_API_KEY_CAPABILITIES` — `packages/cli/engram_cli/commands.py:57-61` —
   and do **not** include `memories:propose`.
10. The plugins bundle byte-identical copies at
    `packages/claude-plugin/hooks/engram_cli/` and
    `packages/codex-plugin/hooks/engram_cli/`, regenerated by
    `scripts/sync_plugin_bundle.py` (`SOURCE_DIR`/`BUNDLE_DIRS` `:10-13`,
    `--check` mode). Never hand-edit the plugin copies.

## Design

Decisions (already fixed by the teamlead brief; expanded and made precise here).

- **New source variant `agent_proposal`, not a marker field on the candidate.**
  Add `MemoryCandidateSource.source_kind = 'agent_proposal'` and make
  `observation` nullable so the row can carry actor provenance with no
  distillation lineage. Rejected: a boolean/enum on `MemoryCandidate` with no
  source row — loses the typed, audited provenance the team wants and still
  forces the empty-manifest edits below.
- **Provenance lives in the existing `anchors` JSON + `anchors_hash`**, no new
  columns. Rejected: dedicated `actor_api_key` FK — new column and migration for
  data already implied by the scope.
- **Confidence is never accepted from the agent.** `confidence` stays `NULL`;
  the deterministic gates + shortlist + judge decide. Rejected: an optional
  confidence param — agent-supplied confidence is fabricated signal (see MEMORY
  "distillation confidence provenance").
- **Route through the existing `CANDIDATE_DECISION` orchestrator**, dispatched
  transactionally exactly like distillation. Rejected: direct-to-approved
  creation — bypasses dedup/conflict/judge and is out of scope.
- **`agent_proposal` becomes a first-class source kind threaded through the
  entire v1 curation path**, not a two-function patch. Each distillation-shaped
  stage (evidence manifest ×2, deterministic gates, judge evidence tiering, and
  the transition state machine) gains an explicit agent branch (see
  **Curation-path changes** and Evidence constraint 4 for the full list). The
  agent branch keeps the same deterministic hash across the two evidence-manifest
  callers via a single shared helper. Rejected: giving the agent source a fake
  distillation shape — impossible without a real window/stage/observation.
- **An agent proposal carries a single synthetic evidence group → judge tier
  `supported`, with a `None` evidence time.** The judge's evidence tier counts
  independent distillation observation groups; an agent proposal has none but
  represents one deliberate, audited assertion, so `_eligible_group_hash` returns
  a stable token derived from the source's `anchors_hash` (validated inline —
  finding: target-traversal validation). An agent source has **no orderable
  observation lineage**, so its evidence time is `None` (not `source.created_at`),
  and that choice is what makes the intended terminal set actually reachable in
  `_apply_evidence_policy` (`curation_judge.py:369-434`):
  - **`publish_new`** — tier `supported` + `comparison_complete` (`:390`). ✓
  - **`merge_evidence`** into an equivalent supported target (`:392`). ✓
  - **`reject_candidate`/`redundant`** against a supported target (`:416-417`). ✓
    Rejection as `unsupported` requires candidate tier `none` (`:418-419`) and is
    therefore **not** applicable to a deliberate, supported assertion — by design.
  - **`open_conflict`** against a contradicting supported target (`:420-431`).
    This branch requires `not _deterministic_precedence` (`:430`), i.e. candidate
    and target evidence times must be equal **or one must be `None`**
    (`:363-366`). A real agent source created "now" has a `created_at` that
    differs from every target's observation time, so a **non-null** agent evidence
    time would make deterministic precedence hold and **block every conflict** —
    leaving a contradictory proposal with no valid verdict (`judge_policy_denied`,
    a retryable `_operational` error at `curation.py:1290,1062`). There is **no
    finite attempt budget** (finding: attempt budget): an ordinary retryable
    failure moves the work to `RETRY_WAIT` with a backoff-capped `next_retry_at`
    (`work_execution.py:807-824`, `work_failures.py:163-170` caps only the *delay*,
    not the *count*) and the reconciler re-queues due `RETRY_WAIT` work
    indefinitely — only `CONFIGURATION`→`BLOCKED` and `INVALID_INPUT`→
    `TERMINAL_FAILURE` are terminal (`work_execution.py:810-817`), and
    `judge_policy_denied` is neither. So a deterministically-denied conflict would
    **retry forever, consuming unbounded provider/judge calls** (worse than a clean
    terminal stuck state), never settling. A **`None`** evidence
    time makes `_deterministic_precedence` return `False` (`:363`), so
    `open_conflict` is reachable, while `_candidate_precedes` (`:349`) also returns
    `False`, so `revise`/`supersede` stay blocked exactly as intended.
  Tier is deliberately **not** `corroborated`, so a lone agent proposal can never
  `revise`/`supersede` an existing memory on its single-source authority.
  Rejected: fabricating `corroborated` — would let one agent assertion overwrite
  curated memory. Rejected: `source.created_at` as evidence time — it silently
  disables `open_conflict` (above).
  **A supported proposal that "updates" an existing fact is not stranded
  (finding: no terminal verdict for revision).** The `revise_memory`/
  `supersede_memory` outcomes requiring `corroborated` is the intended
  single-source invariant — **identical to a single-observation distillation
  candidate**, which is also tier `supported` (`_claim_evidence` `:169-170`) and
  equally blocked from revise/supersede; this slice introduces no new judge-policy
  behavior for it. When a supported proposal genuinely contradicts/updates a
  **same-visibility** target, the reachable terminal verdict is `open_conflict`
  (kept reachable by the `None` evidence time), which routes to human review where
  an authorized reviewer performs the actual revise/supersede — so the deliberate
  fact update settles via conflict → review, not an infinite `judge_policy_denied`
  loop. (The cross-visibility TEAM-vs-PROJECT variant is handled separately by the
  cross-visibility guard + `:471` relaxation, Curation-path §3.)
- **In the transition fence, an agent proposal's canonical content hash is
  *recomputed* from the candidate's own title/body/kind/team, not read back from
  the stored `candidate.content_hash`.** Those candidate fields are mutable
  (`models.py:639-707`), so anchoring on the stored `candidate.content_hash` (as
  an import does) and then comparing it to itself is a tautology that cannot
  detect a post-creation body edit (finding: content-hash recompute). The fence
  instead recomputes `agent_proposal_candidate_content_hash(candidate.title,
  candidate.body, candidate.kind, candidate.team_id)` — passing `candidate.kind`
  **verbatim** (it was already clamped once at creation), because the hash function
  no longer clamps internally (see Content hash); re-clamping in the fence would let
  a `''`→`digest`/unknown mutation slip through since `clamp_memory_kind` maps both
  to `''` (finding: fence kind-clamp bypass). It is the same namespaced function
  used at creation — mirroring the distillation branch that recomputes
  `session_candidate_content_hash(...)` and is pinned by
  `transitions_tests.py:314-327`. It needs no observation session lineage.
- **Idempotency is content-hash based** (the existing unique constraint on
  `(organization, project, content_hash)` — `models.py:673-676`), with
  `request_id` required and recorded in provenance + audit for traceability,
  mirroring the feedback/version request-id convention. Rejected: a separate
  idempotency table — the unique constraint already dedups identical proposals.
- **No per-key rate cap in v1.** Curation gates reject noise and the audit event
  records the actor; documented as accepted risk.
- **New view in its own file** (`memory/propose_view.py`) per the one-view-per-file
  rule; the existing `memory/views.py` multi-view file is left untouched.

## API and Schema Changes

### Endpoint

`POST /v1/memories/propose` (no trailing slash — mounted under
`path('v1/memories/', include('engram.memory.urls'))`,
`apps/backend/settings/urls.py:24`; add
`path('propose', MemoryProposeView.as_view(), name='memory-propose')` to
`apps/backend/engram/memory/urls.py`).

Auth: `TokenAuthentication` + bearer, resolved by `resolve_request_scope(...,
required_capability='memories:propose', ...)` (same view shape as
`MemoryFeedbackView`, `memory/views.py:51-83`). The endpoint is always on once
deployed — there is no write feature flag (see Deployment).

### Request body (`MemoryProposeSerializer`)

| field           | type    | required | default | notes |
|-----------------|---------|----------|---------|-------|
| `title`         | string  | yes      | —       | `CharField(max_length=255, allow_blank=False, trim_whitespace=True)` — matches `MemoryCandidate.title` max_length 255 (`models.py:657`) |
| `body`          | string  | yes      | —       | `CharField(max_length=MEMORY_PROPOSE_BODY_MAX_LENGTH, allow_blank=False, trim_whitespace=True)` — non-blank is critical (Evidence 1); **bounded** so an authorized key cannot submit an arbitrarily large body into embedding + judge calls with no rate limit (finding: body cap) |
| `kind`          | string  | no       | `''`    | `CharField(required=False, allow_blank=True, max_length=40)` — matches `MemoryCandidate.kind` (`models.py:668`); clamped server-side via `clamp_memory_kind` (digest/unknown → `''`) |
| `request_id`    | string  | yes      | —       | `CharField(allow_blank=False, max_length=255)`; audit + retry correlation only — **not** the idempotency key (idempotency is content-hash based, see below) |
| `project_id`    | uuid    | no       | —       | routing; passed to `resolve_project_for_scope` |
| `repository_url`| string  | no       | `''`    | `CharField(required=False, allow_blank=True, default='', max_length=1024)`; routing fallback when `project_id` absent |
| `team_id`       | uuid    | no       | —       | optional team scope; must be linked to the resolved project (see Data Flow) |
| `correlation_id`| string  | no       | `''`    | `CharField(required=False, allow_blank=True, default='', max_length=255)`; stored in provenance + audit |

Field length caps mirror the existing memory serializers
(`serializers.py:7-9`: `MEMORY_FEEDBACK_METADATA_MAX_LENGTH=255`,
`MEMORY_REPOSITORY_URL_MAX_LENGTH=1024`); `body` reuses the existing write-path
body cap `MEMORY_PROPOSE_BODY_MAX_LENGTH = 16000` (identical to
`MEMORY_VERSION_BODY_MAX_LENGTH`, `serializers.py:48`, and hook ingestion), and
the `AuditEvent.request_id`/
`correlation_id` columns (255, `models.py:1068-1069`). Without the 255 cap on
`request_id`, a bearer request whose scope resolution writes an
`AccessScopeResolved` audit row carrying `request_id`
(`resolve_project_for_scope` → `_authorize_resolved_project`) would raise a
`DataError` **500** on an oversized value instead of a documented **400**
(finding 11).

`kind` is deliberately **not** a strict `ChoiceField`: the server clamps, so an
unknown/`digest` value degrades to `''` rather than 400.

### Response — `202 Accepted`

```json
{
  "candidate_id": "b1c9...uuid",
  "status": "proposed",
  "decision_work_queued": true,
  "request_id": "mcp-8f3e...uuid"
}
```

- `status` echoes the candidate's current status. On idempotent reuse of a
  candidate already settled by curation it may be `"promoted"` or `"rejected"`.
- `decision_work_queued` is `true` when this request created and queued the
  `CANDIDATE_DECISION` work, `false` when the work already existed (idempotent
  reuse).

### Model / migration changes (`apps/backend/engram/core/models.py`)

1. `MemoryCandidateSourceKind` (`:100-102`): add
   `AGENT_PROPOSAL = 'agent_proposal', 'Agent Proposal'`.
2. `MemoryCandidateSource.observation` (`:2244`): add `null=True, blank=True`
   (keep `on_delete=models.PROTECT`). Backward compatible — existing rows all
   have observations.
3. Replace `core_candidate_source_shape_ck` (`:2288-2304`) with the three-way
   expression (exact new check). **Because `observation` becomes nullable
   (change 2), the distillation and import branches must now assert
   `observation__isnull=False` explicitly — the non-null FK used to supply that
   invariant for free; without re-asserting it here a malformed legacy-kind row
   with a null observation would pass the check and later crash every consumer
   that dereferences `source.observation` (finding 3).**

   ```python
   models.CheckConstraint(
       condition=(
           models.Q(
               source_kind=MemoryCandidateSourceKind.DISTILLATION,
               window__isnull=False,
               stage__isnull=False,
               import_source__isnull=True,
               observation__isnull=False,
           )
           | models.Q(
               source_kind=MemoryCandidateSourceKind.IMPORT,
               window__isnull=True,
               stage__isnull=True,
               import_source__isnull=False,
               observation__isnull=False,
           )
           | models.Q(
               source_kind=MemoryCandidateSourceKind.AGENT_PROPOSAL,
               window__isnull=True,
               stage__isnull=True,
               import_source__isnull=True,
               observation__isnull=True,
           )
       ),
       name='core_candidate_source_shape_ck',
   )
   ```

   Every existing distillation/import row already has a non-null observation, so
   the added `observation__isnull=False` clause is satisfied by all live rows.
   Only `agent_proposal` requires `observation IS NULL`.
4. Add a per-candidate uniqueness guard for agent sources (mirrors the
   distillation/import partial uniques `:2278-2287`):

   ```python
   models.UniqueConstraint(
       fields=['candidate'],
       condition=models.Q(source_kind=MemoryCandidateSourceKind.AGENT_PROPOSAL),
       name='core_candidate_source_agent_uniq',
   )
   ```
5. `_validate_source_shape` (`:2352-2360`): mirror the DB check in model
   validation. Add `observation_id is not None` to the distillation and import
   branch conditions (they must reject a null observation now that the FK is
   nullable), and add an `AGENT_PROPOSAL` branch that errors unless
   `window_id is None and stage_id is None and import_source_id is None and
   observation_id is None`. Concretely the distillation branch becomes
   `if self.window_id is None or self.stage_id is None or self.import_source_id
   is not None or self.observation_id is None:` and the import branch becomes
   `if self.window_id is not None or self.stage_id is not None or
   self.import_source_id is None or self.observation_id is None:`.

Migration: one migration under `core/migrations/` —
`AlterField` on `source_kind` to carry the third choice (**not** a no-op:
Django freezes a field's `choices` in migration state, and `0039` froze exactly
`choices=[('distillation', …), ('import', …)]` — `0039_import_provenance.py:19-28`
— so adding `AGENT_PROPOSAL` to the `TextChoices` changes the deconstructed
`choices` kwarg and the `makemigrations --check --dry-run` CI gate
`.github/workflows/backend.yml:64` will otherwise demand a follow-up migration),
`AlterField` on `observation` (nullable), `RemoveConstraint` + `AddConstraint`
for the shape check, `AddConstraint` for the agent unique. No data migration.
The `source_kind` `AlterField` must keep the existing
`db_default`/`default='distillation'` and `max_length=20` and only extend
`choices` to the three-way list, so it is a pure state/`choices` change with no
column rewrite.

**Reverse guard (finding 9).** The migration is forward-compatible but not
blindly reversible, so the reverse must be guarded (mirroring
`0039_import_provenance.py:7-10`, which refuses to reverse while import rows
exist):

- **Reverse guard:** a `migrations.RunPython(noop, _guard_reverse)` step (or the
  reverse of the shape-constraint op) must raise if any
  `MemoryCandidateSource.objects.filter(source_kind='agent_proposal').exists()`
  — restoring the two-kind check / non-null `observation` against live agent
  rows would fail mid-migration and leave the schema half-reverted. The forward
  `AlterField`/constraint swap is the reversible schema part; the guard protects
  the data. **Place the `RunPython(noop, _guard_reverse)` step LAST in
  `operations`** (exactly as `0039_import_provenance.py:102`), so on reverse it
  runs **first** — before any schema op — and aborts the reversal cleanly rather
  than after the constraint/observation changes have partially applied. The
  MigrationExecutor test (Test Plan §1) asserts this ordering.

**Deployment.** engram is a single-instance dogfood deployment with no
production fleet, so there is no rolling-deploy window to choreograph and no
backward-compatibility contract with old workers. Deployment is stop-the-world:
stop all services, run the migration, then start every service on the new code.
No window exists where an old worker (lacking the agent branches) sees a new
`agent_proposal` row, so no write feature flag or staged deploy ordering is
needed. In-progress `WorkflowWork` / tasks may be dropped or failed during a
deploy at zero cost. Rollback: a clean schema reverse is possible only before the
first agent proposal is created (the migration's reverse guard refuses to reverse
once any `agent_proposal` row exists); after that, recovery is fix-forward.

### Curation-path changes (make the dispatched worker survive AND promote an agent source)

An agent proposal is durable only if it survives every v1 stage. Each change
below is required; the ones marked **(promotion-enabling)** are what let a novel
proposal actually become an approved memory rather than an inevitable reject.
All agent branches key on the exact source-kind set being `{'agent_proposal'}`
(finding 12): a candidate whose sources mix agent with any other kind must
**raise** (`'candidate provenance has mixed source kinds'`), never silently drop
the non-agent rows.

**1. Evidence manifest — one shared helper, identical hash from both callers,
validated provenance (finding: agent-source validation).**

- New `validated_agent_candidate_source(candidate, *, sources)` in
  `import_provenance.py`, mirroring `validated_import_candidate_source`
  (`:119-135`). It does **not** trust the stored `anchors`/`anchors_hash`; it
  re-derives and checks them, so a malformed or corrupted agent row cannot earn a
  `supported` tier and promote:
  - the selected source set is **exactly one** row whose kind is `agent_proposal`
    (raise `ImportProvenanceError`/scope error otherwise);
  - it then calls the shared `_validated_agent_anchors(source)` helper (Curation-
    path §3) which enforces the full per-source invariant: **shape** (`window_id is
    None and stage_id is None and import_source_id is None and observation_id is
    None` — mirrors the DB check + `_validate_source_shape`); **`anchors` is a dict**
    with `schema == 'agent_proposal_source.v1'` and every required provenance key
    (`actor_type`, `actor_id`, `api_key_id`, `request_id`, `correlation_id`); and
    the **recomputed hash** `source.anchors_hash ==
    sha256(canonical_json_bytes(source.anchors)).hexdigest()` (the check the model
    does not enforce — `core_candidate_source_anchors_hex` only proves 64-hex, not
    that it hashes the anchors);
  - **candidate ownership** (this validator only — finding: source ownership):
    `source.candidate_id == candidate.id`, raising `ImportProvenanceError`
    otherwise. The import validator enforces this exact check
    (`import_provenance.py:67-68`, `'candidate source belongs to another
    candidate'`); the scope-tuple equality below is **not** sufficient because a
    different candidate in the *same* `(org, project, team)` can share the tuple,
    so a foreign candidate's agent source could otherwise satisfy the manifest
    validator and alter the work fingerprint/provenance. (The target-traversal
    tiering path in `_eligible_group_hash` legitimately has no owning candidate to
    compare against, which is why this check lives in `validated_agent_candidate_
    source`, not in the shared `_validated_agent_anchors` helper.)
  - **scope** (this validator only, not the shared helper — the target-traversal
    path has no candidate to compare against): `(source.organization_id,
    source.project_id, source.team_id) == (candidate.organization_id,
    candidate.project_id, candidate.team_id)`.
  Returns `(source, source.anchors)`. Because it delegates the shape/schema/keys/
  hash checks to `_validated_agent_anchors`, the candidate manifest path and the
  target-traversal tiering path (`_eligible_group_hash`) are guaranteed to reject
  the **same** malformed/corrupted anchors (schema, keys, and hash — not only
  shape), so a `{}`-anchors or missing-key agent source can never earn a
  `supported` tier from either path.
- New `agent_proposal_evidence_manifest(candidate, *, sources)`: call
  `validated_agent_candidate_source` first, then build `entry = {'anchors':
  source.anchors, 'anchors_hash': source.anchors_hash}` (same entry shape as the
  import branch `:185`), return
  `([entry], sha256(canonical_json_bytes([entry])).hexdigest())`.
- `candidate_evidence_manifest` (`:161-187`): add
  `if kinds == {'agent_proposal'}: return agent_proposal_evidence_manifest(...)`.
- `evidence_manifest` in `candidate_decision_work.py` (`:138-148`): the
  kind-dispatch must **preserve the existing plain-`Mapping` source path**
  (finding: Mapping dispatch). This function accepts `Iterable[
  MemoryCandidateSource | Mapping[str, object]]` (`:141`) and an existing test
  passes bare manifest dicts with no `source_kind` key
  (`candidate_decision_work_tests.py:220-243`). Compute kinds **only over model
  rows** — `kinds = {s.source_kind for s in selected if isinstance(s,
  MemoryCandidateSource)}` — and treat any `Mapping` entry as the existing
  distillation-shaped manifest entry (Mappings are only ever the 6-key
  distillation dict; agent/import rows are never passed as Mappings). Dispatch:
  if `kinds == {'agent_proposal'}` and there are no Mapping entries, delegate to
  `agent_proposal_evidence_manifest` (lazy import per `curation.py:654,694`); if
  agent is mixed with any other kind (model or Mapping), raise; otherwise fall
  through to the current per-entry `_source_value` path unchanged. This keeps
  `build_candidate_decision_input` / `_is_superseded_generation` and the existing
  dict-source test green — a regression the test plan (§2) now asserts.
- Negative tests: a corrupted `anchors_hash` (≠ sha256(anchors)), a wrong-shape
  agent source (non-null window/stage/import/observation), and a foreign-scope
  agent source each raise and never yield an evidence manifest / `supported` tier.

**2. Deterministic gates (`deterministic_gates.py`).**

- `_validate_sources` (`:257-285`): move the `observation` scope check
  (`:265-267`) **inside** the distillation/import branches (they already require
  a non-null observation), and add an `agent_proposal` branch that asserts
  `source.window is None and source.stage is None and source.import_source is
  None and source.observation_id is None` and raises
  `CandidateDecisionWorkScopeError('invalid agent proposal source relation')`
  otherwise. The trailing `else` still rejects genuinely unknown kinds.
- NOISE_LIFECYCLE_ONLY gate (`:520-525`): guard the observation deref — only
  consider sources that have an observation (`source.observation_id is not
  None`); an agent-only candidate has no lifecycle observation, so this gate is a
  no-op for it (it is filtered by the redaction/empty gates like any other).

**3. Judge evidence tiering (`curation_judge.py`) — (promotion-enabling).**

- `_eligible_group_hash` (`:196-215`): add, before the distillation logic, an
  `AGENT_PROPOSAL` branch that **validates the source inline** — it must **not**
  trust the stored `anchors_hash`. It calls the shared
  `_validated_agent_anchors(source)` helper (below), which asserts the **full**
  invariant — agent shape (`window_id`/`stage_id`/`import_source_id`/
  `observation_id` all `None`), `anchors` is a `dict` with
  `schema == 'agent_proposal_source.v1'` and every required provenance key
  (`actor_type`, `actor_id`, `api_key_id`, `request_id`, `correlation_id`), **and**
  a recomputed `sha256(canonical_json_bytes(source.anchors)).hexdigest() ==
  source.anchors_hash` — raising `CurationJudgeError('transition_dependency_
  unavailable')` on any mismatch (exactly as the distillation branch raises on a
  bad manifest/digest at `:208-213`), then `return source.anchors_hash`. One valid
  agent source → one group hash → `_claim_evidence` tier `supported`. **The
  validation must live here, not only in the evidence-manifest step (§1), because
  `_eligible_group_hash` is also called by `_traverse_target` (`:257-266`) on the
  historical `MemoryVersionSource.candidate_source` of a shortlist *target* — a
  path that never runs `validated_agent_candidate_source`.** Without inline
  validation a corrupted historical agent source would earn a group hash and
  inflate a target's tier (finding: target-traversal validation).
  **The shared helper must enforce the anchors *schema* and *required provenance
  keys*, not merely shape and hash consistency** — otherwise a malformed anchor
  such as `{}` paired with its own `sha256({})` would pass a shape+hash-only check,
  earn a group hash, and inflate a historical target to `supported` (finding:
  target-traversal validation). `_validated_agent_anchors` (shape + schema +
  required-keys + recompute) is the **single** validator; `_eligible_group_hash`
  (target and candidate tiering) and `validated_agent_candidate_source` (§1, which
  additionally checks scope and returns `(source, anchors)`) both call it, so the
  candidate and target paths enforce the **identical** invariant.
  (Distillation still returns `None` when it is lifecycle-only or fails its
  manifest checks, unchanged.) The negative tests in §1 plus a target-traversal
  negative (§3) pin this — including a `{}`-anchors / missing-schema / missing-key
  target source, not only a corrupted-hash one.
- `_source_evidence_time` (`:181-184`): guard the observation deref — `if
  source.observation_id is None: return None` (an agent source has no orderable
  observation lineage; a `None` evidence time is **required** so `open_conflict`
  stays reachable and `revise`/`supersede` stay blocked — see the Design tier
  bullet and finding: agent conflict precedence), else the existing
  observation-time logic. Returning `source.created_at` here would silently
  disable agent conflicts.
- **Cross-visibility target guard for the mutation outcomes (finding: cross-
  visibility target).** The shortlist for a **TEAM** candidate intentionally
  includes **PROJECT-global** targets (`curation_shortlist._scope_q:73-77` returns
  `PROJECT | TEAM(team)`), but the transition state machine requires **exact team
  equality** on any fenced target memory (`_scope_matches:281-285`,
  `_locked_memory_map:690`), and a PROJECT memory has `team_id=None ≠` the
  candidate's team. So if the judge selects a PROJECT target for a **mutation**
  outcome (`merge_evidence`, `open_conflict`, `revise_memory`, `supersede_memory`
  — the outcomes that carry a `MemoryFence` and lock/mutate the target), the
  transition raises a non-retryable `'scope'` error and the proposal **never
  settles** — a deterministic stuck-work path (and, semantically, a team candidate
  must **not** be allowed to mutate project-global memory anyway — that is the same
  cross-team escalation as finding: team-bound project write). Guard it in
  `_apply_evidence_policy` (`curation_judge.py:369-434`): for the four mutation
  outcomes, additionally require the selected shortlist `entry`'s
  `(visibility_scope, team_id)` to **equal the candidate's effective
  `(visibility_scope, team_id)`** (the entry carries both —
  `CurationShortlistEntry.visibility_scope`/`team_id`, `curation_shortlist.py:42-43`;
  the candidate's effective pair is `data.evidence`/shortlist scope, already TEAM
  for a team proposal per finding 3). The guard must **raise a non-retryable error**
  for those four outcomes against a cross-visibility target — **not** the default
  retryable `judge_policy_denied` (finding: cross-visibility no-terminal-verdict).
  `judge_policy_denied` is caught at `curation.py:1290` as an `_operational`
  (retryable) failure that the reconciler re-queues **forever** with no finite
  attempt budget (round-10 finding 9); a deterministic cross-visibility denial can
  never succeed on retry, so it would loop, burning unbounded judge/provider calls —
  strictly **worse** than the pre-guard non-retryable `'scope'` transition crash
  (which at least terminated).

  **Concrete `INVALID_INPUT` classification mechanism (finding: cross-visibility
  no-terminal-verdict — round 12).** Naming the guard location alone does **not**
  produce a terminal failure: `_apply_evidence_policy` can only raise
  `CurationJudgeError`, and every existing policy denial there uses the code
  `judge_policy_denied` (`curation_judge.py:395,406,434`), which
  `work_failures._CURATION_TRANSITION_CODE_MAP:52` maps to `PROVIDER_TRANSIENT`
  (retryable) — and an ad-hoc *new* code with no map entry falls through
  `_classify_transition` to `UNEXPECTED` (`work_failures.py:116-121`), which is
  **also** retryable. Neither yields `INVALID_INPUT`. So the guard must raise a
  **new, distinct** code **and** that code must be **explicitly mapped** to
  `INVALID_INPUT`:
  - The guard raises `CurationJudgeError('judge_cross_visibility_denied')` for the
    four mutation outcomes against a cross-visibility target — a code **distinct
    from** `judge_policy_denied` (which must keep its `PROVIDER_TRANSIENT` meaning
    for genuinely transient judge-output denials).
  - Add `'judge_cross_visibility_denied': (INVALID_INPUT, 'judge_cross_visibility_denied')`
    to `_CURATION_TRANSITION_CODE_MAP` in `work_failures.py:46-62`.
  Propagation: the guard's `CurationJudgeError` is caught at `curation.py:1290` and
  re-raised via `_operational(getattr(error, 'code', ...))`, which **preserves the
  code** on the wrapping `MemoryTransitionError`; no new `except` branch is required.
  The `retryable=True` flag `_operational` sets (`curation.py:1062-1063`) is **inert
  for the retry decision** — `translate_failure`/`_classify_transition`
  (`work_failures.py:116-134`) route solely on `error.code` → `failure_class`, and
  `_apply_failure_work` (`work_execution.py:810-817`) branches on `failure_class`,
  never on `.retryable`. With the code mapped to `INVALID_INPUT`, the candidate
  decision work lands on `work_execution.py:814` → `TERMINAL_FAILURE`, so a genuine
  TEAM-vs-PROJECT contradiction/merge terminates cleanly for human attention rather
  than looping — matching and improving on the pre-existing TEAM-distillation
  terminal behavior (now at the policy level, no transition exception).

  **The two reachable terminal verdicts against a PROJECT target
  (finding: cross-visibility no-terminal-verdict).** The earlier draft claimed
  "`publish_new` and `reject_candidate`/`redundant` remain valid" — that is only
  half true and the `publish_new` half was **wrong** for a genuine contradiction:
  - **Duplicate** (target classified `redundant`): the judge emits
    `reject_candidate`/`redundant` **directly** — it is not one of the four mutation
    outcomes, is not guarded, and `_apply_evidence_policy` (`:416-417`) permits it
    for any supported target. Clean settle (the reject path never locks/mutates the
    target — Curation-path §… / finding: redundant-reject). ✓
  - **Genuine contradiction** (target classified `mutually_incompatible`): the honest
    relation maps **only** to `open_conflict`, which the guard now terminally
    refuses; and `publish_new` is **blocked** by the targetless-outcome guard
    (`parse_curation_judge_verdict:471-474`, which rejects `publish_new` when **any**
    comparison relation is an identity relation such as `mutually_incompatible`). To
    give this case a **clean team-local terminal settlement** instead of a terminal
    *failure*, **relax that `:471` guard to ignore identity-relation comparisons
    against cross-visibility targets** — a TEAM candidate may `publish_new` its own
    **TEAM-visible** memory even though it identity-relates a PROJECT-global target it
    has no authority to mutate. The team's contradicting knowledge is preserved as
    team-local (coexisting with the project-global memory; humans reconcile via the
    normal review surface), which is the correct least-authority outcome.

    **The `:471` relaxation only opens the lane — it does not steer the provider
    into it (finding: cross-visibility no-terminal-verdict, round 13).** Relaxing
    the *parser* lets a `publish_new`-with-honest-`mutually_incompatible`-comparison
    verdict pass validation, but nothing tells the *model* to emit that verdict. The
    provider prompt today carries only JSON-shape rules
    (`_CURATION_DECISION_SCHEMA_INSTRUCTIONS`, `services.py:1209-1226`) and the
    serialized scope/comparisons (`build_curation_judge_prompt`,
    `curation_judge.py:512-559`) — **no decision rule**. Handed a genuinely
    contradictory PROJECT target, the natural verdict a competent judge emits is
    `open_conflict` (the honest relation `mutually_incompatible` maps there and
    nowhere else), which the mutation guard terminally refuses. So `publish_new` is
    **not** the "reachable" verdict on the provider path unless the prompt is
    extended with a **cross-visibility decision rule**. Therefore this slice **must**
    add to `_CURATION_DECISION_SCHEMA_INSTRUCTIONS` an explicit least-authority
    instruction with **same-visibility precedence** and a **fixed top-level verdict
    tuple**, both of which the earlier one-line draft omitted (finding:
    cross-visibility incomplete/unsatisfiable contract — round 14):

    - **Same-visibility precedence (mutable targets win).** A TEAM shortlist mixes
      PROJECT and same-TEAM entries (`_scope_q`, `curation_shortlist.py:69-78`), and
      a comparison for **every** entry is mandatory (`_validate_comparisons:330` — the
      comparison version-id tuple must equal the shortlist tuple). The cross-visibility
      fallback may **only** be chosen when **no same-visibility (same-`team_id`) target
      carries an identity relation**. If *any* same-visibility target relates as
      `equivalent`/`candidate_revises`/`candidate_supersedes`/`redundant`/
      `mutually_incompatible`, the judge **must** act on that mutable target with its
      normal, permitted outcome (`merge_evidence`/`revise_memory`/`supersede_memory`/
      `reject_candidate`/`open_conflict`) — a targeted outcome, so
      `target_memory_version_id` is non-null and the targetless `:471` guard never
      fires; any cross-visibility PROJECT comparison rides along in the `comparisons`
      array unguarded. This closes the reviewer's counterexample (closest PROJECT
      target `mutually_incompatible` **and** a same-TEAM `equivalent` target): the
      correct settle is `merge_evidence` against the same-TEAM target, **not**
      targetless `publish_new`, so the same-visibility identity comparison the `:471`
      relaxation deliberately does **not** ignore is never emitted under a targetless
      outcome in the first place.

    - **The cross-visibility fallback and its exact top-level tuple.** Only when every
      identity-related target in the shortlist is **cross-visibility** (no mutable
      same-visibility identity target exists): *do not choose `merge_evidence`/
      `revise_memory`/`supersede_memory`/`open_conflict` against a cross-visibility
      target; instead emit `outcome=publish_new`, `relation=compatible_distinct`,
      `reason_code=distinct_claim`, `target_memory_version_id=null`, still reporting
      the honest per-target relation (including `mutually_incompatible`) in the
      `comparisons` array.* The top-level `relation` **must** be `compatible_distinct`
      (the least-authority framing — the TEAM-local memory coexists with the
      PROJECT-global one): `_ALLOWED_COMBINATIONS` (`curation_judge.py:144-153`) admits
      `publish_new` **only** with `unrelated`/`compatible_distinct`, so a top-level
      `relation=mutually_incompatible` would fail `(outcome, relation) ∉
      _ALLOWED_COMBINATIONS` → retryable `judge_invalid_output`
      (`PROVIDER_TRANSIENT`, `work_failures.py:50`) and burn the attempt budget. The
      honest `mutually_incompatible` lives **only** in the per-target `comparisons`
      entry, which the `:471` relaxation permits by ignoring cross-visibility identity
      comparisons; the top-level summary stays `compatible_distinct`. This makes
      `publish_new` (TEAM-local) the verdict a compliant provider actually produces → clean settle. The non-retryable
    mutation guard above remains the safety net for a **non-compliant** provider that
    ignores the rule and still emits a mutation outcome against a cross-visibility
    target (`INVALID_INPUT`→`TERMINAL_FAILURE`, `work_execution.py:814-817`: a real
    bounded terminal, no loop). The *other* non-compliant shape — a provider that
    emits `publish_new` with a wrong top-level `relation` (e.g. `mutually_incompatible`)
    — is **not** terminal and there is **no finite attempt budget** for it: it fails
    the `_ALLOWED_COMBINATIONS` check as `judge_invalid_output`
    (`curation_judge.py:371-372`) → `PROVIDER_TRANSIENT` (`work_failures.py:50`), which
    `work_execution.py:818-824` moves to `RETRY_WAIT` on a backoff-**capped delay**
    (≤1800s, `work_failures.py:163-170` caps the *delay*, not the *count*) that the
    reconciler re-queues indefinitely (round-10 finding 9: there is no attempt budget
    anywhere on the candidate-decision path). So this is a slow, bounded-**latency**
    retry — not a tight/infinite-CPU loop, and not a bounded-**count** one. This design
    neither introduces the failure mode nor relies on it being terminal: it is exactly
    the pre-existing behavior of **every** malformed judge output (`curation_judge.py`
    raises `judge_invalid_output` for each schema/tuple/reference violation), and on an
    invalid tuple **no mutation occurs** — the verdict is rejected before any transition
    runs, so the candidate stays `PROPOSED` with consistent state and no lost work
    (idempotency and lost-work reconciliation, the in-scope steady-state guarantees, are
    untouched). A transiently-malformed provider self-heals on a later attempt; a
    **permanently** non-compliant provider is a pre-existing systemic condition
    identical to a provider that never returns parseable judge output for *any* call,
    and is out of scope for this design to solve differently from the rest of the judge
    path (its only cost is repeated provider calls, not corruption or a stuck-forever
    unsettled-yet-mutated state). Neither non-compliant shape is the *expected* path.
    This strictly improves the pre-existing TEAM-distillation path, which today only
    crashes.
  The judge tests (§3) add: a TEAM agent candidate offered a PROJECT target (a) is
  denied `merge_evidence`/`open_conflict`/`revise_memory`/`supersede_memory` with a
  **non-retryable** (`INVALID_INPUT`-class) failure — no infinite retry, no
  transition crash; (b) is permitted `reject_candidate`/`redundant` against it
  (duplicate); (c) for a shortlist whose **only** identity-related target is a
  `mutually_incompatible` cross-visibility PROJECT target, settles via `publish_new`
  with top-level `relation=compatible_distinct` (asserting the exact tuple
  `(publish_new, compatible_distinct)` — a top-level `mutually_incompatible` is
  rejected as `judge_invalid_output`) as a **TEAM-visible** memory carrying the
  honest `mutually_incompatible` only in its `comparisons` entry (the `:471`
  cross-visibility relaxation), never `judge_policy_denied`-looping; (c2)
  **same-visibility precedence**: a shortlist mixing a `mutually_incompatible`
  PROJECT target and an `equivalent` same-TEAM target settles via `merge_evidence`
  against the same-TEAM target (targeted, non-null `target_memory_version_id`), never
  targetless `publish_new`, so the same-visibility identity comparison never trips
  `:471`; and (d) a **provider-path** test
  (real-`JudgeCurationCandidate.execute` against a scripted/fake provider whose
  completion is fed through the actual `build_curation_judge_prompt` +
  `parse_curation_judge_verdict` pipeline, not a direct parser call) asserts the
  cross-visibility decision rule is present in the emitted prompt and that a
  rule-compliant `publish_new` completion settles TEAM-local while a rule-ignoring
  `open_conflict` completion lands on the non-retryable `judge_cross_visibility_denied`
  terminal — pinning both the instruction text and the end-to-end behavior so a
  parser-only test can never mask a broken provider path.

**4. Transition state machine (`transitions.py`) — (promotion-enabling).**

- `_source_rows` (`:396-440`): skip null observation_ids when hydrating
  (`observation_ids = {s.observation_id for s in sources if s.observation_id is
  not None}`), guard the `source.observation = observations[...]` assignment on
  `source.observation_id is not None`, and add an `agent_proposal` `sort_key`
  branch keyed on `('agent_proposal', source.anchors_hash)` (no observation /
  window / stage access).
- `_candidate_fence` (`:288-323`): accept an agent proposal, but **first add a
  shared mixed-kind precheck** that all six transitions inherit (finding: mixed
  transition provenance). Today the fence's only kind check is the single-kind
  guard at `:299` (`any(source.source_kind != allowed_source_kind ...)` → generic
  non-retryable `'provenance'` `'... source kind is not allowed'`), and the exact
  non-retryable `'candidate provenance has mixed source kinds'` message the spec
  (Design/§4) and Test Plan §3 require exists **only** in
  `_promotion_uses_import_source` (`:1328`) — i.e. only promotion. Merely widening
  `allowed_source_kind` to a set (or deriving it) does **not** give the other five
  transitions that error: a mixed `{agent_proposal, distillation}` set would either
  slip past a permissive set-membership check and then fail inside
  `candidate_evidence_manifest` (wrapped at `:303-304` as **retryable**
  `'stale_decision'`), or trip the generic `'... source kind is not allowed'` — in
  both cases the wrong code/retryability and the wrong message, failing the
  transition-level mixed-source tests. So, at the top of `_candidate_fence` (after
  the candidate-id check), compute `kinds = {s.source_kind for s in sources}` and,
  if `len(kinds) > 1`, raise
  `MemoryTransitionError('provenance', 'candidate provenance has mixed source
  kinds')` (**non-retryable**, matching promotion's `:1329`) — a single shared
  precheck reached by promote/merge/revise/supersede/conflict/reject alike. Then
  fence against a **per-transition allowed-kind set**, **not** a single kind
  derived from the candidate (finding: import-only promotion boundary). Deriving
  the allowed kind from the candidate's own source set would silently **remove the
  existing import-only-promotion boundary**: today only promotion accepts import,
  and `test_import_only_candidate_is_rejected_by_non_promotion_candidate_transition`
  (`transitions_tests.py:364-453`) requires an import candidate to raise
  `'provenance'` and stay `PROPOSED` on merge/revise/supersede/open/resolve. A
  candidate-derived kind would make an import candidate fence against `IMPORT` on
  those paths and pass — regressing that test and letting imports be merged/
  conflicted. Instead, each transition declares which kinds it accepts and
  `_candidate_fence` raises the generic non-retryable `'provenance'`
  `'... source kind is not allowed'` (`:300`) for any source outside that set:
  - **promotion** accepts `{DISTILLATION, IMPORT, AGENT_PROPOSAL}`;
  - **merge/revise/supersede/open_conflict/reject and conflict-resolution** accept
    `{DISTILLATION, AGENT_PROPOSAL}` — **import stays excluded** (regression test
    green), and agent is newly admitted.
  The `kinds` set is uniform after the mixed-kind precheck, so "in the allowed set"
  reduces to a single-kind membership test; the hash branch below keys on that one
  kind. And on the canonical-content-hash step,
  branch on that kind and for `agent_proposal` **recompute** the agent hash from the
  candidate's own fields — `canonical_content_hash =
  agent_proposal_candidate_content_hash(candidate.title, candidate.body,
  candidate.kind, candidate.team_id)`, passing `candidate.kind` **verbatim** (not
  re-clamped) so a `''`→`digest`/unknown mutation is detected rather than masked by
  `clamp_memory_kind` (finding: fence kind-clamp bypass) — so the
  `candidate.content_hash != canonical_content_hash` check at `:319` actually
  detects a mutated title/body/kind (using `candidate.content_hash` directly would
  be tautological — finding: content-hash recompute). This mirrors the
  distillation branch's recompute at `:317`; it does **not** reconstruct an
  observation session. The mixed-kind precheck living in the shared fence (not
  duplicated per transition) is what makes the transition-level mixed-source test
  (Test Plan §3, lines "a **mixed** agent + distillation source set passed to any
  transition **raises**") pass uniformly across all six callers.
- transition call sites (`:1422-1423,1607,1927,2115,2408,2570`): replace the
  hardcoded `allowed_source_kind=DISTILLATION` (or `IMPORT`) with the
  **per-transition allowed-kind set** above (`allowed_source_kinds`). Promotion
  passes `{DISTILLATION, IMPORT, AGENT_PROPOSAL}` (superseding the current
  `IMPORT if import_only else DISTILLATION` at `:1422-1423`); the five
  non-promotion call sites (`:1607,1927,2115,2408,2570`) pass
  `{DISTILLATION, AGENT_PROPOSAL}`, which **admits agent proposals while keeping
  import excluded** — preserving the import-only-promotion invariant
  (`transitions_tests.py:364`) and newly admitting agent sources rejected today at
  `:299`. Agent proposals reach revise/supersede only if the judge emits them,
  which the tier-`supported` policy forbids for a lone agent source, so in practice
  agent candidates exercise publish_new / merge / conflict / reject.
- `_promotion_uses_import_source` (`:1311-1332`): the non-import branch must
  accept `AGENT_PROPOSAL` alongside `DISTILLATION` (raise only on a genuinely
  foreign/mixed kind). Agent proposals return `import_only=False` (they go
  through candidate-decision work exactly like distillation), so the existing
  `_require_claimed_candidate_work` path applies unchanged.

**5. Promotion metadata / version-source rows.** `_promotion_memory_metadata`
(`:1335-1353`) and the `MemoryVersion`/`MemoryVersionSource` creation
(`:1467-1485`) already guard `candidate.source_observation` with
`if candidate.source_observation else []` and set
`source_content_hash=source.anchors_hash`; an agent candidate has
`source_observation=None` and a valid `anchors_hash`, so these paths need no
change. This is asserted by the end-to-end settlement test (Test Plan §8).

**Agent candidates must keep `source_observation IS NULL` at settlement (finding:
agent provenance mutation).** `MemoryCandidate.source_observation` is a mutable FK
whose model-level scope validation checks only organization/project, not team
(`core/models.py:171-…`), and it is **not** part of the content-hash fence (which
hashes only title/body/kind/team). Promotion copies `candidate.source_observation`
into `MemoryVersion.source_observation` and the promoted memory's provenance
(`transitions.py:1073,1471`). No in-slice path mutates it, but as defense-in-depth
the agent branch of `_candidate_fence` (and `validated_agent_candidate_source`)
**asserts `candidate.source_observation_id is None`**, raising a non-retryable
`'provenance'` error otherwise — so an agent candidate can never acquire a
cross-team observation's provenance. This is cheap and mirrors the import
validator's ownership rigor.

The promoted memory's other semantic fields: `kind` is copied from the
already-clamped, fence-verified `candidate.kind` (any post-creation change trips
`stale_decision` via the verbatim recompute above). `confidence` is created `NULL`
on the agent path and set only by the curation decision — identical to
distillation. **`visibility_scope` is re-derived and re-checked by
`_revalidated_effective_scope` (`:1002-1025`) only on the promote (`:1432`) and
supersede (`:2130`) paths** — **not** on merge or conflict-resolution
(finding: conflict-resolution visibility). `_validate_conflict_resolution_rows`
(`:2560-2607`) validates fence/status/provenance but does **not** call
`_revalidated_effective_scope`, and `_create_candidate_memory` (`:1034-1082`)
publishes with the **stored** `candidate.visibility_scope` when no `scope_override`
is passed (`:1045`). The earlier claim that conflict resolution re-derives
visibility was inaccurate. This is nonetheless safe: (a) **no in-slice endpoint
mutates a proposed candidate's `visibility_scope`** (there is no candidate-update
view — grep confirms only reads/comparisons), and (b) distillation candidates flow
through the **identical** merge / conflict-resolution paths with the identical
stored-visibility behavior today — agent proposals are strictly parity, not a new
exposure. Combined with the source_observation guard above, the durable
provenance an agent proposal can carry is fully constrained.

**6. Reconciler coverage for stuck agent proposals (lost-work safety net).**
In-transaction (outbox) dispatch commits the `CeleryOutbox` row atomically with
the work, closing the send-**before**-commit race; it does **not** recover a
stalled/failed outbox relay or a lost broker delivery. In either case the
`MemoryCandidate` + `MemoryCandidateSource` + `WorkflowWork` + a **`QUEUED`
`WorkflowRun`** are committed but the celery message is never consumed, so the run
stays `QUEUED` and the candidate sits `PROPOSED` forever. The reconciler
(`candidate_work_reconciler.py`) today (a) filters **distillation-only**
(`_cp3_repair_candidates` `:159-164`, `_repair_candidate` `:267-276`) **and** (b)
in `_repair_candidate` **returns `False` whenever any `QUEUED`/`RUNNING` run
already exists** (`:282-286`) — it only ever *re-dispatches* work that has **no**
active run. A lost broker delivery leaves a `QUEUED` run, so `_repair_candidate`
short-circuits and **never re-signals** it (the earlier claim that "the existing
duplicate-run guard re-signals the committed work" was wrong — the guard is a bail,
not a re-signal). Atomic dispatch narrows the window but does not close it, so the
reconciler **must** both cover agent sources **and** gain the stale-`QUEUED`
re-signal the session reconciler already has:
- `_cp3_repair_candidates` (`:159-181`): broaden `durable_sources` so a candidate
  is eligible when it has a distillation source **or** exactly one
  `agent_proposal` source (an `Exists` on `source_kind=AGENT_PROPOSAL`), keeping
  the unresolved-conflict exclusion unchanged.
- `_repair_candidate` (`:255-284`): (a) resolve the candidate's source set and its
  decision work, then acquire locks **work-before-candidate** (§ (a′)). When the
  source set is `{'agent_proposal'}`, load that agent source (no `window`/`stage`
  `select_related`) and pass it to `ensure_candidate_decision_work_locked(candidate,
  sources=sources)` to resolve the work identity.
  - **(a′) Lock order: work (+runs) → candidate, then revalidate (finding:
    reconcile ↔ settlement deadlock, and reconcile ↔ claim race).** Two hazards, one
    lock discipline. First, `ensure_candidate_decision_work_locked` → `create_work` →
    `_create_non_digest_work` uses `get_or_create` **without** `select_for_update`
    (`workflow_work.py:870-…`), so it does **not** lock the existing `WorkflowWork`,
    and the run lookup at `:282` is unlocked; if the early-return is replaced with a
    re-signal (below) without locking, a worker can `claim_work` the stale `QUEUED`
    run (→ `RUNNING`/`LEASED`) **between** the reconciler's unlocked classification and
    `queue_work_attempt`, and `_eligible_queued_run` then finds no `QUEUED` row and
    **creates a second run** against already-claimed work. Second — and the reason the
    lock **order** matters — **every** settlement transition locks **work before
    candidate**: promotion (`transitions.py:1391`→`lock_work_fence`,
    then `:1397` candidate `select_for_update`), merge/revise
    (`transitions.py:1909`→`_lock_optional_work`, then `:1913` `_lock_candidate`),
    conflict settlement (`transitions.py:2378`→`_lock_optional_work`, then `:2382`),
    and model rejection (`curation.py:1354`). The current `_repair_candidate` inverts
    this: it takes the candidate `select_for_update` **first** (`:258`) and only later
    reaches the work lock (`queue_work_attempt`→`WorkflowWork.objects.
    select_for_update().get`, `work_dispatch.py:98`), establishing a candidate→work
    order. A reconciler holding the candidate lock and waiting on the work, concurrent
    with a worker mid-settlement holding the work-claim lock and waiting on the
    candidate, is an ABBA cycle Postgres resolves by aborting one side
    (`deadlock_detected`) — spurious failures/retries, and the "leave a live lease
    alone" classification cannot avoid it because the deadlock happens while
    **acquiring** the work lock, before any classification runs. So the reconciler
    **must adopt the settlement order**: do the initial candidate/source/work
    resolution as an **unlocked read** (candidate identity fields are never mutated
    post-creation — round-10 finding 8 — so this read is safe and revalidated below);
    then acquire, **in this order**, `select_for_update()` on the work
    (`WorkflowWork.objects.select_for_update().get(id=work.id)`) and its v1 runs
    (`WorkflowRun.objects.select_for_update().filter(work_id=work.id,
    execution_contract_version=1)`); then acquire `select_for_update()` on the
    candidate and **revalidate under lock** — still `status=PROPOSED`,
    `decision_work_contract_version=1`, no unresolved `MemoryConflict`, and the source
    set unchanged — returning `False` (no-op) on any mismatch, because a settlement or
    promotion may have run in the read→lock gap. This serializes against both
    `claim_work`'s `_lock_work` **and** every settlement transition on the identical
    work→candidate order, so no deadlock cycle exists; the candidate revalidation
    restores the correctness the up-front candidate lock previously provided.
  - (b) **replace the unconditional `QUEUED`/`RUNNING`
  early-return** with the session-reconciler rule
  (`session_work_reconciler._classify_queued` `:138-147`): a **`RUNNING`** run or a
  **fresh `QUEUED`** run (`dispatched_at is not None and as_of - dispatched_at <=
  RESIGNAL_WINDOW`) is left alone (no double dispatch), but a **stale `QUEUED`** run
  (`dispatched_at is None or as_of - dispatched_at > RESIGNAL_WINDOW`) is re-signaled
  by calling `queue_work_attempt(work_id=work.id, now=as_of,
  origin=WorkflowRunOrigin.RECONCILIATION)` — whose `_eligible_queued_run` path
  (`work_dispatch.py:103-113`) re-signals the **existing** `QUEUED` run (advancing
  its `dispatched_at`) rather than creating a second one, and is a no-op inside the
  `RESIGNAL_WINDOW` even if reached. The no-active-run case still re-dispatches via
  `_requeue_eligible` (`:280`) as before.
  - **(c) Recover an expired lease — worker died mid-decision (finding: expired
    lease abandoned).** A claim moves the run to `RUNNING` and the work to `LEASED`
    with `lease_expires_at` (`work_execution.py:340-365`). If the worker then dies
    or hangs, the lease expires but the current `_repair_candidate` never revives
    it: `_requeue_eligible` (`:244-252`) covers only `READY`/due-`RETRY_WAIT`
    (a `LEASED` work is neither), the run lookup bails on any `RUNNING` run, and
    `inspect_candidate_work._classify` (`:109-129`) does not flag lease expiry
    either — so an agent (or distillation) proposal whose worker crashed sits
    `PROPOSED` forever. This is steady-state lost work, **not** a deploy-window
    concern (the operator directive keeps lost-work reconciliation in scope), and
    the session reconciler already handles it (`LEASE_EXPIRED` →
    `reclaim_via_claim_work`, `session_work_reconciler.py:182-190,44`). Mirror it:
    when the (now locked) work is `LEASED` with `lease_expires_at < as_of` and its
    `RUNNING` run is stale, re-signal via `queue_work_attempt(...)` so a fresh
    consumer runs `claim_work`, whose `_handle_leased_state` (`work_execution.py:
    434-461`) fails the lost `RUNNING` run (`_fail_run_worker_lost`) and re-claims
    the work. A live (unexpired) lease is left untouched (`busy`).
  - **(d) Recover a CONFIGURATION-blocked work after config is fixed
    (finding: configuration-blocked abandoned).** A judge/model/rollout
    configuration failure classifies as `CONFIGURATION` and moves the work to
    `BLOCKED` with a `blocked_configuration_fingerprint` (`work_execution.py:810-813`),
    **not** `RETRY_WAIT`. The current `_repair_candidate` never revives it:
    `_requeue_eligible` (`:244-252`) matches only `READY`/due-`RETRY_WAIT`, and a
    `BLOCKED` work is neither, so once the operator **fixes the configuration while
    the system runs** the accepted proposal (agent **or** distillation) stays
    `PROPOSED` forever — nothing ever re-delivers the work, and `claim_work`'s
    `_short_circuit_state` (`work_execution.py:423-431`) only clears a changed
    fingerprint on a **delivery that never comes**. The session reconciler already
    handles this via `CONFIGURATION_CHANGED` → `clear_block_and_queue`
    (`session_work_reconciler._classify_blocked:131-135`, `_clear_block:354-367`,
    `:399-402`). Mirror it: when the (now locked) work is `BLOCKED` and
    `execution_configuration_fingerprint(work) != work.blocked_configuration_fingerprint`
    (config changed since the block), clear the block (reset `execution_state=READY`,
    `blocked_configuration_fingerprint=''`, `failure_streak=0` — exactly
    `_clear_block`) and re-signal via `queue_work_attempt(...)`. A `BLOCKED` work
    whose fingerprint is **unchanged** (config still broken) is left blocked (a
    no-op, not a loop). This keeps the candidate reconciler's recovery surface at
    parity with the session reconciler.
  A mixed agent+other source set is skipped
  (it cannot occur — the create path writes a single kind — and is not a repair
  target). This fix recovers agent **and** distillation candidates from a lost
  delivery, a dead-worker expired lease, **and** a config-fixed `BLOCKED` work.
This makes the reconciler the durable safety net for the never-dispatched, the
lost-delivery, the expired-lease, **and** the configuration-changed cases; the
in-transaction dispatch is the fast path. Covered by Test Plan §4 (reconciler
re-dispatch, stale-`QUEUED` re-signal, expired-lease reclaim, **and**
config-changed `BLOCKED` re-signal) and asserted not to double-dispatch when a
**fresh** run already exists, a **live** lease is held, or the block fingerprint is
**unchanged**.

### Provenance anchors

Provenance is **actor-specific** (finding 4). Session-authenticated users have no
API key: `resolve_session_scope` sets `api_key_id = uuid.UUID(int=0)`
(`auth_services.py:257-260`, the all-zero sentinel) and `actor_type='user'`.
Recording `str(scope.api_key_id)` unconditionally would stamp
`00000000-0000-0000-0000-000000000000` as a real API-key identity on a
user-authored proposal. `api_key_id` is therefore included **only** when
`actor_type == 'api_key'`, and is `None` for `actor_type='user'`:

```python
anchors = {
    'schema': 'agent_proposal_source.v1',
    'actor_type': scope.actor_type,           # 'api_key' or 'user'
    'actor_id': scope.actor_id,               # key id or user id
    'api_key_id': str(scope.api_key_id) if scope.actor_type == 'api_key' else None,
    'request_id': request_id,
    'correlation_id': correlation_id or '',
}
anchors_hash = sha256(canonical_json_bytes(anchors)).hexdigest()  # 64 hex, satisfies core_candidate_source_anchors_hex (:2305-2308)
```

Both authentication paths are covered by service tests (bearer → real
`api_key_id`; session user → `api_key_id is None`, `actor_type='user'`).

### Content hash

New `agent_proposal_candidate_content_hash(title, body, kind, team_id)`
(namespaced, so it can never collide with distillation
`sha256(observation.content_hash)` or import hashes). It hashes its `kind` argument
**verbatim** — it does **not** call `clamp_memory_kind` internally. Clamping happens
**exactly once**, in the service at creation (`clamped_kind = clamp_memory_kind(kind)`,
Data Flow step 2), and the clamped value is what is both stored on `candidate.kind`
and passed here; the fence later re-hashes `candidate.kind` **as stored**
(Curation-path §4). If the function clamped internally, the fence could not detect a
post-creation mutation of a blank kind to `digest`/an unknown value, because
`clamp_memory_kind` collapses both `digest` and any unknown string back to `''`
(`models.py:137-141`) — the mutated and original kinds would hash identically while
promotion copies the **raw** `candidate.kind` into the memory metadata
(`transitions.py:1351`), leaking an un-clamped `digest` with no `stale_decision`
(finding: fence kind-clamp bypass). Hashing verbatim over the already-clamped stored
value keeps creation and fence in agreement at creation time (`candidate.kind ==
clamped_kind`) and makes **any** later change to `candidate.kind` trip
`stale_decision`. **`kind` and `team_id` are part of the identity, and this only
stays consistent because a team proposal is created with `visibility_scope=TEAM`
(finding 3).** The curated claim identity is
`deterministic_gates._claim_bytes` (`:288-297`), which hashes title/body/kind and
the **effective** `visibility_scope`/`team_id` returned by `_scope_for`
(`:238-254`). For `visibility_scope=PROJECT`, `_scope_for` returns
`(PROJECT, None)` — team is dropped — so if a team proposal were stored as
PROJECT-visible, the content hash would carry the team but the curated claim would
not, and two same-title/body proposals from different teams would be distinct
candidates yet collapse to one claim in the judge (each merging/conflicting the
other). Storing a team proposal as `visibility_scope=TEAM` makes `_scope_for`
return `(TEAM, team_id)` (its source-team set equals `{candidate.team_id}`), so
the effective claim carries the team and matches the content hash end to end. The
candidate unique constraint is per `(organization, project, content_hash)`;
omitting `kind`/`team_id` from the hash would let a team-B (or different-kind)
caller collide with and receive team-A's candidate UUID/status (finding 6):

```python
sha256(json.dumps(
    ('agent_proposal_candidate', title, body, kind,
     str(team_id) if team_id is not None else None),
    sort_keys=True, separators=(',', ':')).encode()).hexdigest()
```

computed from the **redacted, stripped** title/body and the **already-clamped**
kind (clamped once in the service, passed here verbatim), so the reuse lookup
naturally scopes by content + kind + team. Because the identity
carries these fields, an identical `(title, body)` submitted under a different
kind or team is a distinct candidate, not a cross-scope reuse.

## Data Flow

Service `ProposeMemory().execute(ProposeMemoryInput(scope, project, team_id,
title, body, kind, request_id, correlation_id))` (the view resolves `project`
via `resolve_project_for_scope` before calling the service). Everywhere below,
`team_id` means the **effective** team derived in step 0 — never the raw request
field on its own:

0. **Effective team derivation — confine team-bound callers (finding: team-bound
   project write).** A team-bound bearer key retains its authorized team on the
   scope even when the request omits `team_id`: `ResolveApiKeyScope._team_ids`
   returns `(key.team_id,)` for a key with `key.team_id` set, regardless of the
   request (`access/services.py:351-355`, `access_scope_tests.py:203`). If the
   service took visibility straight from the request field
   (`TEAM iff team_id is not None`), a **team-confined** principal could omit
   `team_id` and mint a **PROJECT-visible** memory that every other team scope can
   read (`context/services.py:280`, and the shortlist/`_scope_q` TEAM branch reads
   PROJECT rows) — a cross-team injection the key has no authority to perform. The
   fix derives the effective team from the **scope**, not the bare request field.
   Add a **defaulted** `team_bound: bool = False` field to `EffectiveScope`.
   **It must be a *defaulted* field, not a required one like `project_bound`
   (finding: EffectiveScope constructors).** `EffectiveScope` is a frozen dataclass
   constructed at **five** sites — the bearer path (`access/services.py:206`), the
   rebuilt request-session scope (`request_scope.py:94`), **and three the earlier
   draft omitted**: `resolve_session_scope` (`auth_services.py:201`),
   `resolve_user_scope_for_organization` (`auth_services.py:257`), and the curator
   system scope (`curation.py:972`). A *required* `team_bound` (mirroring
   `project_bound`, which every site passes explicitly) would raise `TypeError` at
   those three unedited sites across session authentication and curation. Declaring
   it **defaulted `False`** (placed last, after `project_bound`) means only the
   bearer path sets it explicitly to `bool(key.team_id)`; the session
   (`request_scope.py:94`, `auth_services.py:201`/`:257`) and curator
   (`curation.py:972`) constructors inherit the safe `False` default with **no
   edit** — none of them is an explicitly team-confined bearer key. A dataclass
   test asserts a bearer key with `key.team_id` set yields `team_bound=True` and the
   other constructors yield `team_bound=False`.

   **Session path stays `team_bound=False` — read/write symmetry, not an escalation
   (finding: session team authority).** A session user's `memories:propose` and
   project access may derive solely from a `TeamMembership` role
   (`auth_services._user_capability_codes:270-294` aggregates org + project-grant +
   team roles; `_user_project_ids:297-321` grants the team's linked projects). One
   could argue such a team-derived user omitting `team_id` and minting a
   **PROJECT**-visible memory escalates cross-team. It does **not**: the session
   user is a **project participant** with **symmetric** project-global read and
   write scope. `context/services.py:280` already makes **every** PROJECT-global
   memory in that project readable by that same team-derived participant — their
   read authority came from Team A yet spans the whole project's global memory, so
   letting their write authority publish project-global knowledge (readable by
   Team B) is the write-side of the identical participation scope, not a new data
   exposure beyond what they already read. The system's **explicit** confinement
   mechanism is a team-**bound bearer key** (`key.team_id`, the round-10 finding-1
   fix), which is honored; a session participant carries no such explicit binding.
   Every session proposal is authenticated, audited with the actor
   (`MemoryProposed`), and routed through curation (dedup/judge/conflict), so a
   team-derived contribution to project-global knowledge is accountable, not a
   silent injection. Confining session **writes** by per-role grant provenance while
   leaving their project-global **reads** unconfined would be an inconsistent
   asymmetry, so the session path keeps `team_bound=False`. Then:
   - if `scope.team_bound` is `True`: the proposal is **always** TEAM-scoped to the
     bound team — set `effective_team_id = scope.team_ids[0]` (the request either
     omitted `team_id`, or supplied it and `resolve_request_scope` already forced
     it to equal the bound team, returning 403 `team_scope_denied` otherwise). The
     bound key can never create a PROJECT proposal.
   - else: `effective_team_id = request.team_id` (may be `None`). Visibility is
     TEAM iff `effective_team_id is not None`, PROJECT otherwise.
   A view test asserts a **team-bound bearer key omitting `team_id`** produces a
   `visibility_scope=TEAM` candidate scoped to the bound team, **not** a
   project-global one.


1. `title = redact_text(raw_title).strip()`, `body = redact_text(raw_body).strip()`
   (`memory.services.redact_text`, `services.py:702`).
   - If either is empty → `ProposeMemoryError('empty_content', ...)` → 422. This
     is a service-level guard: `redact_value` only substitutes `[REDACTED]` and
     never turns a non-blank string into empty, and the serializer already
     rejects blank/whitespace input with 400, so this branch is not reachable from
     a valid HTTP body — it is exercised by a service unit test, not the API test.
   - **Post-redaction length re-validation (finding 14).** `redact_text` can
     **grow** a string: when the input is a JSON-shaped string, `redact_value`
     parses and re-serializes it with `json.dumps(..., sort_keys=True)`
     (`redaction.py:62-70`), whose default `', '`/`': '` separators add whitespace,
     so a title within the serializer's 255-char limit can exceed
     `MemoryCandidate.title.max_length=255` after redaction — and
     `TimestampedModel.save()` runs `full_clean` (`models.py:25`), which would
     raise an **unhandled `ValidationError` → 500** rather than a documented 4xx.
     The service therefore re-checks the redacted `title` (`len(title) > 255`) and
     redacted `body` (`len(body) > MEMORY_PROPOSE_BODY_MAX_LENGTH`, finding: body
     cap) **after** redaction, and on overflow raises
     `ProposeMemoryError('content_too_long')` → 422 (never reaching `full_clean`).
     A regression test feeds a near-limit JSON title containing a secret and
     asserts a 422 `content_too_long`, not a 500.
2. `clamped_kind = clamp_memory_kind(kind)`.
3. **Team–project link validation (finding 7).** If `team_id is not None`,
   require a `ProjectTeam` row for `(project, team_id)` (the model is
   **`ProjectTeam`**, `models.py:303-310`, related_name `project_links`, unique
   `core_project_team_unique_pair` — there is no `ProjectTeamLink` class); this
   check runs **only** when the scope already authorized the team, so it is the
   422 `team_not_in_project` service path from the error table (a bearer with an
   explicit `project_id` is instead stopped earlier with 403 `team_scope_denied`,
   finding 5). Otherwise raise `ProposeMemoryError('team_not_in_project')` → 422.
   Session/agent scope authorizes project and team **independently**
   (`request_scope.py:91-100`) and
   team-admin users receive every org team (`auth_services.py:329-330`), while
   `MemoryCandidate.clean` only checks org membership (`models.py:691-697`);
   without this link check a caller could attach an unrelated same-org team to
   the project. Distillation/import candidates never hit this because their team
   comes from a project-scoped window/observation — the propose endpoint is the
   only path that takes `team_id` from the request, so it must enforce the link.
4. `content_hash = agent_proposal_candidate_content_hash(title, body,
   clamped_kind, team_id)`.
5. Idempotency lookup (unlocked) by `(organization, project, content_hash)`:
   - **Existing + status != proposed** (settled promoted/rejected) → return it,
     `decision_work_queued=False`, and still write a `MemoryProposeReused` audit
     event recording `request_id`/`correlation_id`/`candidate_id`/`status` so
     retry traceability is preserved (finding 5). Reusing settled content is
     intentional dedup: identical rejected content returns the rejection rather
     than re-running curation (documented, see Out of Scope).
   - **Existing + proposed** → `with transaction.atomic():` re-fetch the
     candidate `select_for_update()`, `work, created =
     ensure_candidate_decision_work_locked(candidate)`; if `created`, call
     `queue_work_attempt` **inline within the transaction** (step 7). Write a
     `MemoryProposeReused` audit event. Return `decision_work_queued=created`.
     (No new source row.)
   - **New** → `with transaction.atomic():`
     1. Create candidate + source + work inside a **nested savepoint** and catch
        the unique-constraint race (finding 4). Two concurrent proposals can both
        miss the unlocked lookup in step 5; the loser hits
        `core_memory_candidate_unique_content_hash_per_project` and must reload
        the winner, mirroring distillation (`distillation.py:350-383`):

        ```python
        try:
            with transaction.atomic():  # savepoint
                # team_id here is the EFFECTIVE team from step 0 (scope-confined
                # for team-bound keys, finding: team-bound project write); TEAM
                # visibility iff it is set, so the curated claim identity carries
                # the team (finding 3):
                visibility = VisibilityScope.TEAM if team_id is not None else VisibilityScope.PROJECT
                candidate = MemoryCandidate.objects.create(
                    organization=..., project=..., team_id=team_id,
                    source_observation=None, title=title, body=body,
                    status=PROPOSED, visibility_scope=visibility, evidence=[],
                    content_hash=content_hash, confidence=None, kind=clamped_kind)
                MemoryCandidateSource.objects.create(
                    organization=..., project=..., team_id=team_id,
                    candidate=candidate, source_kind=AGENT_PROPOSAL,
                    window=None, stage=None, import_source=None,
                    observation=None, anchors=anchors, anchors_hash=anchors_hash)
                candidate.decision_work_contract_version = 1
                candidate.save(update_fields=['decision_work_contract_version',
                                              'updated_at'])
        except IntegrityError:
            candidate = MemoryCandidate.objects.select_for_update().get(
                organization_id=..., project_id=..., content_hash=content_hash)
            # winner already exists → fall through to the "existing" branches
            # (proposed → ensure work; settled → return), no second source row.
        ```
     2. `work, created = ensure_candidate_decision_work_locked(candidate)`.
     3. Call `queue_work_attempt` **inline within the transaction** (step 7).
     4. `MemoryProposed` audit event (below).
     5. Return `decision_work_queued=created`.
6. `decision_work_queued` in the response reflects whether **this** request
   created the work row.
7. **In-transaction (outbox) dispatch (finding: outbox dispatch).**
   `queue_work_attempt` calls `app.send_task` (`work_dispatch.py:70,111,127`), but
   `app` is an `OutboxCelery` (`celery_app.py:9,17`), so `send_task` does **not**
   contact the broker — it writes a `CeleryOutbox` row **inside the current DB
   transaction** (asserted by `work_dispatch_tests.py:79-82` and
   `observation_work_fault_tests.py:173-230`, which prove the work + outbox rows
   commit atomically with no broker access). The broker message is published only
   after commit by the outbox relay, so a worker can never consume before the
   `WorkflowWork` row is visible — there is **no send-before-commit race to
   defer**. Dispatch therefore runs **inside** the same `transaction.atomic()` as
   the candidate/source/work creation, mirroring distillation
   (`distillation.py:576-584`), so `MemoryCandidate` + `MemoryCandidateSource` +
   `WorkflowWork` + `WorkflowRun` + `CeleryOutbox` all commit atomically:
   `queue_work_attempt(work_id=work.id, now=timezone.now(),
   origin=WorkflowRunOrigin.MANUAL)`. An `on_commit` deferral would be **worse**,
   not better: it splits the run/outbox creation into a second post-commit
   transaction, opening a window where a committed candidate+source+work has no
   run or outbox row if the process dies or the callback raises.
   `created`/`decision_work_queued` is known synchronously from
   `ensure_candidate_decision_work_locked`. The reconciler (extended to agent
   sources, Curation-path changes §6) remains the backstop for the residual
   relay-stall / lost-delivery failures that atomic dispatch cannot itself
   recover.
8. The queued `CANDIDATE_DECISION` work runs `DecideMemoryCandidate`: deterministic
   gates → shortlist → judge decide promote / merge / hold-conflict / reject. The
   agent branches added across the curation path (see **Curation-path changes**)
   let the candidate survive the gates, earn judge tier `supported`, and promote
   through the transition state machine. **The proposal is not instantly
   retrievable** — it is a candidate until curation promotes it.

### Audit event (mirror the write-path style, e.g. `request_scope.py:148-159`)

Every terminal path writes an audit row keyed on `request_id`/`correlation_id`
so retry/request traceability is preserved even on idempotent reuse (finding 5):
`event_type='MemoryProposed'` when this request created the candidate,
`'MemoryProposeReused'` when it returned an existing one (proposed or settled).
Truncate `request_id`/`correlation_id` to the serializer caps before writing.

**The audit row MUST carry `team=candidate.team` (finding: team-scoped audit
leak).** `AuditEvent.team` is nullable (`models.py:1060`) and audit inspection
filters on `Q(team__isnull=True) | Q(team_id__in=scope.team_ids)`
(`inspection/services.py:53`, applied by `ListInspectionAuditEvents` at
`:257,281`), so a `team IS NULL` row is visible to **every** team scope and its
detail exposes actor, candidate id, request/correlation ids, and metadata
(`inspection/views.py:373`). Omitting `team` on a team-scoped proposal (`team_id`
supplied → `visibility_scope=TEAM`) would leak team-private proposal activity to a
reader scoped to another team. Setting `team=candidate.team` scopes the row to the
proposing team; project-scoped proposals (`team_id` absent) correctly leave it
`NULL` (project-global), matching their `PROJECT` visibility.

```python
AuditEvent.objects.create(
    organization=candidate.organization,
    project=candidate.project,
    team=candidate.team,  # team-scope the audit row (NULL only for project proposals)
    event_type='MemoryProposed',  # or 'MemoryProposeReused' on idempotent reuse
    actor_type=scope.actor_type,
    actor_id=scope.actor_id,
    target_type='memory_candidate',
    target_id=str(candidate.id),
    capability='memories:propose',
    result=AuditResult.RECORDED,
    request_id=request_id,
    correlation_id=correlation_id or '',
    metadata={
        'kind': candidate.kind,
        'body_length': len(candidate.body),
        'reused': False,  # True + 'status': candidate.status on the reuse event
    },
)
```

## Error Handling

| condition | HTTP | body |
|-----------|------|------|
| missing/blank required field (`title`,`body`,`request_id`) — incl. blank/whitespace `body` | 400 | DRF serializer errors (`CharField(allow_blank=False, trim_whitespace=True)` rejects `''` and whitespace) |
| oversized `title`/`body`/`request_id`/`correlation_id`/`repository_url` | 400 | serializer `max_length` error (never reaches the audit write) |
| empty **after redaction** (service guard) | 422 | `{'code':'empty_content','detail':'Proposed memory title and body must be non-empty.'}` — a service-level defence; not reachable from a valid HTTP body (redaction only substitutes `[REDACTED]`, never empties a non-blank string, and blank input is already 400). Covered by a **service** unit test, not an API test. |
| `team_id` supplied but scope does not authorize it | 403 | `AccessDeniedError('team_scope_denied')` — raised in `ResolveApiKeyScope._team_ids` (`services.py:344-372`, bearer) / `_session_denial_reason` (`request_scope.py:127`, session non-team-admin) **before the service runs** (finding 5) |
| `team_id` authorized by scope but not linked to the **resolved** project | 422 | `{'code':'team_not_in_project','detail':'Team is not linked to this project.'}` — the service-level `ProjectTeam` link check (`models.py:303-310`); reachable when scope authorized the team independently of the routed project: a **session team-admin** (gets every org team, `auth_services.py:329-330`), or a **bearer** whose team is linked to a different project than the one repository-routing resolved (finding 5) |
| caller lacks `memories:propose` | 403 | `AccessDeniedError('missing_capability')` rendered by `custom_exception_handler` |
| explicit `project_id` absent from scope (nonexistent, or not granted) | 403 | `AccessDeniedError('project_scope_denied')` — `_project_ids` returns `None` for an id not in the org / not granted (`services.py:301-309`), and session auth rejects ids outside its tuple (`request_scope.py:124`); this fires in `resolve_request_scope` **before** `resolve_project_for_scope`, so a nonexistent explicit `project_id` is **403, not 404** (finding 6) |
| missing/malformed routing (`project_id` absent **and** `repository_url` blank/unresolvable) | 400 | `RepositoryUrlRequiredError` → `project_or_repository_required` (`repository.py:16-17`, default status 400) |
| valid but unmatched **repository_url** (no project matches, no create) | 404 | `ProjectNotFoundError('project_not_found')` (`repository.py:20-22,108-118`, status 404) — only the repository path reaches this; an explicit `project_id` never does (it is 403 above) |
| missing/invalid bearer | 401 | `AccessDeniedError('missing_api_key'/'invalid_key')` |

**Two-phase scope vs routing (findings 5, 6).** `resolve_request_scope` runs
first and narrows/authorizes the requested `project_id`/`team_id` against the
scope; `resolve_project_for_scope` runs second and resolves/authorizes the actual
project. As a result the **same invalid relationship can surface with different
statuses depending on the auth/routing path**, and the tests must pin which path
they exercise:
- a nonexistent **explicit** `project_id` → **403 `project_scope_denied`** (scope
  phase), never 404. `project_not_found` (404) is reachable **only** via an
  unmatched `repository_url` (routing phase).
- an unauthorized/unlinked `team_id` → **403 `team_scope_denied`** whenever the
  scope phase can decide it (bearer with explicit project, session non-team-admin);
  the **422 `team_not_in_project`** service check is reached only when the scope
  phase authorized the team independently of the resolved project (session
  team-admin, or bearer routed to a different project). The API tests name the
  path: the 422 case uses a **session team-admin** actor (or a bearer whose team
  links to another project), the 403 case uses a **bearer with an explicit
  `project_id`** the team is not linked to.

`ProposeMemoryError` codes map through a `PROPOSE_STATUS` dict in the view
(`{'empty_content': 422, 'content_too_long': 422, 'team_not_in_project': 422}`,
default 400). Empty-result is not applicable — the endpoint always returns the
created/reused candidate.

Client friendly messages (findings: shared-error-text misfire, CLI remediation).
`_error_text(status, body)` (`mcp_tools.py:400-407`) is **shared by all six**
existing tools (called at `:159,209,248,278,324,365`) and carries **no operation
context**, so a generic `403 missing_capability` branch **there** would wrongly
tell a `search`/`context`/`link`/`observations`/`version`/`feedback` caller (who
needs `memories:read`/`memories:review`/etc.) to grant `memories:propose`. The
reissue hint must therefore be **localized to the propose surfaces**, never added
to `_error_text`:
- **MCP:** the `propose_memory` handler inspects its own response and, **only**
  when `status == 403` and `code == 'missing_capability'`, returns a fixed
  constant `MISSING_PROPOSE_CAPABILITY_MESSAGE = 'Engram: this API key cannot
  propose memories. Re-issue the key via `engram connect` (or the wizard) to grant
  memories:propose (and projects:agent for routing).'`; every other status falls
  through to the shared `_error_text` unchanged.
- **CLI:** the global `ERROR_REMEDIATION['missing_capability']` message is `'Use a
  key with observations:write for hook dry-run.'` (`commands.py:84`) and is shared
  across commands, so `run_memory_propose` must **not** rely on it for a 403 — it
  raises a `CliError` whose remediation is the propose-specific reissue hint
  (mirroring the constant above) rather than the generic
  `remediation_for('missing_capability')` value (finding: CLI remediation).
This exact trigger and text are asserted by dedicated MCP and CLI tests (Test
Plan §7, §9), not by generic `_error_text`/`remediation_for` propagation.

**Accepted risk — no rate limiting only.** A noisy key can create unlimited
proposed candidates; curation gates reject low-signal ones and every create is
audited with the actor. The missing rate limit is the sole accepted v1 risk.

**Lost work is closed by two layers, not one.** In-transaction (outbox) dispatch
(Data Flow step 7) is the fast path: because `app` is an `OutboxCelery`, the
`CeleryOutbox` row commits atomically with the `WorkflowWork`/`WorkflowRun`, so
there is no send-before-commit race and no post-commit callback window to lose.
It does **not** by itself recover a stalled/failed outbox relay or a lost broker
delivery — after either the work row is committed but the `QUEUED` run is never
consumed. Those residual failures are recovered by the reconciler, which this
slice both extends to cover `agent_proposal` candidates **and** teaches to
re-signal a **stale `QUEUED`** run (`dispatched_at` older than `RESIGNAL_WINDOW`)
via `queue_work_attempt`, mirroring the session reconciler's `ATTEMPT_SIGNAL_STALE`
path (Curation-path changes §6). The prior claim that the existing candidate
reconciler already re-signals was wrong: `_repair_candidate` (`:282-286`) returns
on any live run and only re-dispatches work with **no** active run. Atomic dispatch
+ the extended reconciler together close the lost-work path (never-dispatched **and**
lost-delivery); neither alone is sufficient, so the earlier "no reconciler needed
in v1" claim is retracted.

## Test Plan (TDD — failing test first, colocated `<module>_tests.py`)

Backend runner (finding 13): the compose `app` service `working_dir` is
`/srv/app` with `./apps/backend` bind-mounted there
(`docker-compose.yml:10,16`), so pytest paths are **`engram/...`**, not
`apps/backend/engram/...`:
`docker compose -p engram-s4 run --rm app pytest -q engram/memory engram/core`.

1. **Constraint/model** — `engram/core/curation_decision_models_tests.py`
   (append). Failing first:
   - `agent_proposal` source with all of window/stage/import_source/observation
     NULL + valid 64-hex `anchors_hash` saves.
   - each of window/stage/import_source/observation set on an `agent_proposal`
     row raises `IntegrityError`/`ValidationError`.
   - two `agent_proposal` sources for one candidate violate
     `core_candidate_source_agent_uniq`.
   - a **distillation** row with `observation=NULL` raises (regression of the
     re-asserted `observation__isnull=False`, finding 3); existing
     distillation/import rows still save.
   - an **import** row with `observation=NULL` (window/stage NULL,
     import_source set) also raises — the import branch re-asserts the same
     `observation__isnull=False` invariant, and without a dedicated test an
     implementation could drop it from the import branch and still pass every
     other listed case, admitting a null-observation import row that later
     crashes the `source.observation` dereference in
     `memory/import_provenance.py:78` (finding 1).
   Build rows with the existing `memory/transitions_test_support.py` helpers /
   `factory` model factories; typed fixtures (`f_` prefix only when passed in).

   **Existing shared model-contract assertion to *update* (finding: enum contract
   test).** Adding `AGENT_PROPOSAL` to `MemoryCandidateSourceKind` breaks the shared
   contract test `_candidate_source_model_contract` at
   `engram/core/core_models_tests.py:1709-1719`, whose line 1719 asserts
   `{value for value, _label in source_kind.choices} == {'distillation', 'import'}`.
   Update that assertion to
   `== {'distillation', 'import', 'agent_proposal'}` (the file is invoked by the
   declared `engram/core` backend suite, so appending new tests without editing this
   one leaves the suite red). No `observation` null-assertion exists there
   (`:1716-1718` cover only window/stage/import_source), so the nullable-`observation`
   change needs no edit at this site.

   **Migration-executor test (finding: reverse guard + ordering)** —
   `engram/core/migrations_tests.py` (append), mirroring the `0039` precedent
   (`0039_import_provenance.py:7-10,102`, tested via `MigrationExecutor` at
   `migrations_tests.py:2942`). Model/constraint tests validate the *current*
   schema; they cannot validate historical states or reverse-operation order, so
   add an explicit executor test that:
   - migrates **forward** to this migration with **legacy** distillation + import
     rows present (built against the historical model state) and asserts they
     survive — the re-asserted `observation__isnull=False` clause does not reject
     rows that already have a non-null observation;
   - inserts an `agent_proposal` row, attempts to **reverse** past this migration,
     and asserts the reverse **guard raises** (schema left intact, not
     half-reverted) — the guard runs before the schema ops on reverse;
   - deletes the `agent_proposal` row and asserts the reverse then **succeeds**,
     restoring the two-kind check and non-null `observation`.
2. **Evidence manifest** — `engram/memory/import_provenance_tests.py` (new):
   - `candidate_evidence_manifest` on an agent-only candidate returns one entry +
     stable hash; `evidence_manifest` (candidate_decision_work) returns the
     **same** hash; mixed distillation+agent sources **raise** (exact
     `{'agent_proposal'}` set, finding 12).
   - **Validated-provenance negatives (finding: agent-source validation):**
     `validated_agent_candidate_source` / `agent_proposal_evidence_manifest`
     **raise** for (a) a corrupted `anchors_hash` ≠ `sha256(anchors)`, (b) a
     wrong-shape agent row (any of window/stage/import/observation non-null), and
     (c) a foreign-scope agent row — none yield a manifest or a `supported` tier.
   - **Mapping-path regression (finding: Mapping dispatch):** `evidence_manifest`
     still accepts plain `Mapping` sources with no `source_kind` key (the existing
     `candidate_decision_work_tests.py:220-243` scenario) and computes the
     distillation manifest — the kind-dispatch must not `AttributeError` on
     Mappings. Re-run that existing test unchanged plus a new one mixing a Mapping
     entry with an agent model row that **raises**.
3. **Curation-path survival** (findings 1, 2) — colocated with each module:
   - `engram/memory/deterministic_gates_tests.py` (append): `_validate_sources`
     and the noise gates accept an agent-only candidate (no crash, not rejected
     as `unsupported source kind`), and a mixed agent+distillation set still
     raises.
   - `engram/memory/curation_judge_tests.py` (append): an agent-only candidate's
     evidence tier is `supported` (not `none`) and its `latest_evidence_at` is
     `None` (finding: agent conflict precedence); `_apply_evidence_policy` permits
     `publish_new`, `merge_evidence`, `reject_candidate`/`redundant`, and
     `open_conflict` against a supported target (the `None` evidence time makes
     `_deterministic_precedence` `False`), and forbids `revise`/`supersede` and
     `reject_candidate`/`unsupported` for it. **Cross-visibility target guard
     (finding: cross-visibility target / no-terminal-verdict):** a **TEAM** agent
     candidate offered a **PROJECT-global** target is **denied**
     `merge_evidence`/`open_conflict`/`revise_memory`/`supersede_memory` with a
     **non-retryable** (`INVALID_INPUT`-class → `TERMINAL_FAILURE`) error — the guard
     raises the **distinct** `judge_cross_visibility_denied` code (not the retryable
     `judge_policy_denied` that would loop forever); assert both that
     `_apply_evidence_policy` raises `CurationJudgeError('judge_cross_visibility_denied')`
     for the four outcomes and (in `work_failures_tests.py`) that
     `translate_failure` classifies that code as `INVALID_INPUT` — and never a
     transition crash; it is **permitted** `reject_candidate`/`redundant` against the
     target (duplicate); and for a **`mutually_incompatible`** PROJECT target it
     settles via **`publish_new` as a TEAM-visible memory** (the `:471`
     cross-visibility relaxation lets `publish_new` through despite the identity-
     relation comparison), never `judge_policy_denied`-looping. Assert the
     non-retryable classification, the duplicate reject, and the team-local publish
     each explicitly. **Target-traversal validation
     (finding: target-traversal validation):** a shortlist target whose historical
     agent `candidate_source` is malformed raises
     `transition_dependency_unavailable` during
     `build_curation_evidence_context`/`_traverse_target`, so a forged historical
     agent source cannot inflate a target's tier. Cover **all three** malformation
     classes the shared `_validated_agent_anchors` guards, since the target path
     never runs `validated_agent_candidate_source`: (a) a corrupted `anchors_hash`
     (≠ `sha256(anchors)`); (b) **`{}`-anchors / wrong `schema` / missing required
     provenance key** paired with a *matching* recomputed hash (a shape+hash-only
     check would wrongly pass these and earn a `supported` tier); (c) a wrong-shape
     agent source (non-null window/stage/import/observation).
   - `engram/memory/transitions_tests.py` (append): `_source_rows` hydrates an
     agent-only candidate without `KeyError`; `_candidate_fence` accepts it by
     **recomputing** `agent_proposal_candidate_content_hash(...)` from the
     candidate's fields verbatim (a post-creation `body` mutation **and** a
     `candidate.kind` mutation from `''` to `'digest'` each make the promote fail
     `stale_decision`, mirroring `:314-327` — findings: content-hash recompute,
     fence kind-clamp bypass; the `digest` case proves the fence does not
     re-clamp); a promote transition produces a `MemoryVersion` +
     `MemoryVersionSource` with `source_content_hash == anchors_hash`.
     **Per-caller settlement (finding 12):** in addition to promote, exercise the
     other agent-reachable transitions at the transition level, since each passes
     its own **allowed-kind set** through `_candidate_fence` with a different
     target fence and state mutation and the shared `_candidate_fence` test alone
     does not prove each caller admits the agent kind correctly:
     - `merge_evidence` settles an agent candidate into an equivalent target
       (fence on `candidate.content_hash`, agent source row merged);
     - `open_conflict` opens a conflict for an agent candidate;
     - `reject_candidate` rejects an agent candidate;
     - a **mixed** agent + distillation source set passed to any transition
       **raises** `'candidate provenance has mixed source kinds'` (transition-level
       mixed-source rejection), not silently dropping rows.
     - **import-only-promotion boundary preserved (finding: import-only promotion
       boundary):** re-run `test_import_only_candidate_is_rejected_by_non_promotion_
       candidate_transition` (`transitions_tests.py:364`) **unchanged** — an
       **import** candidate still raises `'provenance'` and stays `PROPOSED` on
       merge/revise/supersede/open/resolve (the per-transition allowed set
       `{DISTILLATION, AGENT_PROPOSAL}` excludes import); and add a case that an
       **agent** candidate is **accepted** by those same non-promotion fences,
       proving the set admits agent without admitting import.
     Assert `revise`/`supersede` are **not** exercised by a lone agent source
     (the tier-`supported` policy forbids them), documenting why they are untested
     from the propose path.
4. **Service** — `engram/memory/memory_propose_service_tests.py` (new), stubs
   preferred:
   - creates candidate `status=proposed`, `confidence is None`, `kind` clamped
     (`'digest'`→`''`), redacted title/body; one `agent_proposal` source with
     actor provenance in `anchors`; `decision_work_contract_version == 1` and a
     `CANDIDATE_DECISION` `WorkflowWork` exists.
   - `content_hash` differs when the same title/body is proposed under a
     different `kind` or `team_id` (finding 6); identical inputs collide.
   - idempotent re-execute reuses the candidate, no second source,
     `decision_work_queued=False`, and writes a `MemoryProposeReused` audit
     (finding 5).
   - concurrent create race: a second insert hitting the unique constraint
     reloads the winner and does not raise `IntegrityError` (finding 4). The test
     must actually reach the **nested-savepoint create** and trip the constraint —
     merely pre-creating the winner row makes the unlocked step-5 lookup **find**
     it and take the ordinary reuse branch, so the `except IntegrityError` path is
     never exercised and the test would pass with the race recovery missing or
     broken. Force the initial **miss** followed by a **committed competitor**:
     e.g. patch/stub the step-5 unlocked lookup to return `None` while a winner row
     with the same `content_hash` already exists (so `MemoryCandidate.objects.create`
     raises `IntegrityError`), or use two transactions with a barrier; then assert
     the `except IntegrityError` branch reloads the winner via
     `select_for_update()`, returns it with `decision_work_queued` reflecting the
     winner's existing work, propagates **no** `IntegrityError`, and writes **no**
     second `agent_proposal` source.
   - `team_id` not linked to the project → `ProposeMemoryError('team_not_in_
     project')`, no row written (finding 7); linked team succeeds.
   - the queue dispatch runs **inside** the create transaction (finding: outbox
     dispatch): assert a `CeleryOutbox` row + `WorkflowRun` exist within the
     `transaction.atomic()` block and that no broker `send_task` is invoked
     (mirroring `observation_work_fault_tests.py:173-230`); the dispatch is **not**
     deferred to `on_commit`.
   - blank-after-redaction body → `ProposeMemoryError('empty_content')`, no row.
   - **post-redaction overflow (finding: redaction expansion):** a near-255-char
     JSON title containing a secret expands past 255 after `redact_text` →
     `ProposeMemoryError('content_too_long')`, no row, and never reaches
     `full_clean` (no `ValidationError`/500).
   - **team → TEAM visibility (finding 3):** a proposal with `team_id` creates a
     candidate with `visibility_scope=TEAM` and `team_id` set, so
     `_scope_for` yields `(TEAM, team_id)` and `_claim_bytes` carries the team; a
     proposal without `team_id` is `visibility_scope=PROJECT`.
   - **actor-specific provenance (finding: actor provenance):** a **bearer** key
     records `anchors['api_key_id'] == str(scope.api_key_id)` and
     `actor_type='api_key'`; a **session user** records
     `anchors['api_key_id'] is None` and `actor_type='user'` (never the all-zero
     sentinel).
   - **reconciler re-dispatch + stale re-signal (finding: lost-work backstop):**
     (a) a committed agent candidate whose `queue_work_attempt` never fired
     (simulate by creating the candidate + source + work with **no** `WorkflowRun`)
     is re-dispatched by `reconcile_candidate_work`; (b) a committed agent candidate
     whose `QUEUED` run's `dispatched_at` is **older than `RESIGNAL_WINDOW`** (lost
     broker delivery) is **re-signaled** — the existing `QUEUED` run is reused (its
     `dispatched_at` advances to `as_of`), no second run is created; (c) a **fresh**
     `QUEUED` run (dispatched within `RESIGNAL_WINDOW`) or a `RUNNING` run with a
     **live** lease is left untouched (no double-dispatch); (d) **expired-lease
     reclaim (finding: expired lease abandoned):** a committed agent candidate whose
     work is `LEASED` with `lease_expires_at < as_of` and a stale `RUNNING` run is
     re-signaled so `claim_work` fails the lost run and re-claims — a proposal whose
     worker died mid-decision is revived, not abandoned; (e) **config-changed
     `BLOCKED` re-signal (finding: configuration-blocked abandoned):** a committed
     agent candidate whose work is `BLOCKED` with a `blocked_configuration_fingerprint`
     that **no longer matches** the current `execution_configuration_fingerprint`
     (config fixed) is cleared to `READY` and re-signaled, while a `BLOCKED` work
     whose fingerprint **still matches** (config still broken) is left blocked (no
     re-dispatch); (f) **lock-order / revalidation (finding: reconcile ↔ settlement
     deadlock):** the reconciler acquires work (+v1 runs) with `select_for_update()`
     **before** the candidate, matching every settlement transition's work→candidate
     order, so a concurrent settlement does not deadlock; and when the candidate is no
     longer settleable in the read→lock gap (e.g. promoted, or an unresolved
     `MemoryConflict` appeared, or the source set changed), the revalidation makes
     `_repair_candidate` a **no-op** (`False`) rather than re-signaling stale work.
     Colocated in
     `engram/memory/candidate_work_reconciler_tests.py` (append).
   - `AuditEvent` `MemoryProposed` written with actor + `request_id`, and
     **`team=candidate.team`** (finding: team-scoped audit leak): a team proposal's
     audit row has a non-null `team`, and a project proposal's is `NULL`.
5. **View/API** — `engram/memory/propose_view_tests.py` (new), mocks allowed
   (rule 21):
   - 202 happy path returns `candidate_id`/`status='proposed'`/`decision_work_queued=true`.
   - key without `memories:propose` → 403 `missing_capability`.
   - missing `body` → 400; **blank/whitespace `body` → 400** (serializer
     `allow_blank=False`+`trim_whitespace`, **not** 422 — finding: blank-body
     status); oversized `request_id` → 400 (not 500, finding 11);
     **oversized `body` (> `MEMORY_PROPOSE_BODY_MAX_LENGTH`) → 400** (finding: body
     cap). The 422 `empty_content` path is **not** an API test — it is unreachable
     from a valid HTTP body and is covered by the §4 service test.
   - **team status by path (findings 5):** a **bearer** request with an explicit
     `project_id` whose `team_id` is not linked → **403 `team_scope_denied`**
     (scope phase); a **session team-admin** whose `team_id` is not linked to the
     resolved project → **422 `team_not_in_project`** (service phase). Both are
     asserted, each naming its auth path.
   - **team-bound key confinement (finding: team-bound project write):** a
     **team-bound bearer key** (`key.team_id` set) that **omits** `team_id` creates
     a candidate with `visibility_scope=TEAM` scoped to the bound team — **not** a
     PROJECT-global candidate — so a confined key cannot inject project-wide memory.
   - **project status by path (finding 6):** a nonexistent **explicit**
     `project_id` → **403 `project_scope_denied`** (never 404); an unmatched
     **`repository_url`** (project_id absent) → **404 `project_not_found`**;
     project_id absent **and** blank/unresolvable repository_url → **400
     `project_or_repository_required`**.
   - idempotent re-POST returns the same `candidate_id`.
6. **End-to-end candidate-work settlement** (findings 1, 2 — the gap that lets
   1–2 pass every unit test) — `engram/memory/propose_e2e_tests.py` (new):
   drive `ProposeMemory().execute` then run the real
   `DecideMemoryCandidate().execute` on the queued work (fake judge forced to
   `publish_new`) and assert the candidate reaches an **approved `Memory`** — i.e.
   a novel agent proposal actually settles durably, not just that the worker
   does not crash. A second case with a fake judge `reject_candidate`/`redundant`
   against an equivalent supported target settles to `rejected` — a lone agent
   proposal has tier `supported`, so a targetless `reject_candidate`/`unsupported`
   is not a valid outcome for it (finding: agent conflict precedence). A third
   case **corrupts the agent source `anchors_hash`** before
   running the decision work and asserts the candidate **does not promote** (the
   validated-provenance check rejects it), proving a forged hash cannot reach an
   approved `Memory` end-to-end (finding: agent-source validation).

Client — canonical CLI suite (finding 13) runs in the repo container. The root
`app` compose service mounts **only** `./apps/backend:/srv/app`
(`docker-compose.yml:10,16`) — there is no `/srv/cli` mount and the workflow runs
**unittest**, not pytest (`.github/workflows/backend.yml:103-104`:
`PYTHONPATH=packages/cli python3 -m unittest discover -s packages/cli -p
'*_tests.py'`). The canonical container command is the documented
`deploy/compose` `api` harness (`packages/cli/README.md:71-79`). **The command
must set `ENGRAM_ENV_FILE` to a tracked file and use a unique compose project name
(finding: CLI verification command).** The `api` service inherits
`env_file: - ${ENGRAM_ENV_FILE:-.env}` (`deploy/compose/docker-compose.yml:10-11`),
and `.env` is git-ignored (`.gitignore:5`) so a **fresh mandated worktree** has only
the tracked `deploy/compose/.env.example` — the default `.env` is absent and
`docker compose` aborts at parse time before Python starts. Point `ENGRAM_ENV_FILE`
at `deploy/compose/.env.example` (its contents are irrelevant here: `--no-deps` +
`--entrypoint python3` runs the CLI unittest suite with no DB/broker). And the repo
requires a **unique `-p` project name per worktree** (`CLAUDE.md`, worktree
quickstart) or concurrent worktrees collide on the default `engram` project:

```bash
ENGRAM_ENV_FILE="$PWD/deploy/compose/.env.example" \
docker compose -p engram-s4-cli -f deploy/compose/docker-compose.yml run --rm --no-deps \
  -v "$PWD:/workspace" -w /workspace \
  -v /usr/bin/git:/usr/bin/git:ro -v /usr/lib/git-core:/usr/lib/git-core:ro \
  -e PYTHONPATH=/workspace/packages/cli --entrypoint python3 api \
  -m unittest discover -s packages/cli -p '*_tests.py' -v
```

(the only tracked env template is `deploy/compose/.env.example` — there is **no**
repo-root `.env.example` — so an **absolute**
`ENGRAM_ENV_FILE="$PWD/deploy/compose/.env.example"` is used. A bare relative
`.env.example` would be resolved by Compose against the compose file's directory
`deploy/compose/`, and `$PWD/.env.example` (repo root) does not exist and aborts
Compose at parse time. `-p engram-s4-cli` keeps the project name unique per
worktree.)

After the canonical suite is green, the plugin contract suites must also pass
(`python scripts/sync_plugin_bundle.py --check`, then the Claude/Codex plugin
unittest harnesses per `backend.yml:106-110`), since the bundle copies are
byte-synced.

**Existing six-tool / capability assertions to *update*, not just append to
(finding: hardcoded tool counts).** Adding the seventh tool + `memories:propose`
breaks these hard-coded checks unless each is edited explicitly:
- `mcp_tools_tests.py:531-544` — `test_build_tools_exposes_six_tools` (rename the
  test + add `engram_memory_propose` to the expected key list).
- `mcp_server_tests.py:96-120` — the exact tool-name list **and** the index-based
  schema assertions (`tools[2]`/`[3]`/`[4]`): append the new tool **last** so the
  existing indices stay valid, and add its own schema assertion.
- `mcp_server_tests.py:455,504` and `cli_lifecycle_tests.py:3532` — the
  `len(tools) == 6` / `assertEqual(6, ...)` counts become `7`.
- `cli_lifecycle_tests.py:2929-2935` — the issued-key capability list (today
  `['memories:read','observations:write','search:query']`) gains **both**
  `memories:propose` **and** `projects:agent` (see §9 and finding: wizard key
  unusable).
- `scripts/e2e_claude_plugin.py:253-255` — `len(tools) != 6` becomes `7`.
- `scripts/e2e_codex_plugin.py:31-38,656-658` — `EXPECTED_MCP_TOOLS` gains
  `engram_memory_propose` and the "exact six tools" message becomes seven.
- `scripts/e2e_golden_path.py:386` — `assert_equal(len(tool_names), 6, 'mcp tools
  count')` becomes `7` (finding: golden-path tool count). This script is run by the
  **Compose E2E** workflow (`.github/workflows/compose-e2e.yml:27`), a separate CI
  gate from the plugin harnesses; registering the seventh tool without editing it
  deterministically reds Compose E2E.
Appending the new tool **last** in `build_tools`/`mcp_server` keeps the existing
positional (`tools[2..4]`) schema assertions valid.

7. **MCP handler** — `packages/cli/engram_cli/mcp_tools_tests.py` (append):
   `propose_memory` posts to `/v1/memories/propose` with `_scope_payload` +
   `request_id`, requires title+body (friendly error otherwise), renders
   `candidate_id`/`status`/`decision_work_queued`, surfaces non-403 HTTP errors
   via the shared `_error_text`; a **403 `missing_capability`** response renders
   the exact `MISSING_PROPOSE_CAPABILITY_MESSAGE` constant from the
   `propose_memory` handler (finding: shared-error-text misfire), distinct from
   the generic `HTTP 403 ...` text — plus a regression asserting **another tool**
   (e.g. `submit_memory_feedback`) that receives a `403 missing_capability` does
   **not** render the propose hint (its shared `_error_text` is unchanged).
8. **MCP schema** — `packages/cli/engram_cli/mcp_server_tests.py` (append): a tool
   named `engram_memory_propose` is listed with `inputSchema` properties
   `title,body,kind,project_id`, `required: ['title','body']`, and a description
   stating it goes through curation and is not instantly retrievable.
9. **CLI + wizard** — `packages/cli/engram_cli/cli_lifecycle_tests.py` (append):
   `engram memory propose --title ... --body ...` posts and renders the response;
   a **403 `missing_capability`** response renders the propose-specific reissue
   remediation from `run_memory_propose` (finding: CLI remediation), **not** the
   generic `observations:write` `remediation_for('missing_capability')` value;
   `WIZARD_API_KEY_CAPABILITIES` includes **both** `memories:propose` **and**
   `projects:agent`, **and the existing issued-key capability assertion at
   `cli_lifecycle_tests.py:2929-2935`** (today
   `['memories:read','observations:write','search:query']`) is updated to include
   both (`install_tests.py` issued-key capability list likewise). A CLI/wizard test
   also asserts a wizard-issued key can actually **resolve a project** (repository-
   routed propose does not 403 `project_scope_denied`) — the regression the missing
   `projects:agent` would cause (finding: wizard key unusable).

### Client change surface

- `mcp_server.py`: add the `engram_memory_propose` tool dict **last** (mirror
  `:184-201`; appending last keeps existing positional schema assertions valid —
  finding: hardcoded tool counts).
  Description (approx): "Record a durable, non-obvious engineering fact the user
  asked to remember or that you verified. Goes through curation (dedup + judge),
  so it is not instantly retrievable. Do not use for transient or unverified
  notes."
- `mcp_tools.py`: `propose_memory(arguments, config_dir, transport)` handler +
  register in the `bind` map (`:131`); reuse `_scope_payload`, `_new_request_id`,
  `post_json`, `_error_text`; accept 202 as success.
- `main.py`: `memory propose` subparser (`--title`, `--body`, `--kind`,
  `--request-id`, `--config-dir`, `--project`) + dispatch (`:65-71`).
- **MCP / plugin docs (finding 2)** — shipping a seventh tool makes the shipped
  six-tool contract stale everywhere it is stated. Update, in the same slice:
  - `docs/mcp-tools.md`: bump "Six tools ship" (`:27`) to seven, add the
    `engram_memory_propose` row to the Shipped Tool Set table (`:32-39`), fix
    the "All six are developer-scoped"/"All six also accept" prose (`:41-46`) to
    "All seven", and remove the `memory.propose` bullet from the **Deferred**
    section (`:64-67`) — it is now shipped, not deferred.
  - `docs/guides/mcp.md` (`:164`): update the six-tool contract listing to
    include `engram_memory_propose`.
  - `packages/claude-plugin/README.md` (`:118`) and
    `packages/codex-plugin/README.md` (`:40`): update the shipped-tool lists so
    neither README tells users the newly shipped tool is unavailable.
- `commands.py`: `run_memory_propose` (mirror `run_memory_version` `:2095`) that,
  on a 403 `missing_capability`, raises a `CliError` with the propose-specific
  reissue remediation instead of the shared
  `remediation_for('missing_capability')` (`:84`, `observations:write`) value
  (finding: CLI remediation); add **both** `'memories:propose'` **and**
  `'projects:agent'` to `WIZARD_API_KEY_CAPABILITIES` (`:57-61`).
  **`projects:agent` is required, not optional (finding: wizard key unusable).**
  The wizard issues an **unbound** key (`console/views/api_keys.py:157` →
  `issue_api_key` with no `project`/`team`; the serializer's `project`/`team` are
  read-only — `console/serializers/api_keys.py:58-60`). For an unbound key,
  `ResolveApiKeyScope._project_ids` (`access/services.py:293`) returns `None`
  (→ 403 `project_scope_denied`) unless the **effective** caps contain
  `projects:*`, `policy:admin`, or `projects:agent` — and effective caps are
  `owner ∩ key` (`services.py:126`), so the *key* itself must carry one. A wizard
  set of only `memories:propose` (+ read/observations/search) contains none, so the
  reissued key would 403 on **every** propose (repository-routed or explicit
  `project_id` alike — the unbound-key gate fires before the requested-project
  branch). `projects:agent` is grantable: it is seeded on the `organization_owner`/
  `organization_admin` roles (`migrations/0007_seed_projects_agent_capability.py`),
  so the wizard's org-admin issuer holds it and `_issuer_can_grant`
  (`console/services.py:319`) permits it, and the `owner ∩ key` intersection then
  includes it. This mirrors the golden-path agent key
  (`AGENT_KEY_CAPABILITIES` carries `projects:agent`,
  `engram_bootstrap_golden_path.py:26-33`). Without this the "re-issue via
  `engram connect`" remediation produces a key that cannot propose.
- **Bundle byte-sync (required):** after canonical CLI tests are green, run
  `python scripts/sync_plugin_bundle.py`, then
  `python scripts/sync_plugin_bundle.py --check` (exit 0). Never edit
  `packages/claude-plugin/hooks/engram_cli/` or
  `packages/codex-plugin/hooks/engram_cli/` by hand.

## Out of Scope

- Direct-to-approved memory creation (bypassing curation).
- A `confidence` request param.
- Per-key rate limits / abuse throttling.
- Batch propose (multiple memories per request).
- Auto-granting `memories:propose` to already-issued keys — keys issued before
  this slice must be re-issued via the wizard to gain it; documented, not
  automated.
- Frontend/console surface for agent proposals.
- (Removed from Out of Scope — reconciler now covers agent proposals.) The
  reconciler is extended to re-dispatch stuck `agent_proposal` candidates
  (Curation-path changes §6) because in-transaction outbox dispatch alone does not
  survive a stalled outbox relay or a lost broker delivery. In scope for v1.
- Forcing re-evaluation of settled content. Re-proposing content whose candidate
  is already promoted/rejected returns the existing candidate (content-hash
  dedup); deliberately reproposing rejected content for a fresh curation pass is
  not supported in v1 (would require a distinct re-open path). The reuse is
  audited (`MemoryProposeReused`) so the attempt is still traceable.
- **Revalidating the rejection target on the `reject_candidate`/`redundant` path
  (finding: redundant-reject stale target).** `_settle_model_rejection`
  (`curation.py:1340-1385`) locks the work and candidate but **not** the target
  memory, and the orchestrator branches to it (`:1300-1313`) **before** the
  post-judge `_revalidate_shortlist` (`:1321`, non-reject path only), so a target
  revised/archived/refuted in the narrow window between the judge call (`:1289`) and
  the rejection transaction (`:1354`) is not re-checked — a redundant rejection can
  settle against a now-stale target. This is a **pre-existing** property of the
  rejection path, **shared identically with distillation** redundant-rejections
  (not introduced by this slice), the race window is a single task execution, and
  the outcome **fails closed** (rejects a valid proposal rather than corrupting or
  publishing wrong memory) and stays traceable (`MemoryProposeReused` + audit) with
  human-review recourse. Adding target locking/revalidation to the shared reject
  path is a distillation-wide change out of scope for the propose slice; left
  as-is in v1.

## Review Reconciliation

(append-only; empty at spec time)

- round 1, finding N/A, verdict refuted:false positive — no findings to reconcile: the adversarial Codex review aborted before producing any output (`failed to load configuration: No such file or directory (os error 2)`, a Codex CLI environment/config fault, not a repo or prompt issue). Retrying and altering forwarded task text cannot change the outcome. No numbered findings exist to verify, fix, or refute; spec left unchanged.
- round 2, finding N/A, verdict refuted:false positive — again no findings to reconcile: the Codex review Bash invocation failed with a configuration error and no task started, so the reviewer emitted zero numbered findings. Nothing to verify against code; spec left unchanged.
- round 3, finding N/A, verdict refuted:false positive — no findings to reconcile: the Codex review task exceeded the 10-minute foreground timeout and was moved to background; no completed stdout was captured, so the reviewer produced zero numbered findings. Nothing to verify against code; spec left unchanged.
- round 4, finding N/A, verdict refuted:false positive — no findings to reconcile: the Codex review (background job bk2ad0bbq) again exceeded the 600s foreground window and returned no completed stdout, so the reviewer emitted zero numbered findings. Nothing to verify against code; spec left unchanged.
- round 5, finding 1, verdict fixed — CONFIRMED: `deterministic_gates._validate_sources` derefs `source.observation` at `:265-267` before the kind check and its `else` (`:284`) rejects any non-distill/import kind; the NOISE_LIFECYCLE gate (`:520`) derefs observation too. Added explicit agent branches to both in the new Curation-path changes §2 and Evidence constraint 4.
- round 5, finding 2, verdict fixed — CONFIRMED, fundamental: judge `_eligible_group_hash` (`:196`) returns None for non-distillation → tier `none` → `_apply_evidence_policy` permits only rejection; and promotion `_candidate_fence` (session reconstruction `:308-317`) + `_promotion_uses_import_source` (`:1328`) + `_source_rows` (`:419` KeyError) reject/crash agent sources across all six transitions. Redesigned: agent source earns tier `supported` from anchors_hash, fences on `candidate.content_hash` like import, and threads through `_source_rows`/fence/promotion (Curation-path changes §3–§4) with an e2e settlement test (Test Plan §6).
- round 5, finding 3, verdict fixed — CONFIRMED: nullable `observation` (`models.py:2244`) drops the non-null invariant; added `observation__isnull=False` to the distillation/import branches of both the DB check constraint and `_validate_source_shape`.
- round 5, finding 4, verdict fixed — CONFIRMED: content-hash unique constraint (`models.py:673-676`) races two concurrent unlocked lookups; added the distillation-style savepoint + `IntegrityError` catch + winner reload (Data Flow step 5) and a race test.
- round 5, finding 5, verdict fixed — CONFIRMED spec inconsistency: `request_id` was labelled idempotency key but the algorithm keys on content hash. Reclassified `request_id` as audit/retry correlation only, added a `MemoryProposeReused` audit on every reuse path, and documented rejected-content dedup in Out of Scope.
- round 5, finding 6, verdict fixed — CONFIRMED: curated identity includes kind + team (`deterministic_gates._claim_bytes:288-297`) but the hash omitted them, enabling cross-team/kind candidate collision under the per-(org,project,content_hash) unique. Added `clamped_kind` and `team_id` to `agent_proposal_candidate_content_hash`.
- round 5, finding 7, verdict fixed — CONFIRMED: `ProjectTeamLink` (`models.py:304-310`) links teams to projects, but scope authorizes team independently and `MemoryCandidate.clean` checks only org; propose is the only path taking request `team_id`. Added a required ProjectTeamLink validation (422 `team_not_in_project`).
- round 5, finding 8, verdict fixed — CONFIRMED: `queue_work_attempt` sends the celery task immediately (`work_dispatch.py:70,111,127`); inside an open txn a fast worker hits the non-retryable `MemoryWorkerError` (`tasks.py:129`, default retryable=False) with no reconciler for agent sources → permanent loss. Moved dispatch to `transaction.on_commit` (Data Flow step 7) and rewrote the accepted-risk note.
- round 5, finding 9, verdict fixed — CONFIRMED: no reverse guard / drain order vs `0039`'s `_guard_reverse`. Added a reverse guard that refuses to reverse while agent rows exist and a migrate-first-then-code deployment ordering note.
- round 5, finding 10, verdict fixed — CONFIRMED: `RepositoryUrlRequiredError` default code `project_or_repository_required` status 400 (`repository.py:16-17`) is distinct from `project_not_found` 404. Split the error table into both rows and required both in API tests.
- round 5, finding 11, verdict fixed — CONFIRMED: unbounded `CharField`s vs 255/1024 caps and `AuditEvent` 255 columns (`models.py:1068-1069`) — oversized `request_id` would 500 via the scope-audit write. Added `max_length` caps to the serializer and a 400 test.
- round 5, finding 12, verdict fixed — CONFIRMED contradiction: helper said "exactly one agent source" while the delegator said "any agent source" and the test plan said mixed must raise. Standardized on the exact `{'agent_proposal'}` source-kind set (raise on mixed) across both callers and the delegator.
- round 5, finding 13, verdict fixed — CONFIRMED: compose `working_dir=/srv/app` mounts `apps/backend` there (`docker-compose.yml:10,16`), so paths are `engram/...`; corrected all pytest paths, specified the CLI container command + plugin contract suites, and added the missing e2e candidate-work settlement test (Test Plan §6).
- round 5, finding 14, verdict fixed — CONFIRMED: `_error_text` special-cases only `project_not_found` (`mcp_tools.py:402`). Specified the exact trigger (403 + `missing_capability`), a fixed reissue-hint constant, and a dedicated assertion (Test Plan §7).
- round 6, finding 1 (blocker), verdict fixed — CONFIRMED: migrate-first-then-code does not close the rolling-deploy window; a new web replica can queue an `agent_proposal` while an old worker (no agent branch) consumes it and crashes on `_validate_sources` null-observation deref/`else` reject (`deterministic_gates.py:265,284`). Added a default-off `ENGRAM_MEMORY_PROPOSE_ENABLED` write flag (503 `propose_disabled` until flipped) plus a 4-step ordering (migrate → roll out all web+worker code with flag off → confirm no old worker → flip flag). Endpoint section, deployment ordering, error table, and view test §5 updated.
- round 6, finding 2 (blocker), verdict fixed — CONFIRMED: `transaction.on_commit` only fixes send-before-commit; a broker outage / process kill / `send_task` exception at callback time leaves committed work undispatched, and the reconciler filters distillation-only (`candidate_work_reconciler.py:159-164,267-276`) → permanent stuck proposal. Extended the reconciler to cover single-`agent_proposal` candidates (Curation-path changes §6), retracted the "no reconciler needed" claim, and added a reconciler re-dispatch test (§4). Dispatch + reconciler are the two closing layers.
- round 6, finding 3, verdict fixed — CONFIRMED: candidate was created `visibility_scope=PROJECT`, and `_scope_for` returns `(PROJECT, None)` regardless of team (`deterministic_gates.py:242-243`), so `_claim_bytes` dropped the team even though the content hash carried it — team-distinct candidates would collapse to one curated claim. Made visibility `TEAM` iff `team_id` supplied (Data Flow step 5), aligning content hash, `_scope_for`, `_claim_bytes`, and dedup; added a service test.
- round 6, finding 4, verdict fixed — CONFIRMED: session scope uses the all-zero `api_key_id` sentinel (`auth_services.py:257-260`), so `str(scope.api_key_id)` would falsely stamp a zero UUID as an API-key identity on user-authored proposals. Made `anchors['api_key_id']` actor-specific (`None` for `actor_type='user'`), with service tests for both auth paths.
- round 6, finding 5, verdict fixed — CONFIRMED: an unlinked `team_id` yields **403 `team_scope_denied`** in the scope phase (`services.py:344-372` bearer / `request_scope.py:127` session non-admin) but **422 `team_not_in_project`** only when the scope authorized the team independently (session team-admin `auth_services.py:329-330`, or bearer routed to another project). Split the error table into both rows, documented the two-phase behavior, and pinned each API test to its auth path. Also corrected the model name to `ProjectTeam` (there is no `ProjectTeamLink` class, `models.py:303`).
- round 6, finding 6, verdict fixed — CONFIRMED: `resolve_request_scope` runs before `resolve_project_for_scope`; a nonexistent explicit `project_id` is rejected by `_project_ids` (`services.py:301-303`) as **403 `project_scope_denied`**, not 404. Only an unmatched `repository_url` reaches `ProjectNotFoundError` (`repository.py:108-118`). Corrected the error table to distinguish explicit-id (403) from repository (404) and updated the API test.
- round 6, finding 7, verdict fixed — CONFIRMED contradiction: `CharField(allow_blank=False, trim_whitespace=True)` rejects blank/whitespace `body` with 400 before the service runs, so the old "blank body → 422 `empty_content`" API test could never pass, and redaction never empties a non-blank string. Reclassified `empty_content` as an unreachable-from-HTTP service guard (service unit test only), and set the API test to blank body → 400.
- round 6, finding 8, verdict fixed — CONFIRMED: `body` had no size cap while comparable write paths cap at 16000 (`serializers.py:48`), enabling unbounded embedding/judge cost with no rate limit. Added `MEMORY_PROPOSE_BODY_MAX_LENGTH = 16000` to the `body` field plus an oversized-body 400 test.
- round 6, finding 9, verdict fixed — CONFIRMED: the proposed `agent_proposal_evidence_manifest` trusted stored `anchors`/`anchors_hash` and the judge trusted `source.anchors_hash` directly, while imports validate via `_validated_import_source` (`import_provenance.py:109`) and the model only proves 64-hex, not `anchors_hash == sha256(anchors)`. Added `validated_agent_candidate_source` (recomputes hash + shape + scope) used by the manifest and depended on by the judge tiering, with negative unit tests and a corrupted-hash e2e non-promotion test.
- round 6, finding 10, verdict fixed — CONFIRMED: `evidence_manifest` accepts `Iterable[MemoryCandidateSource | Mapping]` and an existing test passes bare dicts with no `source_kind` (`candidate_decision_work_tests.py:220-243`); a naive `{s.source_kind ...}` would `AttributeError` on Mappings. Specified kind-dispatch over model rows only (`isinstance(s, MemoryCandidateSource)`), Mappings treated as distillation, with the existing dict test kept as a regression plus a mixed-Mapping-raises test.
- round 6, finding 11, verdict fixed — CONFIRMED: Test Plan §1 had only current-model constraint tests, not the `MigrationExecutor` reverse-order test the `0039` precedent uses (`migrations_tests.py:2942`). Added an executor test (legacy rows survive forward; agent row blocks reversal before schema ops; reversal succeeds after removal) and specified placing the `RunPython(noop, _guard_reverse)` step last so it runs first on reverse.
- round 6, finding 12, verdict fixed — CONFIRMED: agent proposals can exercise publish/merge/conflict/reject but the test plan covered only promote + orchestrator publish/reject, and the shared `_candidate_fence` test does not prove each caller derives the source kind. Added transition-level `merge_evidence`/`open_conflict`/`reject_candidate` settlement tests plus a transition-level mixed-source rejection test (Test Plan §3).
- round 6, finding 13, verdict fixed — CONFIRMED: the root `app` service mounts only `./apps/backend:/srv/app` (no `/srv/cli`) and the workflow runs `unittest` not pytest (`backend.yml:103-104`), so `--workdir /srv/cli app python -m pytest` was invalid. Replaced with the documented `deploy/compose` `api` harness command (`README.md:71-79`) and the plugin unittest suites. (The backend pytest runner `-p engram-s4 run --rm app pytest -q engram/...` remains valid and unchanged.)
- round 6, finding 14, verdict fixed — CONFIRMED: `redact_value` reserializes JSON-shaped strings with `json.dumps(sort_keys=True)` (`redaction.py:62-70`), whose `', '`/`': '` separators expand length, so a title within the 255 serializer limit can exceed `MemoryCandidate.title.max_length` after redaction and trip `full_clean` (`models.py:25`) → 500. Added post-redaction length re-validation raising `ProposeMemoryError('content_too_long')` (422) with a regression test (Data Flow step 1, Test Plan §4).
- round 7, finding 1 (blocker), verdict fixed — CONFIRMED the round-5 finding-8 fix was itself wrong: `engram.celery_app.app` is an `OutboxCelery` (`celery_app.py:9,17`), so `app.send_task` writes a `CeleryOutbox` row **inside the current DB transaction** (proven by `work_dispatch_tests.py:79-82` + `observation_work_fault_tests.py:173-230`, which assert atomic work+outbox commit with zero broker access; the relay publishes only post-commit). There is no send-before-commit broker race, and `on_commit` deferral is strictly worse — it opens a post-commit window where committed candidate+source+work have no run/outbox if the process dies or the callback raises. Reverted to in-transaction dispatch mirroring distillation (`distillation.py:576-584`); rewrote Data Flow step 7, step 5 sub-branches, the lost-work section, Evidence constraint 6, Curation-path §6, Out of Scope, and Test Plan §4 (assert `CeleryOutbox`+`WorkflowRun` exist within the atomic block, no broker send, not `on_commit`).
- round 7, finding 2 (blocker), verdict fixed — CONFIRMED: at tier `supported` the advertised `open_conflict` is unreachable and `reject_candidate`/`unsupported` is invalid. `_apply_evidence_policy` open_conflict (`curation_judge.py:420-431`) requires `not _deterministic_precedence`, i.e. equal-or-None evidence times (`:363-366`); the round-5 fix set agent evidence time to `source.created_at` (`_source_evidence_time`), which always differs from a target's observation time → deterministic precedence holds → every agent conflict is `judge_policy_denied`, a **retryable** `_operational` error (`curation.py:1290,1062`) that retries until the attempt budget is exhausted (stuck proposal). `reject_candidate`/`unsupported` needs candidate tier `none` (`:418-419`), never true for an agent source. Fixed: agent `_source_evidence_time` returns **`None`** (not `created_at`), which makes `_deterministic_precedence` `False` (open_conflict reachable) while `_candidate_precedes` (`:349`) stays `False` (revise/supersede still blocked, as intended). Corrected the Design tier bullet, Curation-path §3, the e2e reject case (must be `reject_candidate`/`redundant` against a supported target), and the judge test plan.
- round 7, finding 3 (major), verdict fixed — CONFIRMED: `MemoryCandidate.title`/`body`/`kind` are mutable (`models.py:639-707`), so anchoring the fence on the stored `candidate.content_hash` (`_candidate_fence` `:305-306`, import branch) and comparing it to itself (`:319`) is tautological and cannot detect a post-creation body edit — while the distillation branch recomputes (`:317`) and is pinned by `transitions_tests.py:314-327`. Changed the agent fence to **recompute** `agent_proposal_candidate_content_hash(candidate.title, candidate.body, clamp_memory_kind(candidate.kind), candidate.team_id)`; updated the Design fence bullet, Curation-path §4, and added a body-mutation → `stale_decision` regression test.
- round 7, finding 4 (major), verdict fixed — CONFIRMED: `_eligible_group_hash` is called by `_traverse_target` (`curation_judge.py:257-266`) on a shortlist target's historical `MemoryVersionSource.candidate_source`, a path that never runs `validated_agent_candidate_source`; the round-5 agent branch `return source.anchors_hash` trusted the stored hash, so a corrupted historical agent source would inflate a *target's* tier. Distillation validates inline (`:205-213`). Fixed: the agent branch now validates shape + recomputes `sha256(canonical_json_bytes(anchors)) == anchors_hash` inline (raising `transition_dependency_unavailable` on mismatch) via a shared helper reused by the evidence-manifest validator; added a target-traversal negative test (Test Plan §3).
- round 7, finding 5 (major), verdict fixed — CONFIRMED: `_error_text(status, body)` (`mcp_tools.py:400-407`) has no operation context and is shared by all six tools (`:159,209,248,278,324,365`), so the round-6 finding-14 fix (generic `403 missing_capability` branch there) would misdirect search/context/link/observations/version/feedback callers to grant `memories:propose`. Relocated the hint: `propose_memory` returns `MISSING_PROPOSE_CAPABILITY_MESSAGE` only for its own 403, all other statuses fall through to the unchanged `_error_text`; added a regression asserting another tool's 403 does not render the propose hint (Test Plan §7).
- round 7, finding 6 (minor), verdict fixed — CONFIRMED: `_error_text` is MCP-only; direct CLI commands use `error_from_body`/`remediation_for` and `ERROR_REMEDIATION['missing_capability']` = 'Use a key with observations:write for hook dry-run.' (`commands.py:84`), so `engram memory propose` would show `observations:write` guidance. Specified `run_memory_propose` to raise a `CliError` with the propose-specific reissue remediation for a 403, not the shared map value; added a CLI test (Test Plan §9) and updated the client change surface.
- round 7, finding 7 (minor), verdict fixed — CONFIRMED: `mcp_tools_tests.py:531-544`, `mcp_server_tests.py:96-120,455,504`, `cli_lifecycle_tests.py:2929-2935,3532`, `scripts/e2e_claude_plugin.py:253-255`, and `scripts/e2e_codex_plugin.py:31-38,656-658` hard-code exactly six tools / the current capability list, so appending is insufficient. Added an explicit 'assertions to update, not append' enumeration to the Test Plan (six→seven counts, tool-name lists, `test_build_tools_exposes_six_tools` rename, `EXPECTED_MCP_TOOLS`, issued-key capabilities) and specified appending the new tool **last** to keep the positional `tools[2..4]` schema assertions valid.
- round 8, finding 1 (blocker), verdict fixed — CONFIRMED: `_repair_candidate` returns `False` on any live `QUEUED`/`RUNNING` run (`candidate_work_reconciler.py:282-286`) and only re-dispatches work with **no** active run via `_requeue_eligible` (`:280`), so the spec's claim that "the duplicate-run guard re-signals the committed work" was false — a lost broker delivery leaves a `QUEUED` run that the candidate reconciler never re-signals (unlike the session reconciler, whose `_classify_queued:138-147` classifies a stale `QUEUED` run as `ATTEMPT_SIGNAL_STALE` and calls `queue_work_attempt` to re-signal via `_eligible_queued_run` `work_dispatch.py:103-113`). Redesigned Curation-path §6 to replace the unconditional early-return with the session-reconciler rule: leave `RUNNING`/fresh-`QUEUED` runs alone, re-signal a stale `QUEUED` run (`dispatched_at` None or older than `RESIGNAL_WINDOW`) through `queue_work_attempt` (recovering distillation **and** agent proposals). Corrected Evidence 6, the lost-work section, and added a stale-`QUEUED` re-signal test (Test Plan §4).
- round 8, finding 2 (blocker), verdict fixed — CONFIRMED: "Rollback is symmetric" is false once any `agent_proposal` row exists — durable source rows, post-promotion `MemoryVersionSource` rows, and open `MemoryConflict` persist, and pre-slice code lacks every agent branch (`_source_rows:419` KeyError on null observation, `_validate_conflict_resolution_rows:2570` + all transition fences hardcode `DISTILLATION`, `_eligible_group_hash` curation_judge.py:196 discards the agent source and mis-tiers traversed targets); draining removes none of that and the reverse guard blocks schema rollback. Replaced the symmetric-rollback claim with an accurate pre-activation-only clean-rollback window and a fix-forward-after-activation procedure (deployment ordering section).
- round 8, finding 3 (major), verdict fixed — CONFIRMED: `AuditEvent.team` is nullable (`models.py:1060`) and audit inspection filters `Q(team__isnull=True) | Q(team_id__in=scope.team_ids)` (`inspection/services.py:53`, applied by `ListInspectionAuditEvents:257,281`), so a `team IS NULL` audit row for a team-scoped proposal is visible to any team scope and leaks actor/candidate-id/request-ids/metadata (`inspection/views.py:373`). Added `team=candidate.team` to the audit create (NULL only for project proposals) and a service test asserting team-scoped vs project-scoped audit team values.
- round 8, finding 4 (major), verdict fixed — CONFIRMED: `clamp_memory_kind` collapses both `digest` and unknowns to `''` (`models.py:137-141`), so the fence recompute over `clamp_memory_kind(candidate.kind)` could not detect a `''`→`digest` mutation while promotion copies the **raw** `candidate.kind` into memory metadata (`transitions.py:1351`), leaking an un-clamped `digest`. Changed `agent_proposal_candidate_content_hash` to hash `kind` **verbatim** (clamp once in the service at creation; fence passes stored `candidate.kind` unchanged), so any post-creation kind change trips `stale_decision`; added a `''`→`digest` fence regression (Test Plan §3). The `visibility_scope`/`confidence` parts are refuted-in-place as safe: visibility is re-derived and re-checked by `_revalidated_effective_scope` (`transitions.py:1002-1025`, raises `stale_decision` on divergence), confidence is created `NULL` and set only by curation — both identical to distillation's established fence, with no in-slice mutation path; documented in Curation-path §5.
- round 8, finding 5 (major), verdict fixed — CONFIRMED: pre-creating the winner row makes the unlocked step-5 lookup find it and take the reuse branch, so the nested-savepoint `except IntegrityError` recovery is never exercised and the test could pass with the race handler missing/broken. Rewrote the Test Plan §4 race case to force the initial miss + a committed competitor (stub the step-5 lookup to `None` with a colliding row present, or two transactions with a barrier) so `MemoryCandidate.objects.create` raises `IntegrityError`, and to assert the winner reload, no propagated `IntegrityError`, and no second source row.
- round 9, findings [round 6 finding 1, round 8 finding 2], verdict superseded-by-operator-directive — per the 2026-07-19 operator directive (engram is a single-instance dogfood deployment with NO production fleet; backward compatibility and rolling-deploy choreography are NON-GOALS; deployment is stop-the-world — stop all services, migrate, start all on new code; in-progress work may be dropped at zero cost), the rolling-deploy blocker fixes are removed as out of scope. **Round 6 finding 1** (the default-off `ENGRAM_MEMORY_PROPOSE_ENABLED` write feature flag + 503 `propose_disabled` + 4-step staged deploy ordering): removed entirely — the view pre-check, the error-table row, the Test Plan §5 flag-off case, and the Endpoint-section flag note; the endpoint is always on once deployed. **Round 8 finding 2** (the pre-activation-only clean-rollback window keyed on the flag flip + fix-forward-after-activation choreography): the flag-gated deployment-ordering prose is replaced by a short stop-the-world Deployment note. The two genuine-correctness facts that fix produced are KEPT: the migration reverse guard (refuses reverse while `agent_proposal` rows exist — round 5 finding 9, untouched) and the one-line rollback story (clean reverse only before the first agent proposal; fix-forward after). No steady-state correctness fix (idempotency, lost-work reconciliation, reverse guard, redaction, capability handling, curation-path agent branches) was touched. All prior reconciliation entries are left verbatim.
- round 10, finding 1 (blocker), verdict fixed — CONFIRMED: a team-bound bearer key retains `scope.team_ids=(key.team_id,)` even when the request omits `team_id` (`access/services.py:351-355`, `access_scope_tests.py:203`); taking visibility straight from the request field (`TEAM iff team_id`) let a team-confined key omit `team_id` and mint a PROJECT-global memory readable by every team scope (`context/services.py:280`, shortlist `_scope_q` TEAM branch reads PROJECT). Added a `team_bound: bool` on `EffectiveScope` (`bool(key.team_id)` bearer, `False` session) and a Data-Flow step 0 that derives the effective team from the scope: a `team_bound` caller is always confined to its bound team (TEAM visibility), only non-bound callers may create PROJECT proposals. View test added.
- round 10, finding 2 (blocker), verdict fixed — CONFIRMED: `test_import_only_candidate_is_rejected_by_non_promotion_candidate_transition` (`transitions_tests.py:364-453`) requires import candidates to raise `'provenance'` on merge/revise/supersede/open/resolve; the round-5 §4 design "derive the single allowed kind from the candidate's own source set" would make an import candidate fence against `IMPORT` on those paths and pass, regressing the test and removing the import-only-promotion boundary. Replaced the single-kind derivation with a **per-transition allowed-kind set**: promotion `{DISTILLATION, IMPORT, AGENT_PROPOSAL}`, the five non-promotion transitions `{DISTILLATION, AGENT_PROPOSAL}` (import excluded, agent admitted). Curation-path §4 and the transition-tests plan updated with an import-exclusion regression + agent-acceptance assertion.
- round 10, finding 3 (blocker), verdict fixed — CONFIRMED: the shortlist for a TEAM candidate includes PROJECT-global targets (`curation_shortlist._scope_q:73-77`), but the transition state machine requires exact team equality (`_scope_matches:281-285`, `_locked_memory_map:690`); a judge that selects a PROJECT target for a mutation outcome (merge/open_conflict/revise/supersede) makes the transition raise a non-retryable `'scope'` error → deterministic stuck work (and a team candidate mutating project-global memory is itself a cross-team escalation). Added a cross-visibility guard in `_apply_evidence_policy`: the four mutation outcomes require the target entry's `(visibility_scope, team_id)` to equal the candidate's effective pair (entries carry both, `curation_shortlist.py:42-43`); a PROJECT target for a TEAM candidate is limited to `publish_new`/`reject_candidate`-`redundant`, which never lock/mutate the target. Judge test plan updated. Strictly improves the pre-existing TEAM-distillation path.
- round 10, finding 4 (blocker), verdict fixed — CONFIRMED steady-state lost work (not deploy-window; operator directive keeps lost-work reconciliation in scope): a claim moves candidate-decision work to `LEASED`/`RUNNING` with `lease_expires_at` (`work_execution.py:340-365`); if the worker dies the lease expires but `_repair_candidate` never revives it (`_requeue_eligible:244-252` covers only READY/due-RETRY_WAIT; the run lookup bails on any RUNNING run; `_classify:109-129` does not flag lease expiry), leaving the proposal PROPOSED forever — unlike the session reconciler's `LEASE_EXPIRED`→`reclaim_via_claim_work` (`session_work_reconciler.py:182-190,44`). Extended Curation-path §6 with an expired-lease reclaim: re-signal via `queue_work_attempt` so `claim_work._handle_leased_state` (`work_execution.py:434-461`) fails the lost run and re-claims; live leases untouched. Test Plan §4 adds the expired-lease case.
- round 10, finding 5 (blocker), verdict fixed — CONFIRMED spec inaccuracy: §5 claimed visibility is re-derived and re-checked by `_revalidated_effective_scope` during conflict resolution, but `_validate_conflict_resolution_rows` (`transitions.py:2560-2607`) never calls it and `_create_candidate_memory` (`:1034-1082`) publishes with the stored `candidate.visibility_scope` (`:1045`) — only promote (`:1432`) and supersede (`:2130`) re-derive. Corrected §5 to describe the actual behavior. No live security impact: grep confirms **no** in-slice (or existing) endpoint mutates a proposed candidate's `visibility_scope`, and distillation candidates flow through the identical merge/conflict-resolution paths with identical stored-visibility behavior today — agent proposals are strict parity, not a new exposure.
- round 10, finding 6 (major), verdict fixed — CONFIRMED defense gap: `candidate.source_observation` is a mutable FK whose model scope validation checks org/project but not team (`core/models.py:171`), it is absent from the content-hash fence, and promotion copies it into durable provenance (`transitions.py:1073,1471`). No in-slice path mutates it, but as cheap defense-in-depth the agent branch of `_candidate_fence`/`validated_agent_candidate_source` now asserts `candidate.source_observation_id is None` (non-retryable `'provenance'`), so an agent candidate can never carry a cross-team observation. Documented in Curation-path §5.
- round 10, finding 7 (major), verdict fixed — CONFIRMED race in the round-8 §6 re-signal: `ensure_candidate_decision_work_locked`→`create_work`→`_create_non_digest_work` uses `get_or_create` **without** `select_for_update` (`workflow_work.py:870`), and the run read at `:282` is unlocked, so a worker's `claim_work` can claim the stale `QUEUED` run between the reconciler's classification and `queue_work_attempt`, making `_eligible_queued_run` create a second run against already-claimed work; the session reconciler avoids this by locking work+runs before classifying (`session_work_reconciler.py:389`). Added step (a′) to §6: re-select the work and its v1 runs with `select_for_update()` before classification.
- round 10, finding 8 (major), verdict refuted:very rare edge case with no impact — the "reuse trusts a stale content hash after identity fields changed" wedge requires a mutation of `candidate.title`/`body`/`kind` (or a `content_hash` desync) on a proposed candidate; grep confirms **no** code path mutates those fields post-creation (no candidate-update view exists), and `content_hash` is written once at creation and never recomputed on save. Even under a hypothetical mutation the settlement fence recomputes from the current fields and **fails closed** (`stale_decision`) — it never publishes wrong content — so the described permanent wedge is unreachable in steady state and would degrade to a fail-closed error, not corruption. Adding a reuse-time semantic recompute would harden a non-existent threat; declined. (The related mutation-class concerns that DO warrant cheap guards — visibility claim accuracy and source_observation — are fixed in findings 5 and 6.)
- round 10, finding 9 (major), verdict fixed — CONFIRMED spec inaccuracy: `work_execution.py:807-824` moves retryable failures to `RETRY_WAIT` with a backoff-capped delay (`work_failures.py:163-170` caps the *delay*, not the *count*), only `CONFIGURATION`→`BLOCKED` and `INVALID_INPUT`→`TERMINAL_FAILURE` are terminal, and the reconciler re-queues due `RETRY_WAIT` indefinitely — there is **no finite attempt budget**. Corrected the Design tier bullet's "retried until the attempt budget is exhausted, then permanently stuck" to "retries forever, consuming unbounded provider/judge calls," which strengthens (not weakens) the rationale for the `None` agent evidence time that avoids the deterministic-denial loop entirely.
- round 10, finding 10 (minor), verdict fixed — CONFIRMED: `scripts/e2e_golden_path.py:386` asserts exactly six MCP tools and is run by the Compose E2E workflow (`.github/workflows/compose-e2e.yml:27`), a separate CI gate omitted from the round-7 tool-count enumeration; registering the seventh tool without editing it deterministically reds Compose E2E. Added it (6→7) to the "assertions to update" list.
- round 10, finding 11 (minor), verdict fixed — CONFIRMED: `validated_agent_candidate_source` (Curation-path §1) validated the scope tuple but not `source.candidate_id == candidate.id`, so a foreign candidate's agent source sharing the same `(org, project, team)` could satisfy the manifest validator and alter the work fingerprint/provenance; the import validator enforces this exact ownership check (`import_provenance.py:67-68`). Added the `source.candidate_id == candidate.id` check to `validated_agent_candidate_source` (candidate-manifest path only — the target-traversal `_eligible_group_hash` legitimately has no owning candidate).
- round 11, finding N/A, verdict refuted:false positive — the adversarial review returned `AIRTIGHT` with zero numbered findings, so there is nothing to verify against code, fix, or refute. Spec left unchanged (Review Reconciliation entry only).
- round 11, finding 1 (blocker), verdict refuted:false positive — CONFIRMED the mechanics (`_user_capability_codes:270-294` aggregates org+project-grant+team roles; `_user_project_ids:297-321` grants a team's linked projects, so a session user's `memories:propose` + project access can be purely team-derived), but the write it enables is NOT an escalation: the same team-derived participant already reads **every** PROJECT-global memory in that project (`context/services.py:280`), so publishing project-global knowledge (readable by other teams) is the write-side of the identical, already-granted participation scope — no data exposure beyond their existing reads. The system's explicit confinement mechanism is a team-**bound bearer key** (`key.team_id`, honored by the round-10 finding-1 fix), which a session participant does not carry; every proposal is authenticated, actor-audited (`MemoryProposed`), and curation-gated. Confining session **writes** by per-role grant provenance while leaving project-global **reads** unconfined would be an inconsistent asymmetry. Kept `team_bound=False` for sessions and added the read/write-symmetry rationale to Data-Flow step 0 so the choice is defensible on the record.
- round 11, finding 2 (blocker), verdict refuted:false positive — CONFIRMED `(revise_memory, candidate_revises)`/`(supersede_memory, candidate_supersedes)` are the only mappings and both require `corroborated` (`curation_judge.py:144-153,398,409`), so a lone `supported` candidate cannot revise/supersede — but this is **not introduced by this slice** and does **not** strand a valid revision. It is **identical** to a single-observation distillation candidate, which is also tier `supported` (`_claim_evidence:169-170`) and equally blocked; the corroboration requirement is the intended single-source invariant. A supported proposal that genuinely updates a **same-visibility** fact settles via the reachable `open_conflict` (kept reachable by the `None` evidence time), routing to human review for the actual revise/supersede — so it never `judge_policy_denied`-loops. Added a clarifying Design tier note; the cross-visibility variant is handled by finding 3.
- round 11, finding 3 (blocker), verdict fixed — CONFIRMED a real dead-end in the spec's own round-10 finding-3 cross-visibility guard: `mutually_incompatible` maps only to `open_conflict` (`curation_judge.py:152`), the guard denies it for a PROJECT target, and `publish_new` is separately blocked by the targetless-identity guard (`parse_curation_judge_verdict:471-474`), so a genuine TEAM-vs-PROJECT contradiction had no reachable verdict; worse, the denial was the **retryable** `judge_policy_denied` (`curation.py:1290`) with no finite budget → infinite provider/judge loop (a regression vs the pre-guard non-retryable transition crash). The earlier "publish_new and reject_candidate/redundant remain valid" claim was wrong for the contradiction case. Fixed: (a) the guard now raises a **non-retryable** `INVALID_INPUT`-class failure (`work_execution.py:814`→`TERMINAL_FAILURE`) so residual mutation-insistence terminates instead of looping; (b) relaxed the `:471` guard to ignore identity-relation comparisons against **cross-visibility** targets so `publish_new` (TEAM-local) is the clean reachable settlement for a `mutually_incompatible` PROJECT target; (c) duplicates still settle via `reject_candidate`/`redundant` (unguarded). Corrected Curation-path §3 and the judge test plan (§3).
- round 11, finding 4 (blocker), verdict fixed — CONFIRMED: the wizard issues an **unbound** key (`console/views/api_keys.py:157` → `issue_api_key` with no project/team; serializer project/team are read-only `console/serializers/api_keys.py:58-60`), and for an unbound key `_project_ids` (`access/services.py:293`) returns `None` (403 `project_scope_denied`) unless the **effective** (`owner ∩ key`, `services.py:126`) caps carry `projects:*`/`policy:admin`/`projects:agent`. The proposed wizard set (`memories:propose` only) has none, so the reissued key 403s on every propose. Fixed: add **both** `memories:propose` **and** `projects:agent` to `WIZARD_API_KEY_CAPABILITIES` (grantable — seeded on org-owner/admin roles `migrations/0007`, so `_issuer_can_grant` permits it and the intersection includes it), mirroring the golden-path agent key (`AGENT_KEY_CAPABILITIES`); updated the remediation hint, the issued-key capability test assertions, and added a wizard-key-resolves-a-project test.
- round 11, finding 5 (major), verdict fixed — CONFIRMED steady-state lost work (config fixed while running; kept in scope by the operator directive): a `CONFIGURATION` judge/model failure moves candidate-decision work to `BLOCKED` (`work_execution.py:810-813`), but `_repair_candidate`/`_requeue_eligible` (`candidate_work_reconciler.py:244-252`) match only `READY`/due-`RETRY_WAIT`, and `claim_work._short_circuit_state` (`:423-431`) clears a changed fingerprint only on a delivery that never comes — so a config-fixed agent (or distillation) proposal stays `PROPOSED` forever, unlike the session reconciler's `CONFIGURATION_CHANGED`→`clear_block_and_queue` (`session_work_reconciler.py:131-135,354-367,399-402`). Extended Curation-path §6 (d): when the locked work is `BLOCKED` and its fingerprint no longer matches the current config, clear the block (`READY`, reset streak/fingerprint) and re-signal via `queue_work_attempt`; an unchanged fingerprint is left blocked. Added the config-changed reconciler test (§4e).
- round 11, finding 6 (major), verdict refuted:out of scope for v1 — CONFIRMED the mechanics: `_settle_model_rejection` (`curation.py:1340-1385`) locks work+candidate but not the target, and the orchestrator branches to it (`:1300-1313`) before the non-reject `_revalidate_shortlist` (`:1321`), so a `reject_candidate`/`redundant` can settle against a target revised/archived/refuted in the window between the judge call (`:1289`) and the rejection txn (`:1354`). But this is a **pre-existing** property of the rejection path **shared identically with distillation** redundant-rejections (not introduced by this slice), the race window is a single task execution, and the outcome **fails closed** (rejects a valid proposal; never publishes or corrupts memory) and stays traceable + human-recoverable. Adding target locking/revalidation to the shared reject path is a distillation-wide change; added an explicit Out-of-Scope bullet documenting it as an accepted pre-existing limitation for v1.
- round 11, finding 7 (major), verdict fixed — CONFIRMED: `EffectiveScope` is constructed at **five** sites (`access/services.py:206`, `request_scope.py:94`, `auth_services.py:201`, `auth_services.py:257`, `curation.py:972`) but the round-10 draft named only the bearer + rebuilt-session sites; a **required** `team_bound` (mirroring non-defaulted `project_bound`) would `TypeError` at the three unedited session/curator constructors. Fixed: declare `team_bound: bool = False` as a **defaulted** field placed last, so only the bearer path sets it explicitly (`bool(key.team_id)`) and the session/curator constructors inherit the safe `False` default with no edit. Updated the Data-Flow step-0 EffectiveScope bullet and its dataclass test.
- round 11, finding 8 (minor), verdict fixed — CONFIRMED: the shared contract test `_candidate_source_model_contract` (`core/core_models_tests.py:1709-1719`) asserts `source_kind.choices == {'distillation','import'}` at `:1719` and is run by the declared `engram/core` suite, so adding `AGENT_PROPOSAL` reds it unless edited. Added an explicit "update `core_models_tests.py:1719` to `{'distillation','import','agent_proposal'}`" instruction to Test Plan §1 (noting no `observation` null-assertion exists there, so the nullable change needs no edit at that site).
- round 11, finding 9 (minor), verdict fixed — CONFIRMED: the canonical CLI command omitted an env-file override and a unique `-p`. The `api` service inherits `env_file: - ${ENGRAM_ENV_FILE:-.env}` (`deploy/compose/docker-compose.yml:10-11`) and `.env` is git-ignored (`.gitignore:5`), so a fresh worktree (only tracked `.env.example`) makes `docker compose` abort before Python; and the repo requires a unique per-worktree `-p`. Fixed the command to set an absolute `ENGRAM_ENV_FILE` and `-p engram-s4-cli`. **NOTE (superseded by round 12, finding 2):** this round-11 edit wrongly pointed at `$PWD/.env.example` claiming a repo-root tracking that does not exist; the only tracked template is `deploy/compose/.env.example`. Corrected in round 12.
- round 12, finding 1 (blocker), verdict fixed — CONFIRMED a real gap: the spec asserted the cross-visibility guard in `_apply_evidence_policy` "raises the `INVALID_INPUT`-class failure (`work_execution.py:814`→`TERMINAL_FAILURE`)" but never specified how. `_apply_evidence_policy` can only raise `CurationJudgeError`, and its sole policy-denial code `judge_policy_denied` (`curation_judge.py:395,406,434`) maps to `PROVIDER_TRANSIENT` (retryable) at `work_failures.py:52`; an ad-hoc unmapped code falls through `_classify_transition` (`work_failures.py:116-121`) to `UNEXPECTED`, also retryable. Verified the propagation: guard `CurationJudgeError` → caught `curation.py:1290` → `_operational(getattr(error,'code',...))` preserves the code on a `MemoryTransitionError`; the `retryable=True` flag is inert because `translate_failure`/`_classify_transition`/`_apply_failure_work` route solely on `error.code`→`failure_class` (`work_failures.py:116-134`, `work_execution.py:810-817`), never on `.retryable`. So implementing the guard as named still loops forever. Fixed by specifying (a) the guard raises the **distinct** code `judge_cross_visibility_denied` (not `judge_policy_denied`), and (b) adding `'judge_cross_visibility_denied': (INVALID_INPUT, ...)` to `_CURATION_TRANSITION_CODE_MAP` (`work_failures.py:46-62`); updated the judge section mechanism block and the §3 test plan (assert the code + a `work_failures_tests.py` classification assertion).
- round 12, finding 2 (minor), verdict fixed — CONFIRMED: the only tracked env template is `deploy/compose/.env.example` (`git ls-files` shows no repo-root `.env.example`), so the round-11 command `ENGRAM_ENV_FILE="$PWD/.env.example"` points at a missing file and `docker compose` aborts at parse time (exit 1) in a fresh worktree. Fixed the command to `$PWD/deploy/compose/.env.example`, corrected the surrounding prose and the parenthetical that wrongly claimed repo-root tracking, and annotated the now-superseded round-11 disposition.
- round 13, finding 1 (minor), verdict fixed — CONFIRMED: the three-way `core_candidate_source_shape_ck` re-asserts `observation__isnull=False` on **both** the distillation and import branches (spec `:336,343`), but Test Plan §1 exercised the regression only for a distillation `observation=NULL` row; an implementation could drop the clause from the import branch alone and still pass every listed case, admitting a null-observation import row that crashes the `source.observation` dereference at `import_provenance.py:78` (verified: `:78-80` unconditionally reads `source.observation` and errors only on a wholly-missing relation, not on a constraint-permitted null). Added the equivalent import-source/null-observation regression assertion to Test Plan §1.
- round 13, finding 2 (minor), verdict fixed — CONFIRMED: shipping `engram_memory_propose` makes the "six tools ship" contract stale in four docs — `docs/mcp-tools.md:27` ("Six tools ship") + `:64-67` (explicitly lists `memory.propose` as **Deferred**), `docs/guides/mcp.md:164`, `packages/claude-plugin/README.md:118`, `packages/codex-plugin/README.md:40` — so users would be told the newly shipped tool is unavailable. The round-7 "Client change surface" enumerated code sites but omitted these doc/plugin contracts. Added a "MCP / plugin docs" bullet to the Client change surface directing the slice to bump the counts, add the tool to each shipped-tool list, and remove the `memory.propose` Deferred entry.
- round 14, finding 1 (blocker), verdict fixed — CONFIRMED a real provider-path gap: the design relaxed only the `:471` *parser* guard, but the provider prompt carries no decision rule — `_CURATION_DECISION_SCHEMA_INSTRUCTIONS` (`services.py:1209-1226`) defines JSON shape only and `build_curation_judge_prompt` (`curation_judge.py:512-559`) serializes scope/comparisons with no steering. The honest relation for a genuine contradiction, `mutually_incompatible`, maps solely to `open_conflict` (`_ALLOWED_COMBINATIONS`, `curation_judge.py:152`), so a competent provider handed a cross-visibility PROJECT target naturally emits `open_conflict` → the new `judge_cross_visibility_denied` mutation guard terminal-fails it; `publish_new` was never the reachable verdict on the provider path, and a direct-parser test would mask this. Fixed by (a) requiring the slice to add an explicit least-authority cross-visibility decision rule to `_CURATION_DECISION_SCHEMA_INSTRUCTIONS` (mutate-forbidden target → emit `publish_new`/`target=null` with the honest relation still in `comparisons`, which the `:471` relaxation permits), making `publish_new` the verdict a compliant provider actually produces, with the mutation guard demoted to a safety net for a non-compliant provider; and (b) adding a §3 provider-path test (test d) that drives `JudgeCurationCandidate.execute` end-to-end through the real prompt+parser pipeline, asserting the rule is present in the emitted prompt, that a rule-compliant `publish_new` completion settles TEAM-local, and that a rule-ignoring `open_conflict` completion lands on the non-retryable `judge_cross_visibility_denied` terminal.
- round 15, finding 1 (blocker), verdict fixed — CONFIRMED the round-14 decision rule was incomplete on two verified counts: (1) **multi-target precedence.** A TEAM shortlist may hold both PROJECT and same-TEAM entries (`_scope_q`, `curation_shortlist.py:69-78`) and a comparison for **every** entry is mandatory (`_validate_comparisons:330`); the `:471` relaxation ignores only *cross-visibility* identity comparisons, so a same-TEAM `equivalent` comparison co-occurring with a `mutually_incompatible` PROJECT target would still trip `:471` under a targetless `publish_new`. (2) **top-level verdict tuple.** `_ALLOWED_COMBINATIONS` (`curation_judge.py:144-153`) admits `publish_new` **only** with `unrelated`/`compatible_distinct`; the round-14 rule left the top-level `relation` unspecified, so a provider setting it to the honest `mutually_incompatible` fails `(outcome, relation)` membership → retryable `judge_invalid_output` (`work_failures.py:50`), burning the attempt budget. Fixed by (a) adding a **same-visibility-precedence** clause: the cross-visibility `publish_new` fallback is chosen **only** when no same-visibility target carries an identity relation; any same-visibility identity target is settled with its normal targeted outcome (`merge_evidence`/…/`open_conflict`, non-null target → `:471` never fires) — directly resolving the reviewer's closest-PROJECT-`mutually_incompatible` + same-TEAM-`equivalent` counterexample as a `merge_evidence` against the same-TEAM target; and (b) fixing the exact fallback tuple to `outcome=publish_new`, `relation=compatible_distinct`, `reason_code=distinct_claim`, `target=null`, with the honest `mutually_incompatible` carried **only** in the per-target `comparisons` entry. Updated the Curation-path §3 decision-rule block and added §3 tests (c) exact-tuple assertion and (c2) same-visibility precedence.
- round 16, finding 1 (major), verdict fixed — CONFIRMED spec inaccuracy self-contradicting round-10 finding 9. §3 claimed a non-compliant `publish_new`-with-wrong-`relation` verdict is "caught by the ordinary bounded `judge_invalid_output` retry budget … also not an infinite loop." No such budget exists: `judge_invalid_output` → `PROVIDER_TRANSIENT` (`work_failures.py:50`) → `RETRY_WAIT` with a backoff-**capped delay** only (`work_execution.py:818-824`; `work_failures.py:163-170` caps the *delay*, ≤1800s, not the *count*); `_apply_failure_work` sets a terminal state only for `CONFIGURATION`→`BLOCKED` and `INVALID_INPUT`→`TERMINAL_FAILURE`, and `_requeue_eligible` (`candidate_work_reconciler.py:244-252`) re-queues due `RETRY_WAIT` work forever — no `failure_streak` cap anywhere on the candidate-decision path (the distillation reconciler's `_transient_max_attempts` is a *different* work path). The cited "(round-10 finding 9)" is exactly the finding that established there is no budget, so the sentence was internally contradictory. Fixed by rewriting the §3 clause to describe the actual behavior — an unbounded bounded-**latency** transient retry, not a bounded-**count** one and not a tight/infinite loop — and to state explicitly that the design neither introduces this failure mode (it is the pre-existing behavior of *every* malformed judge output) nor relies on it being terminal: an invalid tuple mutates nothing (the verdict is rejected before any transition), so the candidate stays `PROPOSED` with consistent state and no lost work; a transient malformation self-heals and a *permanently* non-compliant provider is a pre-existing systemic condition (identical to one that never returns parseable judge output for any call), out of scope to solve differently from the rest of the judge path. The real state-corrupting shape — a mutation outcome against a cross-visibility target — retains its genuine non-retryable terminal (the mutation guard, `INVALID_INPUT`→`TERMINAL_FAILURE`). No design behavior changed; the false safety claim was removed.
- round 16, finding 2 (major), verdict fixed — CONFIRMED a real lock-order inversion → deadlock. The round-8/round-10-(a′) reconciler design locked the candidate first (`_repair_candidate` `select_for_update` at `:258`) and the work later (`queue_work_attempt`→`WorkflowWork.objects.select_for_update().get`, `work_dispatch.py:98`), establishing a candidate→work order; but **every** settlement transition locks work **before** candidate — promotion (`transitions.py:1391`→`lock_work_fence`, then `:1397`), merge/revise (`:1909`→`_lock_optional_work`, then `:1913`), conflict settlement (`:2378`→`_lock_optional_work`, then `:2382`), model rejection (`curation.py:1354`). A reconciler holding the candidate lock while acquiring the work lock, concurrent with a worker mid-settlement holding the work-claim lock while acquiring the candidate lock, is an ABBA cycle (Postgres aborts one side with `deadlock_detected`); the "leave a live lease alone" classification cannot avoid it because the block occurs while *acquiring* the work lock, before classification. Fixed by rewriting §6 (a)/(a′) to adopt the settlement order: resolve candidate/sources/work as an **unlocked read** (candidate identity fields are never mutated post-creation, round-10 finding 8 — read is safe and revalidated), then `select_for_update()` the work **and** its v1 runs **first**, then `select_for_update()` the candidate and **revalidate under lock** (`status=PROPOSED`, `decision_work_contract_version=1`, no unresolved `MemoryConflict`, unchanged source set), returning a no-op on any mismatch. This serializes the reconciler against both `claim_work._lock_work` and every settlement transition on one consistent work→candidate order (no cycle), and the candidate revalidation restores the correctness the up-front candidate lock provided. Added Test Plan §4 case (f): the reconciler locks work (+runs) before the candidate (no deadlock vs a concurrent settlement) and no-ops when the candidate is no longer settleable in the read→lock gap.
