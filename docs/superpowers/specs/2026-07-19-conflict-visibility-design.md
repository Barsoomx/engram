# S3 Conflict Visibility — Design Spec

Slice: S3 conflict-visibility (backend only)
Base: master @ `f58faaaa`
Scope: two backend changes — (1) fix `authorized_for_injection` for conflict-excluded
memories in inspection; (2) add a new `conflict_excluded` retrieval warning. No client change.

## Problem and Evidence

Since C5.4 (#266) fresh retrieval **hard-excludes** (at ranking time) any memory that has an
OPEN `MemoryConflict` (no `resolved_transition`). The exclusion lives in
`authorized_retrieval_documents`:

- `apps/backend/engram/context/services.py:315-322` — after the status/stale/refuted
  filter, documents are further filtered by
  `~Exists(MemoryConflict.objects.filter(memory_id=OuterRef('memory_id'), resolved_transition__isnull=True))`.
- `MemoryConflict` model: `apps/backend/engram/core/models.py:2661-2739`; the open/closed
  predicate is `resolved_transition__isnull=True` (open) — enforced structurally by the
  `core_memory_conflict_open_close_ck` check constraint at `core/models.py:2718-2732`
  (`resolved_transition IS NULL` ⇔ `resolution='' AND resolved_at IS NULL`).

Two visibility defects follow from that hard-exclusion:

**Defect 1 — inspection lies.** `authorized_for_injection` is computed at
`apps/backend/engram/inspection/views.py:243` as
`memory.status == MemoryStatus.APPROVED and not memory.stale and not memory.refuted`.
It ignores open conflicts. So for an APPROVED, non-stale, non-refuted memory that has an
open `MemoryConflict`, inspection reports `authorized_for_injection = true` even though a
fresh retrieval will **never** rank or inject it (idempotent replay of an already-persisted
bundle is a distinct, pre-existing snapshot path — see Error Handling → Replay snapshot).
This flows through both the list path
(`memory_response(..., include_detail=False)` at `inspection/views.py:91`, built from
`ListInspectionMemories().execute` → `_base_queryset`, `inspection/services.py:63-74,182-200`)
and the detail path (`inspection/views.py:118`, `ListInspectionMemories().detail`,
`inspection/services.py:82-88`).

**Defect 2 — retrieval is silent about the exclusion.** `compute_retrieval_warnings`
(`apps/backend/engram/context/retrieval_warnings.py:180-217`) emits `stale_match` /
`refuted_match` for would-have-matched-but-excluded stale/refuted memories
(`stale_and_refuted_warnings`, `retrieval_warnings.py:66-120`), but there is **no** warning
for memories excluded solely by an open conflict. Both retrieval entrypoints call
`compute_retrieval_warnings`:
- context: `apps/backend/engram/context/services.py:927-939` (`BuildContextBundle`).
- search: `apps/backend/engram/search/services.py:160-173` (`SearchMemories`).
So an agent whose query matches a conflicted memory gets zero signal that a relevant memory
was withheld.

**Scope of the signal (deliberate parity, not full coverage).** This slice restores a signal
for the **same class of matches the existing stale/refuted warning already covers** — query
matches scoring ≥ `MIN_SCORE = 60` (symbol/exact-term/exact matches, `score_retrieval_document`
`context/services.py:370-387`). It does **not** warn for weaker matches: full-text-only (score
40, `:389-396`), semantic (score 30, `_semantic_retrieval_matches_python`/pgvector
`:437-470,473-...`), or lexical-recall (score 20). A memory whose query relevance is only
full-text/semantic/lexical remains conflict-excluded from ranking **and** below the warning
gate, so it stays silent — exactly as a stale/refuted memory at those scores does today
(`stale_and_refuted_warnings` uses the same `STALE_REFUTED_MIN_SCORE = 60`,
`retrieval_warnings.py:38,98`). Lowering `CONFLICT_EXCLUDED_MIN_SCORE` to surface weaker matches
is out of scope: it would diverge from the stale/refuted warning shape (Decision 4 parity) and is
a cross-cutting change to *all* retrieval warnings, not conflict-specific. The "zero signal"
defect is closed for score-≥60 matches (the strong, high-confidence relevance band); weaker-match
visibility is a known, parity-matched limitation, tracked in Out of Scope.

Note — distinct from the existing `conflicting_memory` warning
(`retrieval_warnings.py:134-177`): that warning flags an **included** memory whose candidate
carries an unresolved `CONFLICTS_WITH` contradiction claim (soft signal, still injected).
The new `conflict_excluded` warning flags a memory that was **excluded** from retrieval by an
open relational `MemoryConflict` (hard exclusion). The two are not merged.

Conflict resolution stays **console-only by design** (`If-Match` ETag +
`memories:admin`): `apps/backend/engram/console/views/memory_review.py:38-122`
(`RequireCapability('memories:admin')` at :51; `If-Match` precondition at :117-122;
`conflict_set_etag` at :96,:122). The agent story this slice enables is:
agent sees `conflict_excluded` warning → `engram_memory_get` (S2) shows the memory with a
conflict signal → agent **escalates to a human operator**, who resolves in the console.
Agents never resolve conflicts themselves.

## Design

**Decision 1 — extract the open-conflict predicate to one shared helper.** The predicate is
code-owned (commit `564f1d6d`) and is about to have three call sites (context retrieval
filter, retrieval-warnings query, inspection annotation). Create
`apps/backend/engram/memory/conflict_predicate.py` exposing `open_memory_conflict_exists()`
returning a Django `Exists` subquery parameterized by the outer-ref field. It imports only
`engram.core.models` — no cycle: `context`, `search`, and `inspection` already import from
`engram.memory` (`context/services.py:52-54`, `inspection/services.py:26`,
`retrieval_warnings.py:21`), and this module imports nothing from them.
- _Rejected_: duplicate the `Exists(...)` literal at each site — drifts, defeats the point.
- _Rejected_: put the helper in `context/services.py` — inspection importing context risks a
  cycle and inverts the dependency direction (context already depends on memory, not vice-versa
  at this layer).

**Decision 2 — fix `authorized_for_injection` via a queryset annotation, not a per-row query.**
Annotate `has_open_conflict` on `ListInspectionMemories._base_queryset`
(`inspection/services.py:182-200`) with `open_memory_conflict_exists('pk')`. Both list and
detail read through `_base_queryset` (`execute` at `inspection/services.py:64-74`, `detail` at
`:82-88`), so one annotation fixes both; `count()` (`inspection/services.py:90-102`) does not
render rows and is left untouched. `memory_response` reads the annotation directly as
`memory.has_open_conflict` — the annotation is a **required precondition** of calling
`memory_response`, and both (and only) its callers source `memory` from `_base_queryset`
(`inspection/views.py:91,118`).
- _Rejected_: recompute per memory in `memory_response` with a `.exists()` call — N+1 on the
  list endpoint.
- _Rejected_: `getattr(memory, 'has_open_conflict', False)` default-false fallback — this is
  **fail-open for the exact defect being fixed**: an un-annotated, approved, conflicted memory
  would report `has_open_conflict=false` → `authorized_for_injection=true`, silently
  re-introducing Defect 1. A missing annotation is a programming error in a new caller, so it
  must fail loud (`AttributeError`), never default to the unsafe value. Direct attribute access
  is fail-closed; the annotation being required is enforced by the tests below.

**Decision 3 — expose `has_open_conflict` in the inspection memory response.** It is already
computed for `authorized_for_injection` and is the observable *reason* injection is denied — so
the field stands on its own for **any** inspection consumer (console, tests, ops) independent of
S2. It is also the seam that S2 (sibling slice, `engram_memory_get`, not yet merged) will use to
render a conflict signal (`status=conflict` in the agent story); S2's existence is **not** a
precondition for this field. Adding it now is a one-line, non-speculative contract extension.
- _Rejected_: expose nothing and let S2 recompute — S2 reads inspection detail; without this
  field it cannot distinguish "conflict-excluded" from other unauthorized states.

**Decision 4 — new `conflict_excluded` warning mirrors `stale_and_refuted_warnings` exactly**
(same query→score→cap→dedupe shape, same `[:200]` prefetch cap, same team-visibility filter,
same min-score gate). It selects documents that are otherwise fully injectable
(status APPROVED, memory + doc not stale, not refuted) **and** have an open conflict, then
scores them against the query. Because the selection requires not-stale/not-refuted, a memory
that is both stale and conflicted is **never** surfaced here — `stale_match`/`refuted_match`
wins by construction (see Error Handling → precedence).
- _Rejected_: reuse the `conflicting_memory` (`included_matches`) mechanism — that path only
  sees included memories; conflict-excluded memories are, by definition, not in `included_matches`.
- _Rejected_: emit for stale-and-conflicted too — double warnings for one memory, no added
  signal; the memory is unusable for the more fundamental staleness reason.

## API and Schema Changes

No DB schema change. No migration. No new endpoint. No new capability.

### New module `apps/backend/engram/memory/conflict_predicate.py`

```python
from __future__ import annotations

from django.db.models import Exists, OuterRef

from engram.core.models import MemoryConflict


def open_memory_conflict_exists(memory_ref: str = 'memory_id') -> Exists:
    return Exists(
        MemoryConflict.objects.filter(
            memory_id=OuterRef(memory_ref),
            resolved_transition__isnull=True,
        ),
    )
```

### `apps/backend/engram/context/services.py`

Replace the inline `~Exists(...)` block (`:315-322`) with:

```python
documents = documents.filter(~open_memory_conflict_exists('memory_id'))
```

Add `from engram.memory.conflict_predicate import open_memory_conflict_exists`.
Remove now-unused imports `MemoryConflict` (`:36`), and `Exists`, `OuterRef` from the
`django.db.models` import at `:13` (keep `Q` — still used). Verify no other references remain
(grep confirmed: `Exists`/`OuterRef`/`MemoryConflict` in `context/services.py` occur only at
`:13`, `:36`, `:316-318`). Behavior is byte-identical; this is a pure extraction.

### `apps/backend/engram/inspection/services.py`

In `ListInspectionMemories._base_queryset` (`:182-200`) add:

```python
.annotate(has_open_conflict=open_memory_conflict_exists('pk'))
```

Import `from engram.memory.conflict_predicate import open_memory_conflict_exists`.

### `apps/backend/engram/inspection/views.py`

In `memory_response` (`:231-290`), replace `:243` with:

```python
has_open_conflict = bool(memory.has_open_conflict)
authorized_for_injection = (
    memory.status == MemoryStatus.APPROVED
    and not memory.stale
    and not memory.refuted
    and not has_open_conflict
)
```

Add `'has_open_conflict': has_open_conflict,` to the response dict (near `:260`, alongside
`authorized_for_injection`). Present on both list and detail responses (same function).

Response shape delta (memory item, list and detail):

```json
{
  "authorized_for_injection": false,
  "has_open_conflict": true
}
```

`memory.has_open_conflict` is read **directly** (no `getattr` default) so a caller that forgets
the annotation fails loud with `AttributeError` rather than silently reporting the unsafe
`authorized_for_injection=true` (fail-closed, see Decision 2 rejected alternative). The annotation
lives only on `ListInspectionMemories._base_queryset`; both `memory_response` callers source their
rows there (`inspection/views.py:91,118`), so the precondition holds today. (The related-memory
block at `inspection/views.py:277-286` builds its dicts inline and does **not** call
`memory_response`, so it is unaffected by this precondition.)

Inspection's `authorized_for_injection` is a **display/report** field; the actual hard-exclusion is
enforced independently in `authorized_retrieval_documents` (`context/services.py:315`). Fail-closed
here means a forgotten annotation surfaces as a loud error in the inspection endpoint, never as a
memory that inspection labels injectable while retrieval silently withholds it.

### `apps/backend/engram/context/retrieval_warnings.py`

Add constants beside the stale/refuted ones (`:37-39`):

```python
CONFLICT_EXCLUDED_WARNING_CAP = 3
CONFLICT_EXCLUDED_MIN_SCORE = 60
```

Add function (mirrors `stale_and_refuted_warnings`, `:66-120`):

```python
def conflict_excluded_warnings(
    organization: Organization,
    project: Project,
    scope: EffectiveScope,
    query: str,
    file_paths: tuple[str, ...],
    symbols: tuple[str, ...],
    has_request_terms: bool,
    kinds: tuple[str, ...] = (),
) -> list[RetrievalWarning]:
    from engram.context.services import filter_documents_by_team_visibility, redact_text, score_retrieval_document
    from engram.memory.conflict_predicate import open_memory_conflict_exists

    if not has_request_terms:
        return []

    documents = RetrievalDocument.objects.filter(
        organization=organization,
        project=project,
        memory__status=MemoryStatus.APPROVED,
        memory__stale=False,
        memory__refuted=False,
        stale=False,
        refuted=False,
    ).filter(open_memory_conflict_exists('memory_id'))
    if kinds:
        documents = documents.filter(memory__kind__in=kinds)
    documents = documents.select_related('memory').defer(*retrieval_embedding_deferred_fields())[:200]
    authorized_documents = filter_documents_by_team_visibility(documents, scope)

    warnings: list[RetrievalWarning] = []
    seen_memory_ids: set[uuid.UUID] = set()
    for document in authorized_documents:
        if len(warnings) >= CONFLICT_EXCLUDED_WARNING_CAP:
            break
        if document.memory_id in seen_memory_ids:
            continue

        match = score_retrieval_document(document, query, file_paths, symbols, has_request_terms)
        if match is None or match.score < CONFLICT_EXCLUDED_MIN_SCORE:
            continue

        memory = document.memory
        seen_memory_ids.add(memory.id)
        warnings.append(
            RetrievalWarning(
                code='conflict_excluded',
                message=f'conflict-excluded memory matched: "{redact_text(memory.title)}"',
                memory_id=str(memory.id),
            ),
        )

    return warnings
```

Wire it into `compute_retrieval_warnings` (`:203-215`), after `stale_and_refuted_warnings`
and before `conflicting_memory_warnings`:

```python
warnings.extend(
    conflict_excluded_warnings(
        organization, project, scope, query, file_paths, symbols, has_request_terms, kinds,
    ),
)
```

Emitted warning dict (unchanged `RetrievalWarning.to_dict`, `retrieval_warnings.py:33-34`):

```json
{"code": "conflict_excluded", "message": "conflict-excluded memory matched: \"<title>\"", "memory_id": "<uuid>"}
```

Shape is exactly `{code, message, memory_id}` — the same triple `stale_match`/`refuted_match`
emit. This slice touches **no client code and adds no bundle byte-sync step**. How the warning
reaches an agent differs by path, and the two paths have different client dependencies:

- **Context path — surfaced server-side, works standalone.** `_render_context`
  (`context/services.py:1281-1307`) renders every warning's `message` into the bundle **text**
  itself (`> Warnings:` block, `:1298-1306`). Agents read the bundle text directly, so the new
  `conflict_excluded` warning is visible on the context path the moment this backend ships — no
  client change required.
- **Search path — surfaced only in the JSON `warnings` array, depends on S1.** The search API
  returns warnings in `SearchResult.to_response()['warnings']` (`search/services.py:45-51`), but
  the **current** canonical `engram_search` client renders only `items` and **discards**
  `warnings` (`packages/cli/engram_cli/mcp_tools.py:161-175`; the search rendering builds each
  line only from `items`, never reading `warnings`). Generic client rendering
  of the search `warnings` array is **S1's** scope (sibling slice, not yet merged). Until S1
  lands, `conflict_excluded` is present in the search HTTP response and asserted by this slice's
  API test, but is **not** yet shown to an agent through the search tool. This is a stated
  cross-slice dependency, not a gap this backend-only slice closes.

## Data Flow

Inspection (list + detail):
1. `MemoryInspectionListView.get` / `MemoryInspectionDetailView.get` build `InspectionScope`.
2. `ListInspectionMemories.execute`/`.detail` → `_base_queryset` now annotates
   `has_open_conflict` (single correlated `EXISTS` subquery, evaluated in the same page query).
3. `memory_response` reads the annotation → `authorized_for_injection` accounts for open
   conflicts and `has_open_conflict` is returned.

Retrieval (context + search, shared `compute_retrieval_warnings`):
1. Ranking runs against `authorized_retrieval_documents`, which excludes open-conflict memories
   (`context/services.py:315`). Conflicted matches are absent from `included_matches`.
2. `compute_retrieval_warnings` independently queries the conflict-excluded-but-scoring set via
   `conflict_excluded_warnings` and appends `conflict_excluded` warnings (capped at 3).
3. context persists them in `ContextBundle.metadata['warnings']`
   (`context/services.py:948`) and renders them into the bundle **text**
   (`_render_context`, `context/services.py:1298-1306`); search returns them in
   `SearchResult.warnings` → `to_response()['warnings']` (`search/services.py:45-51,173`).
4. Context-path warnings are already agent-visible via the rendered bundle text (server-side).
   Search-path warnings live only in the JSON `warnings` array; the current `engram_search`
   client discards it, so agent-visible search warnings depend on S1 (sibling slice). See API
   and Schema Changes → search path for the exact evidence.

## Error Handling

- **HTTP errors**: none added. Inspection auth/404 behavior is unchanged
  (`InspectionNotFoundError` → `not_found_response`, `inspection/views.py:192-196`).
- **Capability denials**: unchanged. Inspection still requires `memories:read`
  (`inspection/views.py:81`); conflict *resolution* remains `memories:admin` console-only
  (`console/views/memory_review.py:51`). No new agent-facing capability. If an agent tries to
  act on a conflict it can only escalate to an operator — stated in Design.
- **Empty results**: `conflict_excluded_warnings` returns `[]` when `has_request_terms` is
  false, when no conflicted document scores ≥ `CONFLICT_EXCLUDED_MIN_SCORE`, or when there are
  no open conflicts. `has_open_conflict` is always present on `memory_response` inputs (annotated
  on `_base_queryset`); it is read directly and has no default (fail-closed, Decision 2).
- **Replay snapshot (idempotent context)**: `BuildContextBundle.execute` returns an existing
  bundle for a repeated `request_id` **before** any conflict filtering or warning computation
  (`context/services.py:901-906`), and `_result_from_bundle` (`:1003-1022`) rehydrates the
  persisted items and `metadata['warnings']`. Semantics: **the creation-time snapshot wins for
  status/stale/refuted/conflict** — `conflict_excluded` is computed and persisted when the bundle
  is first built and is returned unchanged on replay, exactly like `stale_match`/`conflicting_memory`.
  A memory that becomes stale/refuted/conflicted **after** its bundle was built is not re-filtered
  on replay for those states. This is the pre-existing, deliberately tested idempotency contract
  (`context_api_tests.py:2160`,
  `test_session_start_replay_returns_persisted_warnings_verbatim_after_state_change`).
  **One carve-out — digest quarantine is re-checked on replay and fail-closes.**
  `_result_from_bundle` is **not** purely snapshot-based: it re-evaluates digest visibility against
  **current** state (`unproven_digest_memory_ids(digest_memories)`, `context/services.py:1018-1020`);
  if any injected digest memory is now unproven, the replay returns `digest_visibility_unproven=True`,
  and `_quarantined_response` (`:156-171`) **clears items and rendered context and replaces the
  warnings array** with `[{'code': 'context_bundle_digest_visibility_unproven'}]` — discarding the
  persisted `conflict_excluded`/`stale_match`/etc. This fail-closed replay is itself covered by an
  existing regression (`context/services_tests.py:2239-2274`). So the "creation-time snapshot wins"
  statement is true for conflict/stale/refuted replay but is **not universal**: digest quarantine
  overrides the snapshot on replay. `conflict_excluded` inherits both behaviors — persisted verbatim
  on an ordinary replay, wiped along with everything else if the bundle is digest-quarantined at
  replay time. This slice does not change either replay behavior and does not add a per-warning-code
  replay test; the two existing tests already fix both contracts. Consequently the "fresh retrieval
  never injects a conflicted memory" statement in Problem/Evidence holds for fresh ranking, not for
  snapshot replay of a bundle predating the conflict — out of scope to change (see Out of Scope).
- **Initial-build consistency / TOCTOU (accepted risk, not a defect)**: within a single fresh
  build, ranking materializes `authorized_documents` (`context/services.py:913`, which applies the
  hard conflict exclusion at `:315`) and `conflict_excluded_warnings` runs its **own** query
  afterward (`:927`), with no shared snapshot between them — they sit between two separate
  `transaction.atomic()` blocks (`:909,:950`) under READ COMMITTED, and the gap spans the
  **external embedding step** in `_rank_matches` (`:1183`), whose configured HTTP timeout is
  **30 seconds** (`ENGRAM_EMBEDDING_HTTP_TIMEOUT`, `model_policy/services.py:1507-1508`). The
  window is therefore **not** sub-second — it is bounded by that embedding call and can be seconds
  long. A conflict that opens or resolves in that window makes the pair briefly inconsistent: a
  memory ranked-in and then conflicted before `:927` is both injected **and** labeled
  `conflict_excluded`; a memory conflicted-then-resolved is withheld from ranking without a
  warning. **Correction of a prior premise:** conflicts do **not** open only via a rare console
  admin action. Automated curation opens them programmatically — `_hold_for_conflict` calls
  `OpenMemoryConflict().execute(...)` on every near-duplicate hold under the v1 decision contract
  (`memory/curation.py:851-887`, and again at `:1463-1486`); only **resolution** is console
  `memories:admin`-gated (`console/views/memory_review.py:51`). So a conflict can open during
  ordinary background candidate processing that runs concurrently with retrieval — the collision
  is realistic, not negligible. **Why it is accepted anyway:** (1) the mislabel is **strictly
  informational** — `conflict_excluded` never gates injection; the hard exclusion at `:315` is the
  sole injection authority, so a spuriously-injected-and-labeled memory is still a memory the agent
  can use, with an over-cautious warning attached; (2) this is **identical, pre-existing** behavior
  in `stale_and_refuted_warnings`, which likewise runs a separate post-ranking query with no shared
  snapshot (`retrieval_warnings.py:81-87`) across the same embedding gap — `conflict_excluded_warnings`
  mirrors it exactly (Decision 4), so it does not introduce a new inconsistency class; (3) the
  labeled-but-injected direction self-corrects on the *next* fresh build (the memory is then
  hard-excluded and correctly warned). Wrapping ranking + warnings in one REPEATABLE READ snapshot
  would diverge from the existing warning shape for no change to injection correctness (the
  authority is `:315`, not the warning); deliberately matched to the stale/refuted path, not
  tightened. This is an **explicitly accepted informational-inconsistency risk**, not a claim of
  rarity. Note the persisted-replay caveat below: because the built bundle (items + warnings) is
  persisted and replayed verbatim for a repeated `request_id`, an injected-and-`conflict_excluded`
  bundle is replayed unchanged (it is **not** a momentary in-memory-only mislabel) until a fresh
  `request_id` rebuilds — see Replay snapshot.
- **Precedence (stale/refuted vs conflict)**: decided and enforced structurally.
  `conflict_excluded_warnings` selects only `memory__stale=False, memory__refuted=False,
  status=APPROVED, doc stale=False, doc refuted=False`. A memory that is stale/refuted **and**
  conflicted is therefore excluded from this query and surfaces **only** as `stale_match` /
  `refuted_match` (`stale_and_refuted_warnings`, `retrieval_warnings.py:82,103-118`).
  `stale_match`/`refuted_match` win; no double emission for one memory.
- **Cap behavior**: at most `CONFLICT_EXCLUDED_WARNING_CAP = 3` warnings, deduped by
  `memory_id`, mirroring `STALE_REFUTED_WARNING_CAP` (`retrieval_warnings.py:37,92-94`).
- **Best-effort scope (accepted parity, not a defect)**: like `stale_and_refuted_warnings`, this
  warning query does **not** re-apply retrieval's two extra eligibility gates — digest quarantine
  (`_quarantine_unproven_digests`, `context/services.py:330,333`) and context-only
  `require_provenance` filtering (`context/services.py:1144-1147`). A memory that has an open
  conflict **and** is also quarantined-as-unproven-digest or provenance-filtered is still a
  memory with an open conflict, so `conflict_excluded` is not a false statement — the message
  says the memory matched and was conflict-excluded, never that the conflict is the *sole*
  exclusion reason. Replicating those gates here would diverge from the existing warning shape
  (Decision 4) for a narrow intersection (org-gated provenance ∩ unproven digest ∩ open
  conflict ∩ matching query) with no added agent signal. Deliberately matched to
  `stale_and_refuted_warnings`; not tightened.
- **`[:200]` prefetch cap ordering (accepted parity)**: the `[:200]` cap is applied on the raw
  eligible set — **before both** `filter_documents_by_team_visibility` **and** per-document
  scoring — identical to `stale_and_refuted_warnings` (`retrieval_warnings.py:81,86,97`). The
  slice therefore truncates by whatever default row order the eligible-conflict queryset yields,
  not by query relevance or team visibility. So it is enough to have **>200 eligible conflicted
  documents** in one project (regardless of whether they match the query or are team-visible):
  200 unrelated or unauthorized rows ahead of the cap can push the only visible, query-matching
  conflicted row past row 200 and silently drop its warning. This is pre-existing behavior shared
  by the stale/refuted path, not introduced here; with a warning cap of 3 and per-project open
  conflicts far below 200 in practice it has no real impact. Matched to the existing path; not
  changed in this slice (tightening it — e.g. ordering by score or filtering visibility before
  the cap — would diverge from the existing warning shape, Decision 4).

## Test Plan

TDD: write the failing test first in each pair, then implement. Backend tests run via the root
compose stack with a unique project name:
`docker compose -p engram-s3 run --rm app pytest -q <paths>`.
Reuse existing helpers: `open_single_conflict`
(`apps/backend/engram/memory/transitions_test_support.py:485-509`) returns a
`tuple[MemoryCandidate, MemoryConflict]`. **The conflicted APPROVED memory (with a retrieval
document) is `conflict.memory` — target it via `conflict.memory` / `conflict.memory_id`.** The
returned first element is a *separate* proposed resolution candidate, **not** the conflicted
memory; do not pass it as the inspection `memory_id`. Resolve the conflict via
`transitions.ResolveMemoryConflict` / `ResolveMemoryConflictInput` (pattern in
`apps/backend/engram/memory/candidate_ttl_tests.py:447-449`).

### 1. Inspection — `apps/backend/engram/inspection/services_tests.py` (colocated with services)

Fixtures typed, `f_`/`m_` prefixes only when passed as args; objects built inside a test carry
no prefix.

- `test_authorized_for_injection_false_when_memory_has_open_conflict`:
  `candidate, conflict = open_single_conflict(...)`, query
  `ListInspectionMemories().detail(scope, conflict.memory_id)` (the conflicted APPROVED memory,
  **not** `candidate.id`), assert `getattr(memory, 'has_open_conflict') is True` and, via
  `memory_response(memory, include_detail=True, inspection_scope=scope)`,
  `authorized_for_injection is False` and `has_open_conflict is True`.
  **(fails first: annotation/field absent.)**
- `test_authorized_for_injection_true_after_conflict_resolved`:
  open then resolve the conflict, re-fetch through `.detail`, assert
  `authorized_for_injection is True` and `has_open_conflict is False`.
- `test_list_authorized_for_injection_reflects_open_conflict`:
  one conflicted + one clean APPROVED memory in the same project; run
  `ListInspectionMemories().execute(scope)`, render each via `memory_response(..., include_detail=False)`,
  assert the conflicted item is `False`/`has_open_conflict True` and the clean item is
  `True`/`has_open_conflict False`.
- `test_memory_response_without_annotation_raises`:
  call `memory_response` on a **persisted** `Memory` fetched *outside* `_base_queryset` (so the
  `has_open_conflict` annotation is absent) — e.g. a factory-built APPROVED memory re-fetched via
  `Memory.objects.get(pk=...)`. It must still have its `project` relation so any incidental
  `memory.project.name`/`.slug` read (`inspection/views.py:248-249`) cannot raise for an unrelated
  reason and mask the assertion. Assert the call raises **`AttributeError`**
  (`pytest.raises(AttributeError)`) — the annotation read `bool(memory.has_open_conflict)` is
  direct (no default) and runs before the response dict is built, so a forgotten annotation fails
  loud. This is the fail-closed contract of Decision 2: it proves the code does **not** use a
  `getattr(memory, 'has_open_conflict', False)` fallback, which would return `False` with no
  exception and silently re-introduce Defect 1 (`authorized_for_injection=true` for a conflicted
  memory). **(fails first if the implementation adds a default-`False` fallback: the call would
  return normally and this test would fail.)**

### 2. Retrieval warnings — `apps/backend/engram/context/retrieval_warnings_tests.py` (new, colocated)

`compute_retrieval_warnings` is **keyword-only** and `included_matches` and `semantic_unavailable`
are **required** (no defaults, `retrieval_warnings.py:180-193`). Every call below must pass them
explicitly — e.g. `compute_retrieval_warnings(organization=..., project=..., scope=..., query=...,
file_paths=(), symbols=(), has_request_terms=True, included_matches=(), semantic_unavailable=False,
kinds=())`. Omitting either raises `TypeError` before any assertion runs.

- `test_conflict_excluded_warning_emitted_for_matching_open_conflict`:
  `open_single_conflict`, call `compute_retrieval_warnings(...)` with `query` = the memory
  title and `has_request_terms=True`, assert exactly one warning with
  `code == 'conflict_excluded'`, correct `memory_id`, and title in `message`. **(fails first.)**
- `test_conflict_excluded_absent_when_no_request_terms`:
  same setup, `has_request_terms=False` / blank query, assert no `conflict_excluded` warning.
- `test_conflict_excluded_absent_when_conflict_resolved`:
  resolve the conflict first, assert no `conflict_excluded` warning (memory is now injectable).
- `test_stale_wins_over_conflict_excluded`:
  memory both stale (`memory.stale = True`) and conflicted, assert a `stale_match` warning and
  **no** `conflict_excluded` warning (precedence).
- `test_refuted_wins_over_conflict_excluded`:
  memory refuted and conflicted, assert `refuted_match` and no `conflict_excluded`.
- `test_conflict_excluded_capped_at_three`:
  build **four conflicted memories in a single organization/project/team** — *not* four separate
  `open_single_conflict` calls: each call chains through `provenanced_candidate` → `_create_scope`
  (`transitions_test_support.py:46,486`) and mints a **fresh org/project per suffix**, while the
  warning query is project-scoped, so four separate scopes leave exactly one eligible conflict per
  queried project and the cap can never be reached. Reuse one scope: promote four base candidates
  with `provenanced_candidate_in_scope(organization, project, team, ...)`
  (`transitions_test_support.py:294`) whose titles all match the query, then open a conflict on
  each (`candidate_in_scope` + `OpenMemoryConflict`, mirroring `open_single_conflict`,
  `transitions_test_support.py:485-509`). Call `compute_retrieval_warnings` once and assert
  **exactly three** `conflict_excluded` warnings with **three distinct `memory_id`s** — not
  "at most three": an implementation capped at one, or returning none, must fail this test.
  **(fails first.)**
- `test_conflict_excluded_below_min_score_not_emitted`:
  build a conflicted, query-matching memory that scores **exactly 40** (full-text-only), then
  assert no `conflict_excluded` warning. The fixture must **force the score-40 branch**
  (`context/services.py:389-396`), not a no-match: a wholly unrelated query returns `None` (there
  is no filter-only fallback when `has_request_terms=True`, `context/services.py:398,406`) and is
  dropped by the `match is None` half of the gate, so it would stay green even if the
  `< CONFLICT_EXCLUDED_MIN_SCORE` half were deleted — it does not prove the min-score check. To
  hit score 40, put a distinctive prose word in the memory **body** (so it lands in
  `full_text = f'{title}\n\n{body}'`, `projections.py:80`) that is **neither the title nor an
  extracted `exact_term`** — ordinary prose is not extracted (only tickets, error classes,
  UPPER_SNAKE, and backtick phrases are, `term_extraction.py:54-64`) and the title is always an
  `exact_term` (`term_extraction.py:121-129`). Query by that body word: it misses `exact_terms`
  (no score-60 match, `context/services.py:379-387`) but is a substring of `full_text`
  (score 40, `:389-396`), so the `< 60` check is what suppresses the warning.
- `test_conflict_excluded_excludes_memory_outside_team_scope` **(data-exposure parity —
  required)**: a conflicted, query-matching APPROVED memory owned by a *different* team with
  `visibility_scope=TEAM`, requested under a scope that excludes that team. Assert **no**
  `conflict_excluded` warning **and** that the memory title/`memory_id` do **not** appear
  anywhere in the returned warnings. This is the conflict-path analogue of
  `test_session_start_stale_warning_excludes_memory_outside_team_scope`
  (`context_api_tests.py:2064`) and proves `filter_documents_by_team_visibility` (applied at
  `conflict_excluded_warnings`) actually gates the new title+UUID disclosure path. **(fails first
  if team filtering is dropped.)**
- `test_conflict_excluded_message_redacts_secret_shaped_title` **(data-exposure parity —
  required)**: a conflicted, query-matching memory whose title contains a secret-shaped token
  (matching the same redaction rule the stale/refuted path relies on via `redact_text`). Assert
  the emitted `message` is redacted (raw secret token absent), proving the new warning routes its
  title through `redact_text` identically to `stale_and_refuted_warnings`
  (`retrieval_warnings.py:107,115`).

### 3. End-to-end warning propagation — extend existing API tests

- `apps/backend/engram/search/search_api_tests.py`: assert a matching conflicted memory yields
  a `conflict_excluded` entry in the response `warnings` array (alongside the existing
  `stale_match`/`refuted_match` coverage).
- `apps/backend/engram/context/context_api_tests.py`: assert the same warning is persisted in
  `ContextBundle.metadata['warnings']` and rendered into the bundle text.

## Out of Scope

- Agent-side conflict resolution — resolution stays console-only (`memories:admin` + `If-Match`,
  `console/views/memory_review.py`).
- Any conflict-list MCP tool or CLI command.
- Any client change: no `packages/cli`, `packages/claude-plugin`, or `packages/codex-plugin`
  edits; therefore **no bundle byte-sync step**. Client rendering of the search `warnings` array
  is **S1's** scope (sibling slice); consumption of `has_open_conflict` in a read tool is
  **S2's** scope (sibling slice). Neither is a precondition for this slice: the context-path
  warning is surfaced server-side in the bundle text, and `has_open_conflict` is a
  self-justified inspection field. This slice is deliberately backend-only and does not close the
  search-path client gap.
- Changing the retrieval exclusion rule itself (C5.4 behavior is correct and preserved).
- Warning on weak-relevance conflict matches (full-text score 40, semantic 30, lexical 20).
  The `conflict_excluded` warning fires only for score-≥60 matches, matching the existing
  `stale_and_refuted_warnings` `MIN_SCORE = 60` gate (`retrieval_warnings.py:38,98`). Lowering
  the threshold is a cross-cutting change to the shared warning shape (it would have to move for
  stale/refuted too, or diverge from Decision 4 parity) and is not conflict-specific. The
  score-40 test (`test_conflict_excluded_below_min_score_not_emitted`) deliberately enshrines this
  parity boundary. See Problem/Evidence → Scope of the signal.
- Changing context-bundle idempotent-replay semantics. Replaying a persisted `request_id`
  returns the creation-time snapshot (items + `metadata['warnings']`) verbatim for the
  status/stale/refuted/conflict exclusion types (`context/services.py:901-906,1003-1022`; contract
  test `context_api_tests.py:2160`), with **one existing exception**: digest quarantine is
  re-checked against current state on replay and, when it trips, clears items and replaces the
  warnings array (`_result_from_bundle` `:1018-1020` → `_quarantined_response` `:156-171`; regression
  `context/services_tests.py:2239-2274`). `conflict_excluded` inherits both — persisted verbatim on
  an ordinary replay, and discarded along with all other warnings if the bundle is digest-quarantined
  at replay time (see Error Handling → Replay snapshot). Re-filtering replays against current conflict
  state, or extending the fail-closed replay recheck to conflicts, is a broader idempotency redesign,
  out of scope here.
- Merging or reworking the existing `conflicting_memory` (contradiction-claim) warning.
- Schema/migration changes; new capabilities; console UI changes.

## Review Reconciliation

_(append-only; empty at authoring time)_

- round 1, finding N/A, verdict refuted:false positive — adversarial reviewer (Codex) failed
  to launch (`failed to load configuration: No such file or directory (os error 2)`); zero
  findings were produced, so there is nothing to fix or refute. No spec section changed.
- round 2, finding N/A, verdict refuted:false positive — Codex companion again failed to
  launch with the same configuration error (`failed to load configuration: No such file or
  directory (os error 2)`); no review output available, zero findings. Nothing to fix or
  refute; no spec section outside Review Reconciliation changed.
- round 3, finding 1 (warning query bypasses digest-quarantine + require_provenance gates),
  verdict refuted:very-rare-edge-case — `conflict_excluded_warnings` mirrors the existing
  `stale_and_refuted_warnings`, which also skips `_quarantine_unproven_digests`
  (`context/services.py:330`) and `require_provenance` (`:1144-1147`); the warned memory always
  genuinely has an open conflict (message never claims sole reason), and the intersection is
  narrow. Documented as accepted parity in Error Handling; not tightened (would break Decision 4
  parity).
- round 3, finding 2 (`[:200]` cap applied before team-visibility filter can drop authorized
  warnings), verdict refuted:very-rare-edge-case — identical, pre-existing behavior in
  `stale_and_refuted_warnings` (`retrieval_warnings.py:86-87`); with warning cap 3 and realistic
  per-project conflict counts it has no impact. Documented in Error Handling; not introduced by
  this slice.
- round 3, finding 3 (search client discards `warnings`; spec's "S1 already renders / no client
  change" is false), verdict fixed — verified `engram_search` reads only `items`
  (`mcp_tools.py:161-166,280-285`) and drops `warnings`, while context renders warnings
  server-side into bundle text (`_render_context`, `context/services.py:1298-1306`). Reworked the
  retrieval-warnings section, Data Flow step 4, and Out of Scope to split the two paths: context
  surfaces standalone; search-path agent visibility is an explicit dependency on S1 (sibling
  slice), not something this backend-only slice closes.
- round 3, finding 4 (`engram_memory_get`/S2 not implemented, cannot consume `has_open_conflict`),
  verdict fixed — softened Decision 3 and Out of Scope to state the field is self-justified for
  any inspection consumer and that S2 (sibling slice) consumption is out of scope and not a
  precondition; removed the "S2 consumes" present-tense claim.
- round 3, finding 5 (getattr-fallback example mis-cites related-memory rendering), verdict fixed
  — `inspection/views.py:277-286` builds related dicts inline and never calls `memory_response`;
  corrected the justification to describe the fallback as purely forward-defensive with no
  current un-annotated caller.
- round 3, finding 6 (`open_single_conflict` returns `(candidate, conflict)`; conflicted memory
  is `conflict.memory`), verdict fixed — verified helper signature
  (`transitions_test_support.py:485-509`); updated the reuse paragraph and the first inspection
  test to target `conflict.memory_id`, warning against passing `candidate.id`.
- round 3, finding 7 (retrieval-warning test calls omit required keyword-only `included_matches`
  / `semantic_unavailable`), verdict fixed — verified both are required kw-only
  (`retrieval_warnings.py:189-190`); added an explicit call template to section 2 of the Test
  Plan.
- round 3, finding 8 ("plain `Memory` instance" fallback test invalid — `memory_response`
  dereferences `memory.project`), verdict fixed — verified `inspection/views.py:248-249`; renamed
  the test and specified a persisted, project-bearing `Memory` fetched outside `_base_queryset`
  so it exercises the `getattr` fallback and not a `project` `AttributeError`.
- round 4, finding N/A, verdict refuted:false positive — the adversarial reviewer produced no
  output this round (reported only "Waiting on the Codex task to finish in the background");
  zero numbered findings were delivered, so there is nothing to verify, fix, or refute. No spec
  section outside Review Reconciliation changed.
- round 5, finding 1 (idempotent-replay path reinjects a conflicted memory / persists stale
  warnings; absolute "never inject" is false), verdict fixed — verified `execute` returns
  `_result_from_bundle(existing_bundle)` before any conflict filter or warning computation
  (`context/services.py:901-906`), `_result_from_bundle` rehydrates persisted items + warnings
  (`:1003-1022`), and the contract test `context_api_tests.py:2160` enshrines verbatim replay
  after state change. Decided: the creation-time snapshot wins (pre-existing, non-conflict-
  specific idempotency contract). Qualified Problem/Evidence ("fresh retrieval" at ranking time),
  added an Error Handling → Replay snapshot bullet, and an Out of Scope replay item; no new
  per-warning-code replay test (the existing idempotency test fixes all codes).
- round 5, finding 2 (new title+UUID warning lacks wrong-team isolation and secret-title
  redaction tests), verdict fixed — the code already gates via `filter_documents_by_team_visibility`
  and `redact_text`, but the test plan had neither. Added two required data-exposure parity tests
  to Test Plan §2 (`test_conflict_excluded_excludes_memory_outside_team_scope` mirroring
  `context_api_tests.py:2064`, and `test_conflict_excluded_message_redacts_secret_shaped_title`).
- round 5, finding 3 (`[:200]` cap loss condition mis-stated as ">200 query-matching"), verdict
  fixed — verified the slice is applied before both team-visibility filtering and scoring
  (`retrieval_warnings.py:81,86,97`); corrected the Error Handling wording to ">200 **eligible**
  conflicted documents" and that 200 unrelated/unauthorized rows can hide the only matching one.
  Still accepted parity (no impact at cap 3 / realistic counts); the prior round-3 entry's
  narrower phrasing is superseded by this correction.
- round 5, finding 4 (`getattr(..., False)` fallback is fail-open for the exact defect), verdict
  fixed — verified both and only `memory_response` callers source rows from `_base_queryset`
  (`inspection/views.py:91,118`) and the related-memory block never calls it. Redesigned to
  fail-closed: read `memory.has_open_conflict` directly (no default), documented the annotation as
  a required precondition, added the fail-open rejected alternative to Decision 2, and reframed
  the field as display-only while retrieval enforces exclusion independently. Renamed the former
  "defaults false" test rationale accordingly.
- round 5, finding 5 (`mcp_tools.py:280-285` citation is `list_observations`, not search),
  verdict fixed — verified search rendering is `mcp_tools.py:161-175` and `280-285` belongs to
  `list_observations`; corrected the live citation to `161-175`. (The round-3 reconciliation
  entry retains its historical `280-285` reference; it is append-only history.)
- round 6, finding 1 (fail-loud contract contradicts its mandatory test — the `getattr`-fallback
  test at Test Plan §1 asserts `False`/no-exception, which direct access must fail), verdict fixed
  — the round-5 redesign to direct `memory.has_open_conflict` was correct, but the test bullet was
  left describing the rejected fallback; rewrote it to `test_memory_response_without_annotation_raises`
  asserting `pytest.raises(AttributeError)` (fail-closed, Decision 2). No `getattr` fallback remains
  anywhere in the spec, so the mandatory test now proves the fail-loud contract instead of
  reinstating fail-open.
- round 6, finding 2 (initial-build TOCTOU: ranking and the warning query observe conflict state
  at different, unsnapshotted times, so the warning can be factually false), verdict
  refuted:very-rare-edge-case — verified the warning query runs post-ranking with no shared
  snapshot (`context/services.py:913,927`, between separate atomics `:909,:950`), but this is
  identical pre-existing behavior in `stale_and_refuted_warnings` (`retrieval_warnings.py:81-87`);
  conflicts change only via rare console `memories:admin` actions, the window is sub-second, and
  the warning is strictly informational (the hard exclusion at `:315` is the injection authority).
  Documented as accepted parity in Error Handling → Initial-build consistency / TOCTOU; not
  tightened (a shared snapshot would break Decision 4 parity for no signal).
- round 6, finding 3 (cap test can pass without testing the cap — `open_single_conflict` mints a
  fresh project per call and warnings are project-scoped, and "at most 3" passes at 0/1), verdict
  fixed — verified `open_single_conflict`→`provenanced_candidate`→`_create_scope` creates a new
  org/project per suffix (`transitions_test_support.py:46,486`); rewrote the cap test to build four
  conflicts in **one** scope via `provenanced_candidate_in_scope` and assert **exactly three
  distinct `memory_id`s**.
- round 6, finding 4 (min-score test may exercise the `None` branch, not `< 60`), verdict fixed —
  verified an unrelated query returns `None` with `has_request_terms=True` (`context/services.py:398,406`,
  dropped by `match is None`) while the below-threshold path is a score-40 full-text-only match
  (`:389-396`); rewrote the test to force score 40 via a body-only prose word absent from the title
  and `exact_terms` (`term_extraction.py:54-64,121-129`; `full_text` composed at `projections.py:80`),
  so deleting the `< CONFLICT_EXCLUDED_MIN_SCORE` check would now turn the test red.
- round 7, finding N/A, verdict refuted:false positive — the adversarial reviewer returned
  "AIRTIGHT" with zero numbered findings; there is nothing to verify, fix, or refute. No spec
  section outside Review Reconciliation changed.
- round 7, finding 1 (visibility goal overclaimed — warning suppresses full-text/40, semantic/30,
  lexical/20 matches below the 60 min-score, so those conflicts stay invisible), verdict fixed —
  verified `score_retrieval_document` bands (exact-term 60 `:379-387`, full-text 40 `:389-396`,
  semantic 30 `:437-470`) and that `CONFLICT_EXCLUDED_MIN_SCORE=60` mirrors `STALE_REFUTED_MIN_SCORE=60`
  (`retrieval_warnings.py:38,98`). This is deliberate parity, not a bug, but the Problem/Evidence and
  agent story overclaimed "zero signal → signal" without qualification. Added a "Scope of the signal"
  paragraph to Problem/Evidence stating the warning covers only score-≥60 matches, and an Out of Scope
  item; the score-40 test correctly enshrines the parity boundary. Lowering the threshold is a
  cross-cutting change to the shared warning shape (Decision 4 parity), not conflict-specific.
- round 7, finding 2 (TOCTOU premises false — conflicts open via automated curation, not only console
  admin; window spans a 30s embedding call, not sub-second; persisted-replay makes the mislabel
  non-momentary), verdict fixed — verified `_hold_for_conflict` calls `OpenMemoryConflict().execute`
  automatically under the v1 decision contract (`memory/curation.py:864-887`, also `:1475`) and
  `_embedding_http_timeout` defaults to 30s (`model_policy/services.py:1507-1508`). Rewrote Error
  Handling → Initial-build consistency / TOCTOU: removed the "rare console action" and "sub-second"
  premises, restated it as an **explicitly accepted informational-inconsistency risk** justified by
  the warning never gating injection (authority is the `:315` hard exclusion), stale/refuted parity,
  and next-build self-correction; added the persisted-replay caveat cross-reference.
- round 7, finding 3 (universal "creation-time snapshot wins for all exclusion types" is false —
  `_result_from_bundle` re-checks digest visibility and `_quarantined_response` clears items/warnings
  on replay), verdict fixed — verified `_result_from_bundle` re-evaluates `unproven_digest_memory_ids`
  (`context/services.py:1018-1020`) and `_quarantined_response` replaces the warnings array
  (`:156-171`), enshrined by `context/services_tests.py:2239-2274`. Carved out digest quarantine in the
  Error Handling → Replay snapshot bullet and the Out of Scope replay item: snapshot wins for
  status/stale/refuted/conflict, but digest quarantine is re-checked and fail-closes, discarding all
  persisted warnings including `conflict_excluded`.
