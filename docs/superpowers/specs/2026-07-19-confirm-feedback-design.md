# S5 confirm-feedback — Positive feedback that resets confidence decay

## Problem and Evidence

Feedback actions are strictly negative today. The only accepted actions are `stale`
and `refuted`:

- `apps/backend/engram/memory/serializers.py:37`
  `action = serializers.ChoiceField(choices=('stale', 'refuted'))`

Confidence decay is on by default and steadily lowers the confidence of any approved,
non-stale, non-refuted memory once it ages past the min-age cutoff:

- `apps/backend/engram/core/models.py:255`
  `confidence_decay_enabled = models.BooleanField(default=True)`
- `apps/backend/engram/memory/confidence_decay.py:33-82` (`DecayMemoryConfidence`,
  `_decay_project`). The decay candidate filter uses `updated_at__lt=cutoff`
  (`confidence_decay.py:73`) as the sole "age" anchor, and each decay step writes
  `memory.save(update_fields=['confidence', 'updated_at'])` (`confidence_decay.py:79`),
  so `updated_at` (auto_now, `core/models.py:20`) is the effective decay clock.

Result: when an agent verifies during a session that a memory is still accurate, it
has no way to record that verification. The memory keeps decaying as if unseen. There
is no positive counter-signal.

The client exposes only the two negative actions:

- `packages/cli/engram_cli/mcp_server.py:195`
  `"action": {"type": "string", "enum": ["stale", "refuted"]}`
- `packages/cli/engram_cli/mcp_tools.py:339`
  `if not memory_id or action not in ("stale", "refuted") or not reason:`

There is no CLI `feedback` subcommand — `commands.py` only references
`engram_memory_feedback` in prompt text (`commands.py:1327`). So the client surface is
MCP-only.

Memory model has `stale`/`refuted` booleans but no confirmation field
(`core/models.py:727-728`). `RecordMemoryFeedback` dispatches stale/refuted to the
transition service (`MarkMemoryStale`/`RefuteMemory`) and decides idempotency from the
target boolean (`services.py:248-249`, `_already_applied` → `bool(getattr(memory, action))`).

## Design

Add a third feedback action, `confirmed`, that records that a memory was verified as
still accurate and resets the decay clock, without changing state, status, or confidence.

Decisions:

1. **New action value `confirmed`** added to the serializer choices and to the client
   enum/validation. The HTTP request shape is the same as stale/refuted (`memory_id`,
   `action`, `reason`, `request_id`, optional scope + `correlation_id`).
   **MCP-client reality (finding 5 + finding 3):** the `engram_memory_feedback` inputSchema
   declares only `memory_id`, `action`, `reason`, `project_id` (`mcp_server.py:194-199`) and
   forwards no `correlation_id` (`mcp_tools.py:349-355`). In the **normal** call the tool
   generates a fresh `request_id` per invocation via `_new_request_id`
   (`mcp_tools.py:394-397`), so each deliberate MCP re-confirm gets a new `request_id` and
   therefore **refreshes the decay clock** — the intended re-verification behavior.
   **Freshness is a convention, not an enforced invariant (finding 3):** the schema does not
   set `additionalProperties: false`, the server forwards any undeclared argument verbatim
   (`mcp_server.py:232` `arguments = dict(raw_arguments)`), and `_new_request_id` honors a
   caller-supplied `request_id` (`provided or f'mcp-{uuid4()}'`, `mcp_tools.py:395-397`) — a
   behavior the current MCP guide documents (`docs/guides/mcp.md:189-191`, "the bridge
   generates a new one per call unless the caller supplies its own"). So a caller CAN pin a
   `request_id`. This is **accepted, not a hole:** the only effect of a pinned key is that the
   caller's **own** repeats become idempotent no-ops (`already_applied=true`) that do **not**
   refresh the decay clock. **Cross-caller isolation is enforced, not assumed (finding 1):** the
   ledger lookup is keyed on `(actor_type, actor_id, request_id)`, not `request_id` alone (see
   step 5), so two distinct authorized callers that happen to pin the *same* `request_id` string
   do **not** collide — each actor gets its own ledger entry, its own confirm, its own audit
   attribution and its own receipt; one caller can never suppress another caller's verification
   or be handed another caller's `confirmed_at`. A key collision therefore idempotently dedupes
   only a single caller's retries, which is exactly what an idempotency key means. We therefore
   do NOT disable `additionalProperties`, strip the key, or add `request_id`/`correlation_id` to the
   MCP schema in v1 (agents should not have to manage idempotency keys, and the existing
   stale/refuted contract already relies on `_new_request_id`'s shared behavior; changing it is
   out of scope for this slice). `request_id` idempotency is an **API-contract guarantee for
   HTTP callers that supply/replay a `request_id`**, exercised by the backend tests.

2. **New nullable column `Memory.last_confirmed_at`** (`DateTimeField`, null/blank). A
   real column, not a metadata key, because the decay query filters SQL-side on the anchor and
   the anchor must be a first-class, correctly-typed SQL-comparable value. A metadata JSON key
   would require a per-row `metadata ->> 'last_confirmed_at'` extraction plus a `::timestamptz`
   cast inside the decay filter and the `GREATEST(...)` anchor. That is not literally
   un-indexable — Postgres does support an expression index over the extracted/cast value (the
   same class of expression index this spec floats for `GREATEST(...)` at step 2/finding 6) —
   but it is materially worse: an untyped text extraction that must be cast on every comparison,
   a JSON contract the ORM can't express as a plain field lookup, and NULL/format-validation
   handling the column gets for free. A real nullable `DateTimeField` is simpler and typed.
   Rejected: metadata JSON key.
   **Index caveat (finding 6):** the existing perf index is
   `(organization, project, status, updated_at)` (`core/models.py:744-747`). The decay filter
   `GREATEST(updated_at, last_confirmed_at) < cutoff` still uses the index prefix
   `(organization, project, status)` for the equality columns, but the `GREATEST` expression
   **cannot** use the `updated_at` range component — that narrowing becomes a filter over the
   per-project `status=approved` set. We explicitly **accept** this: `DecayMemoryConfidence`
   is a low-frequency scheduled batch that already iterates project-by-project over the
   already index-narrowed approved set, so losing the `updated_at` range narrowing is a
   bounded, acceptable scan. A future expression index
   `GREATEST(updated_at, last_confirmed_at)` is possible if decay latency ever regresses; not
   added in v1.

3. **Decay anchor = `GREATEST(updated_at, COALESCE(last_confirmed_at, updated_at))`
   (backend-neutral, finding 2).** `_decay_project` annotates
   `decay_anchor = Greatest('updated_at', Coalesce('last_confirmed_at', 'updated_at'))` and
   filters `decay_anchor__lt=cutoff` in place of `updated_at__lt=cutoff`. The `Coalesce` is
   **required** for backend neutrality: the repo also supports SQLite
   (`settings.py:41-55`, `test_settings.py:10-23`), and on SQLite (and Oracle) Django's
   `Greatest` maps to a scalar `MAX` that returns `NULL` when *any* argument is `NULL` — unlike
   Postgres, which ignores NULLs. Without the `Coalesce`, every legacy / never-confirmed row
   (`last_confirmed_at IS NULL`) would yield `decay_anchor = NULL`, fail
   `decay_anchor__lt=cutoff`, and **silently stop decaying** on SQLite. Coalescing a null
   confirmation to `updated_at` feeds `Greatest` two non-null timestamps on every backend, so an
   unconfirmed memory keeps `updated_at` as its anchor (behavior unchanged on both backends) and
   a recently confirmed memory is excluded until the confirmation itself ages past the cutoff.
   Rejected: bumping `updated_at` on confirm — conflates "content changed" with "verified", and
   would silently reset the anchor for any code path that reads `updated_at`.

4. **Confirm does NOT go through the `_execute_memory_state` transition machinery.**
   Confirmation changes no retrievable content and no state flags, so routing it through
   `MarkMemoryStale`-style transitions (`transitions.py:2288-2340`) would write a new
   `RetrievalDocument` and advance the memory pointer on every confirm — table bloat for a
   pure timestamp write. Instead confirm lives in `RecordMemoryFeedback` alongside
   stale/refuted, reusing the same lock + team-scope + audit + idempotency discipline.
   This also means `transition_contract_version` and the transition contract are untouched
   (avoids the known v0-defaults trap). Rejected alternative: a new `CONFIRM`
   `MemoryTransitionType` + `_apply_memory_state` branch — over-engineered for a no-content
   change. This is a scope decision flagged for review (see Review Reconciliation).

5. **Idempotency by `request_id` via an audit ledger.** Confirm is idempotent per
   `request_id`: a repeat confirm with the same `request_id` is a stable no-op
   (`already_applied=True`, `confirmed_at` reported from the **original** audit event, not the
   current memory row); a confirm with a new `request_id` refreshes `last_confirmed_at` and
   writes a new audit event. The memory row is locked (`select_for_update` via
   `lock_memory_for_update`), so concurrent same-request confirms serialize and the second
   sees the ledger.

   **Ledger lookup precedes the state guard (finding 1).** The ledger `.exists()` check runs
   **before** the stale/refuted/status guard, mirroring the transition contract, which checks
   `_existing_transition` before current-state validation
   (`transitions.py:2304-2310`). Otherwise a replay of a successful confirm on a memory that
   has since gone stale/refuted would return 400 instead of the original success. Order:
   lock → team-scope → **ledger lookup** → (only on a new `request_id`) status/stale guard →
   write. On a ledger hit the confirm is a pure reporting no-op regardless of the memory's
   current state.

   **Replay reports the original timestamp (finding 1).** The response's `confirmed_at` on a
   ledger hit is read from the matched `MemoryConfirmed` event's
   `metadata['confirmed_at']`, **not** from `memory.last_confirmed_at` — otherwise a later
   confirm with a newer `request_id` would make an older request's replay return the newer
   timestamp. Each `request_id` therefore replays its own stable timestamp.

   **Lighter collision guarantee than the transition contract (accepted, finding 1).** The
   transition contract fingerprints the full semantic request (reason, actor, capability,
   scope) and raises `idempotency_collision` on mismatched reuse
   (`transitions.py:350-373,490-508`). The confirm ledger keys on `(actor_type, actor_id,
   request_id)` — so **actor is part of the key** (this closes the cross-caller interference in
   finding 1) — but it does **not** fingerprint `reason`/`capability`/`scope`: a same-actor
   same-`request_id` replay with a different `reason` is a harmless first-write-wins no-op that
   returns the original event. This is acceptable because confirm mutates no retrievable content
   or state flags (only a timestamp), so there is no state to corrupt; the idempotency key wins
   and the first reason is the recorded one. Full `reason`/scope fingerprint-collision rejection
   is deliberately out of scope for a pure-timestamp write.

6. **Reject confirming any decay-ineligible OR quarantined memory (finding 3 + finding 2 +
   round 10 finding 1)** with a friendly validation error rather than auto-unstaling. The guard
   mirrors the **full per-row** decay eligibility filter (`confidence_decay.py:65-74`) **and adds
   the retrieval quarantine predicate** (open-conflict exclusion, see the conflict sub-bullet),
   not just the status/flag subset (the per-*org* enablement gate is deliberately excluded — see
   below). Confirm is rejected unless **all** of:
   `status == MemoryStatus.APPROVED and not memory.stale and not memory.refuted and
   memory.confidence is not None and memory.confidence > floor and memory.kind != 'digest' and
   not <memory has an unresolved MemoryConflict> and <memory has a caller-retrievable active
   RetrievalDocument>`, where `floor = settings.ENGRAM_CONFIDENCE_DECAY_FLOOR` (the same setting
   decay reads, `confidence_decay.py:63`) and `<memory has a caller-retrievable active
   RetrievalDocument>` means a `RetrievalDocument` for the memory that retrieval would actually
   surface **to this caller** — `stale=False, refuted=False` **and** the document's own visibility
   is one `filter_documents_by_team_visibility` admits (`context/services.py:280-292`): PROJECT, or
   TEAM whose `team_id` is in the caller's `scope.team_ids`:
   ```python
   RetrievalDocument.objects.filter(memory_id=memory.id, stale=False, refuted=False).filter(
       Q(visibility_scope=VisibilityScope.PROJECT)
       | Q(visibility_scope=VisibilityScope.TEAM, team_id__in=scope.team_ids)
   ).exists()
   ```
   (round 12 finding 2 + round 14 finding 1 — see the retrieval-injectability sub-bullet below).
   - Checking `status == APPROVED` — not just the `stale`/`refuted` flags — is required because
     `ARCHIVED`, `REFUTED`, and `CONFLICT` are independent status values
     (`core/models.py:105-109`) with no model constraint coupling them to the stale/refuted
     booleans (`core/models.py:761-769`). A legacy or noncanonical `ARCHIVED`/`REFUTED`/
     `CONFLICT` row with both flags false would otherwise be confirmable even though decay never
     touches it (decay requires `status=APPROVED`).
   - **Open conflicts are NOT caught by the status check and are rejected separately (round 10
     finding 1).** The canonical conflict representation is **not** the `MemoryStatus.CONFLICT`
     enum value: `OpenMemoryConflict` creates an unresolved `MemoryConflict` row and leaves the
     memory row **`status=APPROVED, stale=False, refuted=False`** untouched
     (`transitions.py:2387-2461`; proven by `curation_tests.py:886-890`, which asserts exactly
     `status == APPROVED and stale is False and refuted is False` after a conflict opens while
     retrieval quarantines the memory). Nothing in the codebase writes `status=CONFLICT` to a
     memory row, so the status check above is inert against real conflicts. Two consequences:
     (a) the raw decay filter (`confidence_decay.py:65-74`) **does decay** an open-conflict
     memory (it passes every decay predicate), so a "mirror the decay filter exactly" guard would
     *accept* it; but (b) retrieval explicitly quarantines it
     (`context/services.py:315-322`: `~Exists(MemoryConflict … resolved_transition__isnull=True)`),
     so it is **never injected** into a session. Confirm therefore **diverges from the raw decay
     filter on purpose**: it requires the memory to be decay-eligible **and retrieval-injectable**,
     rejecting any memory with an unresolved conflict
     (`MemoryConflict.objects.filter(memory_id=memory.id, resolved_transition__isnull=True).exists()`,
     the single-row form of the retrieval quarantine). Rationale: "confirmed" means *an agent
     verified this memory as still accurate*, but a quarantined memory is never injected, so no
     agent could have seen it to verify it; and refreshing the decay clock of a memory whose
     accuracy is under active dispute is precisely the wrong signal. This is the one deliberate
     place where the confirmability contract is **stricter** than the raw decay filter, and it is
     tested (test 7c). Decay's own treatment of conflicted rows is existing behavior and is left
     unchanged (out of scope).
   - **Active RetrievalDocument must exist — the second retrieval-quarantine predicate (round 12
     finding 2).** Open-conflict exclusion is only *one* of the predicates that make a memory
     retrieval-injectable. Retrieval also requires a `RetrievalDocument` for the memory whose **own**
     `stale`/`refuted` flags are false (`context/services.py:309-313` filters `stale=False,
     refuted=False` on the **document**, independently of the memory's own `stale`/`refuted` — the
     two are filtered separately precisely because they can diverge). Neither `Memory` nor
     `RetrievalDocument` has a constraint guaranteeing every approved memory owns an active document
     (`core/models.py:713`, `865`), so a legacy/noncanonical approved, non-stale, non-refuted memory
     with **no** document — or only `stale`/`refuted` documents — passes every memory-field decay
     predicate yet is **never injected** into a session. Confirming it would hand back a false
     "verified accurate" receipt and reset the decay clock of a memory no agent could have retrieved,
     the exact defect the open-conflict clause exists to prevent.
   - **Document visibility MUST be re-checked — a bare `.exists()` overstates the guarantee (round
     14 finding 1).** An earlier draft required only
     `RetrievalDocument.objects.filter(memory_id=memory.id, stale=False, refuted=False).exists()` and
     claimed "document visibility need not be re-checked — the team-scope guard already rejected any
     caller who cannot see the memory." That claim is **false**. Retrieval's final surfaceable set is
     produced by `filter_documents_by_team_visibility` (`context/services.py:280-292`), which appends
     **only** documents whose **own** `visibility_scope` is `PROJECT`, or `TEAM` with
     `team_id in scope.team_ids`; it **hard-drops** every `SESSION`- and `ORGANIZATION`-scoped document
     (`VisibilityScope` carries both, `core/models.py:52-56`, and promotion persists the candidate's
     scope verbatim when no override is supplied, `transitions.py:1440-1455`). The team-scope guard
     (`_ensure_team_scope`, `services.py:838-844`) does **not** close this: (a) it rejects **only**
     `TEAM` memories with a non-null unauthorized team — it lets a `SESSION`/`ORGANIZATION` memory
     through untouched; and (b) it keys on the **memory's** `visibility_scope`/`team_id`, whereas
     retrieval keys on the **document's**, and the two can diverge (no model constraint couples them,
     `core/models.py:713-724,761-769,865`). So a memory whose only active document is `SESSION`/
     `ORGANIZATION`-scoped — or `TEAM`-scoped to a team the caller cannot see — passes both the
     team-scope guard and a bare `.exists()`, yet retrieval never injects it: the same false-receipt
     defect. Confirm therefore mirrors `filter_documents_by_team_visibility` exactly and requires a
     document retrieval would actually surface **to this caller**:
     `RetrievalDocument.objects.filter(memory_id=memory.id, stale=False, refuted=False).filter(
     Q(visibility_scope=VisibilityScope.PROJECT) | Q(visibility_scope=VisibilityScope.TEAM,
     team_id__in=scope.team_ids)).exists()`. Together with the open-conflict clause this makes the
     confirmability contract enforce the full "decay-eligible **and** retrieval-injectable **to this
     caller**" claim, not merely assert it: the memory-field subset
     (status/stale/refuted/confidence/floor/digest) is the decay half, and (open-conflict absent) +
     (a caller-retrievable active document present) is the retrieval half. Tested by test 7d.
   - Checking `confidence is not None`, `confidence > floor`, and `kind != 'digest'` (finding 2)
     is required for the identical reason: the decay filter also carries `confidence__isnull=False`,
     `confidence__gt=floor`, and `.exclude(kind='digest')` (`confidence_decay.py:71-74`). A
     null-confidence, at-or-below-floor, or `digest` approved memory is never decayed, so
     confirming it would be the same meaningless audit/timestamp write this guard exists to
     prevent. Rejecting it keeps the guard and the decay filter in exact lockstep.
   - **Org-level `confidence_decay_enabled` is deliberately NOT part of the guard (finding 3).**
     Decay first skips whole organizations whose `confidence_decay_enabled=False`
     (`confidence_decay.py:39-46`, tested `confidence_decay_tests.py:161-171`), but confirm does
     **not** consult that org toggle, and a memory in a decay-disabled org **is still
     confirmable**. Rationale: (a) the toggle is a *mutable operational org setting*, not an
     immutable property of the memory row — a memory confirmed while decay is toggled off must
     still have its `last_confirmed_at` anchor set so the verification counts if the org later
     re-enables decay; (b) the toggle is invisible to the confirming agent, so rejecting on it
     would surface a confusing "not confirmable" error for a memory that is, by every visible
     property, a normal approved memory. Confirm is therefore a durable verification receipt that
     is *independent* of whether decay is currently running for the org. "Mirrors the full decay
     eligibility filter" above means the **per-row** predicate (`confidence_decay.py:65-74`); the
     per-org gate (`:39-46`) is out of the per-memory confirmability contract by design.

   Restore/unstale is a console decision (out of scope).

7. **No confidence boost in v1** (anti-gaming, KISS). Confirm only resets the decay clock.

8. **Confirm requires a `memories:review` key; the interactive wizard is NOT expanded and no
   new capability is introduced (round 10 finding 2).** The feedback endpoint requires the
   `memories:review` capability (`views.py:60`, `required_capability='memories:review'`), but the
   interactive connect wizard provisions keys with only
   `('memories:read', 'observations:write', 'search:query')` (`commands.py:57`,
   `WIZARD_API_KEY_CAPABILITIES`, pinned by `cli_lifecycle_tests.py:2929-2937`). A key minted by
   the default wizard therefore cannot call feedback and gets HTTP 403 from
   `resolve_request_scope`. **This is pre-existing and unchanged by S5:** the endpoint and the two
   existing negative actions (`stale`/`refuted`) already require `memories:review` today, and the
   wizard already omits it, so the confirm action inherits the identical, already-shipped auth
   contract and adds no new auth surface. Decision for v1: keys used for **any** feedback action
   (`stale`/`refuted`/`confirmed`) must be **separately provisioned with `memories:review`**
   (e.g. an operator-issued review key), the same requirement that governs stale/refuted today.
   We deliberately do **not** (a) widen the wizard's default capability set — that would grant
   every plug-and-play agent org-wide review/mutation authority over memories as a side effect of
   connecting, a security regression far larger than this slice; nor (b) mint a new narrower
   `memories:confirm` capability — that fragments the feedback endpoint's single
   `required_capability`, forces a matching backend authorization change, and would still leave
   stale/refuted gated on `memories:review`, i.e. two capabilities guarding one endpoint for no
   product gain in a dogfood instance. Re-scoping feedback authorization (wizard defaults or a
   dedicated confirm capability) is a cross-cutting change to the existing feedback surface and is
   **out of scope** for this additive slice; it is recorded as a follow-up.

9. **Confirm operates on current-at-processing semantics, not an agent-observed-version fence
   (round 12 finding 1).** The request carries only `memory_id`; confirm resets the decay clock of
   whichever version is current when the confirm is processed, and does **not** verify that the
   memory matches the version the agent saw at retrieval time. This is an explicit choice, not an
   oversight, and it is **consistent with confirm's two sibling feedback actions**: `stale`/`refuted`
   through this same endpoint also take only `memory_id`+`action`+`reason` and build their
   `MemoryFence` **server-side from the just-locked row** (`services.py:294`
   `memory_fence=build_memory_fence(memory)`, where `memory` is the row locked at
   `services.py:226`), **not** from an agent-supplied observed version — the fence there is a
   same-request concurrency guard (row unchanged between lock and transition write), never an
   "agent observed this exact version" assertion. No feedback action is version-fenced against the
   caller's read. Rationale confirm can safely share this:
   - **Confirm can only push the anchor to `now`, never beyond it, so a stale-read confirm gains
     nothing over an honest one.** A confirm writes `last_confirmed_at = now` at processing time
     (call it `t1`). The decay anchor is `GREATEST(updated_at, last_confirmed_at)` (decision 3), and
     the only value confirm can write is the current instant, so after any confirm the anchor is
     exactly `t1`. Consider the worst case the reviewer raised: a revision lands at `t0` (bumping
     `updated_at` via `auto_now`, `core/models.py:20`) and a caller who read the **pre-`t0`** version
     confirms at `t1 > t0`. The anchor moves from `t0` to `t1` — so yes, the stale confirm **does**
     extend decay by `t1 − t0` (the earlier claim that this is a no-op / "cannot extend beyond the
     content change" was wrong and is corrected here). But that extension is (a) **bounded at `now`** —
     confirm cannot write a future timestamp, so it can never push the anchor past the moment of
     processing — and (b) **identical to what a legitimate confirm of the current version at the same
     instant `t1` would produce**: both land the anchor on `t1`. The stale reader therefore obtains no
     more decay life than an honest confirmer acting at the same time, and there is no amplification or
     unbounded extension. This *is* the accepted current-at-processing semantics (decision 9): confirm
     resets the decay clock to processing time regardless of which version the caller read.
   - **Confirm changes no retrievable content or state flags** (decision 4/7): it only writes a
     timestamp, records no confidence boost (anti-gaming), and the `MemoryConfirmed` audit records
     `confirmed_at` and the reason — it makes **no** claim of version fidelity, so a since-changed
     body does not corrupt any stored assertion.
   - **The MCP tool intentionally carries no version** (agents should not manage version fences;
     decision 1), so introducing an observed-version parameter would add client-side idempotency-
     key-style bookkeeping for negligible benefit, against the KISS posture of this slice.
   Current-at-processing semantics are asserted by test 13. Adding an observed-version fence to the
   feedback surface (all three actions) is a cross-cutting change **out of scope** for this additive
   slice.

## API and Schema Changes

### Endpoint (unchanged path)

`POST /v1/memories/{memory_id}/feedback` (`MemoryFeedbackView`, `memory/views.py:51-101`).

Request body (serializer `MemoryFeedbackSerializer`, `serializers.py:33-45`) — only the
`action` enum changes:

- `action`: `ChoiceField(choices=('stale', 'refuted', 'confirmed'))`
- `reason`: `CharField(max_length=2000, allow_blank=False)` (unchanged; required for confirm too)
- `request_id`: `CharField(max_length=255)` (unchanged; idempotency key for confirm)
- other fields (`project_id`, `repository_url`, `team_id`, `correlation_id`) unchanged.

### Model + migration

`apps/backend/engram/core/models.py` — add to `Memory` after `refuted` (`core/models.py:728`):

```python
last_confirmed_at = models.DateTimeField(null=True, blank=True)
```

Migration `apps/backend/engram/core/migrations/0041_memory_last_confirmed_at.py`
(`AddField`, nullable, no `db_default` needed). Latest existing migration is `0040_curation_decision.py`.

**Fail-closed reverse guard (round 8, finding 1).** A plain `AddField` reverse drops the
`last_confirmed_at` column, but the `MemoryConfirmed` receipts live in the **independent**
`AuditEvent` table (`core/models.py:1057`), which is untouched by this migration. After a
reverse + re-apply, every `last_confirmed_at` is NULL while the ledger survives; a replay of a
prior `request_id` then hits the surviving ledger, returns `already_applied=True` with the
original `confirmed_at` (step 2/finding 1), and **never restores the anchor** — the memory
decays as though it was never confirmed. That is silent data loss on a schema reversal, which
the operator directive keeps explicitly in scope. The migration therefore appends a fail-closed
reverse guard, mirroring the existing in-repo precedent `0039_import_provenance.py:7-10,102`
(`RunPython(noop, _guard_reverse)` that raises `RuntimeError` when the data a reverse would
orphan exists):

```python
def _guard_reverse(apps, schema_editor):
    memory_model = apps.get_model('core', 'Memory')
    audit_model = apps.get_model('core', 'AuditEvent')
    if (
        memory_model.objects.filter(last_confirmed_at__isnull=False).exists()
        or audit_model.objects.filter(event_type='MemoryConfirmed').exists()
    ):
        raise RuntimeError(
            'cannot reverse 0041 while confirmation history exists '
            '(dropping last_confirmed_at would orphan MemoryConfirmed receipts and '
            'silently resurrect decay on confirmed memories)'
        )


operations = [
    migrations.AddField(
        model_name='memory',
        name='last_confirmed_at',
        field=models.DateTimeField(null=True, blank=True),
    ),
    migrations.RunPython(migrations.RunPython.noop, _guard_reverse),
]
```

Forward is a plain additive `AddField` (`RunPython.noop` forward). Reverse aborts the whole
transaction (Postgres runs migrations transactionally) unless confirmation history is empty, so
an operator who genuinely wants to reverse must first consciously clear both the column and the
`MemoryConfirmed` ledger. On a clean/never-confirmed database the reverse is an allowed no-op.

### Response format

`MemoryFeedbackResult.to_response` (`services.py:203-213`) gains `confirmed_at`. Existing
fields unchanged; `stale`/`refuted` returned as-is.

Confirm success (first confirm):

```json
{
  "memory_id": "…",
  "project_id": "…",
  "team_id": "",
  "action": "confirmed",
  "stale": false,
  "refuted": false,
  "confirmed_at": "2026-07-19T10:00:00+00:00",
  "retrieval_documents_updated": 0,
  "already_applied": false
}
```

Confirm repeat (same `request_id`): `already_applied": true` and `confirmed_at` unchanged from
the first confirm. **Replay-body contract (finding 4):** the reporting fields `action`,
`already_applied` (`true`), and `confirmed_at` (the **original** value, read from the ledger
event's `metadata['confirmed_at']`) are stable across replays. The `stale`/`refuted`/`team_id`
fields, however, are rendered from the memory's **current live row** (`to_response` reads
`self.memory.stale`/`self.memory.refuted`, `services.py:209-210`), so after the explicitly
supported confirm→stale→replay sequence a replay reports `stale": true`. This is intentional:
the replay is a truthful snapshot of the memory *now* plus the immutable confirm receipt, not a
frozen copy of the first response. The idempotency guarantee is specifically over the confirm
receipt (`confirmed_at`, `already_applied`), never over unrelated state flags that a later
transition may legitimately change. Callers needing the confirm receipt read `confirmed_at`;
callers needing current state read `stale`/`refuted` — both are always live-accurate.

Stale/refuted responses gain `confirmed_at` too (reflecting the memory's existing
`last_confirmed_at`, or `""` if never confirmed) but are otherwise unchanged.

`confirmed_at` renders as `memory.last_confirmed_at.isoformat()` or `''` when null.

### Service changes (`services.py`)

- Imports for the confirm branch: `MemoryStatus`, `MemoryConflict`, and
  `AuditEvent`/`AuditResult` from `engram.core.models`; `redact_value` is already defined in this
  module (`services.py`).
- `MemoryFeedbackResult` (`services.py:196-213`): add field `confirmed_at: str` and emit
  it in `to_response`. For stale/refuted responses compute from `memory.last_confirmed_at`
  (or `''`); for the confirm path it is set explicitly per step 2/5 above.
- `RecordMemoryFeedback.execute` (`services.py:223-240`): branch on
  `data.action == 'confirmed'` **before** calling `_already_applied` (which does
  `getattr(memory, action)` and would raise `AttributeError` for `confirmed`). Confirm path,
  all inside `transaction.atomic()`:
  1. `memory = self._lock_memory(data, scope)`; `self._ensure_team_scope(memory, scope)`.
  2. **Ledger lookup first (finding 1)** — before any state guard:
     ```python
     prior = (
         AuditEvent.objects.filter(
             organization=memory.organization, project=memory.project,
             event_type='MemoryConfirmed', target_type='memory',
             target_id=str(memory.id),
             actor_type=scope.actor_type, actor_id=scope.actor_id,
             request_id=data.request_id,
         )
         .order_by('created_at', 'id')
         .first()
     )
     ```
     The lookup is keyed on **actor + request_id** (finding 1), so a `request_id` pinned by two
     different callers does not collide: each caller replays only its own confirm receipt and no
     caller is handed another caller's `confirmed_at`.
     If `prior is not None`: return
     `MemoryFeedbackResult(memory=memory, action='confirmed',
     retrieval_documents_updated=0, already_applied=True,
     confirmed_at=str(prior.metadata.get('confirmed_at') or ''))` — a stable no-op that
     reports the **original** timestamp from the prior event and does **not** re-evaluate the
     status/stale guard (so a replay still succeeds after the memory later goes
     stale/refuted/archived).
  3. **State guard, new `request_id` only (finding 3 + finding 2 + round 10 finding 1 + round 12
     finding 2):** with `floor = settings.ENGRAM_CONFIDENCE_DECAY_FLOOR`, if **any** of
     `memory.status != MemoryStatus.APPROVED or memory.stale or memory.refuted or
     memory.confidence is None or memory.confidence <= floor or memory.kind == 'digest' or
     MemoryConflict.objects.filter(memory_id=memory.id, resolved_transition__isnull=True).exists()
     or not RetrievalDocument.objects.filter(organization=memory.organization,
     project=memory.project, memory_id=memory.id, stale=False, refuted=False).filter(
     Q(visibility_scope=VisibilityScope.PROJECT) | Q(visibility_scope=VisibilityScope.TEAM,
     team_id__in=scope.team_ids)).exists()`:
     raise
     `MemoryFeedbackError('memory_not_confirmable', 'Only an approved, non-stale, non-refuted, above-floor, non-digest, non-conflicted, retrieval-injectable memory can be confirmed')`.
     (`RetrievalDocument` is already imported in this module — `services.py:251-257`; `Q` from
     `django.db.models` and `VisibilityScope` from `engram.core.models` — `VisibilityScope` already
     imported at `services.py:38`, add `Q` to the `django.db.models` import.)
     The active-document clause mirrors retrieval exactly: the
     `organization=memory.organization, project=memory.project` filter matches
     `authorized_retrieval_documents`' denormalized-scope filter
     (`context/services.py:306-308`) — so a `RetrievalDocument` whose denormalized
     `organization`/`project` has drifted away from its memory (a state with only Python-level
     validation and no DB constraint, exercised by `QuerySet.update` in
     `invariant_queries_tests.py:1414`) is rejected here exactly as retrieval rejects it, closing the
     false-receipt gap (round 15 finding 1) — and the `visibility_scope`/`team_id` clause mirrors
     `filter_documents_by_team_visibility` (`context/services.py:280-292`) exactly, so it rejects a
     memory whose only active document is `SESSION`/`ORGANIZATION`-scoped or `TEAM`-scoped to a team
     the caller cannot see — documents retrieval never surfaces to this caller (round 14 finding 1). The open-conflict clause **and**
     the caller-retrievable-document clause are the single-row form of the
     retrieval quarantine (`context/services.py:280-322`) and are evaluated **while holding the
     memory row lock** (the
     guard runs after `_lock_memory`), so a conflict opened concurrently is serialized on the same
     Memory row lock: `OpenMemoryConflict` locks the memory via `_lock_declared_memories`
     (`transitions.py:728`, `Memory.objects.select_for_update()`) before creating the
     `MemoryConflict`, so confirm either sees the committed conflict row or blocks until the
     conflict transaction commits.
  4. `memory.last_confirmed_at = timezone.now()`;
     `memory.save(update_fields=['last_confirmed_at'])` (deliberately excludes `updated_at`
     so auto_now does not fire); then write the `MemoryConfirmed` audit event.
  5. Return `MemoryFeedbackResult(memory=memory, action='confirmed',
     retrieval_documents_updated=0, already_applied=False,
     confirmed_at=memory.last_confirmed_at.isoformat())`.
- Audit event (finding 2 + round 12 finding 3 — scope the receipt to the memory's *effective
  retrieval visibility*, and carry a redacted reason, matching the transition-path audit at
  `transitions.py:1248-1261,376-393`): `event_type='MemoryConfirmed'`,
  `team_id = memory.team_id if memory.visibility_scope == VisibilityScope.TEAM else None`
  **(round 12 finding 3 — the audit's team scope MUST follow `memory.visibility_scope`, not the raw
  `memory.team_id` FK).** Two failure modes bound the correct value from both sides:
  **(a)** a **TEAM-visible** memory (`visibility_scope == TEAM`, `team_id` non-null) MUST set
  `team_id = memory.team_id`, because a null team makes the event project-global and inspection
  treats `team__isnull=True` as visible-to-all (`inspection/services.py:52-53`), leaking a
  team-scoped memory's confirmation to project readers outside the team (the original round-2
  finding 2 concern). **(b)** a **PROJECT-visible** memory that nonetheless retains a non-null
  `team_id` FK — a reachable state, because `_create_candidate_memory` stores
  `team_id = candidate.team_id` while taking `visibility_scope` from the (possibly collapsed-to-
  PROJECT) `scope_override` (`transitions.py:1045-1053`), and `Memory` has **no** constraint
  coupling `team_id` to `visibility_scope` (`core/models.py:716,720-724,761-769`) — MUST set
  `team_id = None`. Retrieval exposes such a memory's PROJECT-visible document to **every** project
  reader regardless of team (`context/services.py:287-288`,
  `filter_documents_by_team_visibility` appends all PROJECT docs unconditionally), so an authorized
  project reader can see **and confirm** it; keying the audit on the raw non-null `memory.team_id`
  would hide that reader's own receipt from every project reader outside the team
  (`inspection/services.py:52-53`) — the round-12 finding-3 asymmetry. Keying on
  `visibility_scope` resolves **both** modes: the receipt is visible to exactly the audience that
  can retrieve and confirm the memory.
  **Document/memory scope-agreement invariant (round 15 finding 2 — no divergence to leak past):**
  keying the audit on `memory.visibility_scope` while the confirm *authorization* is document-scoped
  is safe because the active (`stale=False`) document always shares the memory's `visibility_scope`.
  The sole `RetrievalDocument` writer copies it directly —
  `document.visibility_scope = memory.visibility_scope` (`projections.py:101,161`), team `= memory.team_id`
  (`projections.py:158`) — and marks every prior document for the memory stale
  (`projections.py:179`), so the one active document is always the freshly-projected one. A memory's
  `visibility_scope` is set once at promotion (`transitions.py:1057`) and is never reassigned on an
  existing row (no rescope path mutates `Memory.visibility_scope`), so it cannot drift out from under
  an already-active document either. A `PROJECT`-memory-with-only-a-`TEAM`-active-document is
  therefore unreachable via live code; unlike the org/project drift retrieval actively defends
  against (finding 1, `invariant_queries_tests.py:1414`), no live path and no invariant test treats
  a document-vs-memory `visibility_scope` divergence as a defended state, so the memory-scoped audit
  key leaks to no reader that document-scoped retrieval would not already admit.
  (The raw-`memory.team_id` form used by the existing
  `MemoryTransitionCommitted` audit `transitions.py:1251` carries this same latent asymmetry for a
  PROJECT-visible-but-team-FK memory; realigning that shipped transition audit is a cross-cutting
  change **out of scope** for this additive slice — confirm is written correctly from the start.)
  `actor_type=scope.actor_type`, `actor_id=scope.actor_id`, `target_type='memory'`,
  `target_id=str(memory.id)`, `capability='memories:review'`,
  `result=AuditResult.RECORDED` (chosen to match the audits for the two closest write-side
  peers — the transition-path `MemoryTransitionCommitted` at `transitions.py:1258` and the
  `DecayMemoryConfidence` batch audit — both of which record a completed state/data mutation
  with `RECORDED`; note `AuditResult.ALLOWED` **is** used by other write-side memory audits
  (`MemoryLinkRecorded` `services.py:1069`, `MemoryLinkRemoved` `services.py:1180`), so the
  choice here is semantic alignment with the mutation-audit peers, not because `ALLOWED` is
  unavailable to write paths),
  `request_id=data.request_id`, `correlation_id=data.correlation_id`, metadata
  `{'memory_id': str(memory.id), 'confirmed_at': memory.last_confirmed_at.isoformat(),
  'reason': str(redact_value(data.reason))[:1024]}`. **Redaction-wrapper caveat (finding 1):**
  do **not** copy the transition-path form `str(redact_value(reason).value)[:1024]`
  (`transitions.py:380`). That module imports the **core** `redact_value` from
  `engram.core.redaction` (returns a `RedactionResult` with a `.value` attribute), but this
  service module has its own local `redact_value` (`services.py:698-699`) that **already
  returns the unwrapped value** (`core_redact_value(value).value`). Calling `.value` on that
  unwrapped string raises `AttributeError`, which would roll back every first confirm. The
  correct form here is `str(redact_value(data.reason))[:1024]` — same redaction semantics as
  the transition audit, adapted to this module's already-unwrapping wrapper. `confirmed_at` is
  stored so replay (step 2) can report it.
- `_log` (`services.py:302-311`) reused; `updated=0` for confirm.

### Decay change (`confidence_decay.py`)

`_decay_project` (`confidence_decay.py:60-82`). Candidate selection swaps the
`updated_at__lt=cutoff` filter for an annotated anchor:

```python
from django.db import transaction
from django.db.models.functions import Coalesce, Greatest

candidates = (
    Memory.objects.filter(
        organization=organization,
        project=project,
        status=MemoryStatus.APPROVED,
        stale=False,
        refuted=False,
        confidence__isnull=False,
        confidence__gt=floor,
    )
    .exclude(kind='digest')
    .annotate(decay_anchor=Greatest('updated_at', Coalesce('last_confirmed_at', 'updated_at')))
    .filter(decay_anchor__lt=cutoff)
)
```

(`updated_at__lt=cutoff` removed; replaced by the annotated `decay_anchor__lt=cutoff`.)

**Lock + re-check on the write (finding 4).** The current loop reads candidates and then
`memory.save(...)` with no transaction, row lock, or conditional update
(`confidence_decay.py:77-79`). A confirm can commit `last_confirmed_at` **between** the decay
read and the decay write, and the stale in-memory candidate would still be decremented — one
decay step slips past a confirmation, violating the "not decayed until the confirmation ages
out" invariant. Fix: each decay step re-locks the row and re-evaluates the anchor inside a
transaction; confirm already locks the same row via `lock_memory_for_update`, so the two
serialize:

```python
decayed_ids: list[uuid.UUID] = []
for candidate in candidates:
    with transaction.atomic():
        locked = (
            Memory.objects.select_for_update()
            .annotate(decay_anchor=Greatest('updated_at', Coalesce('last_confirmed_at', 'updated_at')))
            .filter(
                id=candidate.id,
                status=MemoryStatus.APPROVED,
                stale=False,
                refuted=False,
                confidence__gt=floor,
                decay_anchor__lt=cutoff,
            )
            .exclude(kind='digest')
            .first()
        )
        if locked is None:
            continue
        locked.confidence = max(floor, locked.confidence - step).quantize(_CONFIDENCE_QUANTIZE)
        locked.save(update_fields=['confidence', 'updated_at'])
        decayed_ids.append(locked.id)
```

Now a confirm that commits before the decay lock advances the anchor, so the re-check
(`decay_anchor__lt=cutoff`) excludes the row and it is skipped; a confirm that arrives after
blocks on the row lock until the decay txn commits, then proceeds — at most decay steps that
were legitimately earlier than the confirmation are applied. (Selecting candidates first, then
locking one row at a time, keeps the working set bounded and avoids a project-wide long-held
lock.)

**Backend scope of the row-lock serialization guarantee (round 12 finding 4).** The
*block-and-recheck* ordering guarantee above — a concurrent confirm/decay **blocks** on the shared
`select_for_update` row lock rather than racing — is **PostgreSQL-only**, and this is the repo's
established posture: `select_for_update()` is documented by Django to have **no effect on SQLite**,
and every existing row-lock concurrency test in this codebase is gated
`@pytest.mark.skipif(connection.vendor != 'postgresql', …)` (`transitions_tests.py:181-182`), as is
this slice's own serialization proof (test 10, lines below). PostgreSQL is both the deployed backend
and the test-DB backend (`pgvector`), so the guarantee holds where it is exercised. On **SQLite**
(a supported *degraded* config used only for the `Coalesce`/`Greatest` decay-query neutrality of
decision 3, `settings.py:46-55`, `test_settings.py`) the row lock is a no-op, **but** SQLite
provides serializable isolation via **database-level** write locking: concurrent writers do not
silently lose the read-then-write decay step — a genuinely concurrent confirm surfaces as an
operational `SQLITE_BUSY`/`OperationalError` (the second writer either waits on `busy_timeout` and
then proceeds serially, or errors), never as a silent lost decrement. The **idempotency ledger** (decision 5: a `SELECT` on the
`AuditEvent` ledger followed by an `INSERT`) has **no uniqueness constraint** on its
`(actor_type, actor_id, request_id, event_type, target_id)` identity (`core/models.py:1057`), so its
concurrency serialization — like decay's — rides on the **enclosing memory row lock**: the confirm
path acquires `select_for_update` on the memory row via `lock_memory_for_update` (step 5)
**before** the ledger lookup, so two concurrent same-`(actor, request_id)` confirms of the same
memory serialize on that row lock and the second observes the committed ledger row. **Backend
scope of the ledger's idempotency (round 14 finding 2 — earlier wording "needs no row-level lock and
behaves identically on both backends" was too strong):** the **sequential-replay** contract (a
later replay of a prior `request_id`, which is the guarantee the API actually promises and the
backend tests exercise) **is** backend-neutral — a plain `SELECT` that finds the committed prior
event regardless of backend, no lock involved. The **concurrent** same-`(actor, request_id)` case
depends on the memory row lock and is therefore **PostgreSQL-only**, the same block-and-recheck
scoping as decay; on SQLite the row lock is a no-op, so a genuinely concurrent same-request confirm
surfaces **fail-loud** as `SQLITE_BUSY`/`OperationalError` (database-level write locking), never a
silent double-apply — and even if two writes did land, the receipt is uncorrupted: a later replay
still returns the **first**-by-`(created_at, id)` event (step 2). So on SQLite the *idempotency*
(sequential-replay) and *decay-query correctness* contracts hold unchanged; only the **blocking**
flavor of the concurrency guarantee degrades to database-level serialization (fail-loud, not
fail-silent). We
explicitly scope the block-and-recheck guarantee to PostgreSQL and do **not** add SQLite-specific
serialization or SQLite concurrency tests — the dogfood/deploy backend is PostgreSQL and SQLite is
never run under concurrent production-like load.

### Client changes

- `packages/cli/engram_cli/mcp_server.py:184-200`: enum →
  `["stale", "refuted", "confirmed"]`; the description sentence is **inserted** after
  `…mark it stale or refuted with a reason.` and **before** the existing
  `Clean memory improves…` closing sentence (which is preserved). **The complete final
  description string (finding 5 — this is the single authoritative constant; the verbatim
  description test at `mcp_server_tests.py:164-167` must assert exactly this) is:**
  > Step 3 - close the loop: the moment you discover an injected or retrieved memory is outdated or wrong, mark it stale or refuted with a reason. Confirm a memory when you have verified it is still accurate — this resets its confidence decay clock. Clean memory improves every future session; do not silently ignore bad memory.
- `packages/cli/engram_cli/mcp_tools.py:339`: validation →
  `action not in ("stale", "refuted", "confirmed")`; update the error text to list all
  three actions. Extend the success return line (`mcp_tools.py:367-372`) with
  `confirmed_at={body.get('confirmed_at')}`.
- No CLI subcommand change (none exists).
- **Public docs (finding 9):** `docs/mcp-tools.md:39` currently pins the tool as the
  `memory.feedback` "subset: `stale`/`refuted` only" with the row description "mark an injected
  memory stale or refuted, with a reason" — update both to include `confirmed` (e.g. "subset:
  `stale`/`refuted`/`confirmed`" and "mark an injected memory stale/refuted, or confirm it is
  still accurate, with a reason") so the shipped catalog doc does not go stale.
  **Authorization caveat (round 14 finding 3):** the paragraph immediately below the table
  (`docs/mcp-tools.md:41-43`) currently states "Any actor whose API key resolves read/write
  capability for the target memory can call them" — which is **false for `engram_memory_feedback`**:
  the endpoint requires the distinct `memories:review` capability (`views.py:60-62`), a read-only
  (or plain read/write) key gets HTTP 403 (proven by `memory_feedback_tests.py:269`), and this is
  true for **all three** actions (`stale`/`refuted`/`confirmed`), not new to confirm. Since this
  slice already edits this doc for the `confirmed` action, correct the paragraph to carve out
  feedback — e.g. append "except `engram_memory_feedback`, which requires the `memories:review`
  capability (a read/write key alone gets 403)" — so the doc does not publish a false authorization
  contract for `confirmed`. `docs/guides/mcp.md:189-191` already describes the `request_id`-per-call
  behavior correctly (see finding 3) and needs no change.
- **Bundle byte-sync (mandatory, finding 6):** do **not** hand-copy files into the generated
  bundle trees. After editing `mcp_server.py`/`mcp_tools.py`, run the canonical lockstep
  generator `python scripts/sync_plugin_bundle.py` — it `rmtree`s and rebuilds **both**
  `packages/claude-plugin/hooks/engram_cli/` and `packages/codex-plugin/hooks/engram_cli/`
  from `packages/cli/engram_cli/`, so it also removes any stale files a manual copy would miss
  (`sync_plugin_bundle.py:40-47`, documented at
  `packages/claude-plugin/README.md:105-107`). Verify with
  `python scripts/sync_plugin_bundle.py --check` (exit 1 on drift, `sync_plugin_bundle.py:31-32,50-60`).
  `bundle_sync_tests.py` still asserts byte-identical copies as the CI gate
  (`packages/claude-plugin/bundle_sync_tests.py:34-38`).

## Data Flow

1. Agent (MCP) calls `engram_memory_feedback` with `action="confirmed"`, a `reason`, and
   `memory_id`. `submit_memory_feedback` validates, builds `_scope_payload`, adds a fresh
   `request_id`, and POSTs `/v1/memories/{memory_id}/feedback`.
2. `MemoryFeedbackView.post` validates the serializer (now accepts `confirmed`), resolves
   scope (`memories:review` — see design decision 8; the default wizard key lacks it and gets
   403, unchanged from stale/refuted today) and project, and calls `RecordMemoryFeedback().execute`.
3. `RecordMemoryFeedback.execute` takes the confirm branch: locks the memory, enforces team
   scope, **checks the audit ledger for this `(actor, request_id)` first** (a hit returns a stable
   `already_applied=True` no-op with the original `confirmed_at`), and only on a new
   `request_id` rejects non-approved/stale/refuted/decay-ineligible **and open-conflict
   (quarantined)** memories, sets `last_confirmed_at` without
   bumping `updated_at`, and writes a team-scoped `MemoryConfirmed` audit event with a
   redacted reason.
4. Later, the scheduled `DecayMemoryConfidence` job selects candidates by
   `GREATEST(updated_at, last_confirmed_at) < cutoff`, then **re-locks and re-checks each row's
   anchor** before decrementing; the just-confirmed memory falls outside the window and is not
   decayed until the confirmation itself ages out, and a confirm racing a decay pass cannot
   lose a decay step to a stale read (finding 4).

## Error Handling

- **Decay-ineligible or quarantined memory confirmed** (non-approved / stale / refuted /
  null-or-at-floor confidence / `digest` / **unresolved open conflict** / **no caller-retrievable
  active RetrievalDocument** (round 12 finding 2 + round 14 finding 1 — memory has no document, only
  stale/refuted documents, or only documents retrieval hard-drops for this caller: `SESSION`/
  `ORGANIZATION`-scoped or `TEAM`-scoped to a team the caller cannot see, so it is never injected),
  on a new `request_id`) →
  `MemoryFeedbackError('memory_not_confirmable', …)` → HTTP 400 via
  `MEMORY_FEEDBACK_STATUS.get(code, 400)` (`views.py:77-81`, `42-44`). Client surfaces the
  `detail` string through `_error_text`. A **replay** of a previously-successful
  `request_id` on a now-stale memory does **not** hit this path — the ledger check precedes
  the guard and returns the original success (finding 1).
- **Wrong / out-of-scope project** (finding 4) → rejected **before** `lock_memory_for_update`:
  the view resolves and authorizes the supplied project first (`views.py:60-76`), so an
  out-of-scope `project_id` returns HTTP **403** `project_scope_denied` (proven by the existing
  `memory_feedback_tests.py:343-372`), **not** 404. Unchanged.
- **Memory not found** (authorized project, memory lookup misses) → `lock_memory_for_update`
  raises `MemoryFeedbackError('memory_not_found', …)` → HTTP **404** (`views.py:43`). Unchanged.
- **Team scope denial** → `ensure_memory_team_scope` raises `AccessDeniedError`
  (`team_scope_denied`) as today; unchanged.
- **Capability denial** (`memories:review` missing) → handled by `resolve_request_scope`
  as today (403). Unchanged.
- **Missing/invalid action** at client → `submit_memory_feedback` returns the friendly
  "requires memory_id, action (stale, refuted, or confirmed), and reason." text without a
  network call.
- **Invalid `action` at API** (e.g. typo) → serializer `ChoiceField` 400.
- **Empty results / idempotent repeat** → not an error: `already_applied=true`, HTTP 200,
  timestamp preserved.

## Test Plan (TDD — failing test first)

Run from a worktree with a unique compose project name. The suites use **two different harnesses
(finding 5)** — the root `app` service bind-mounts only `./apps/backend`
(`docker-compose.yml:15-16`), so `app pytest` can run **only** the backend paths; the CLI and both
plugin bundles are `unittest` suites that live outside that mount and run through the
`deploy/compose` container with `packages/` mounted (as in `packages/cli/README.md:71-79` and
`.github/workflows/backend.yml:104-110`). A single `app pytest -q <all paths>` command cannot
execute the client/bundle gates.

- **Backend (pytest):** the `app` service runs in `/srv/app` with `./apps/backend`
  bind-mounted there (`docker-compose.yml:10,15-16`), so paths are relative to `apps/backend`
  (a leading `apps/backend/` would resolve to the nonexistent `/srv/app/apps/backend/...` and
  fail collection):
  `docker compose -p engram-s5 run --rm app pytest -q engram/memory/memory_feedback_tests.py engram/memory/confidence_decay_tests.py engram/core/migrations_tests.py`
  (the `engram/core/migrations_tests.py` path is **required** in the gate — finding 3 — because the
  migration reverse-guard cases that verify the fail-closed `0041` durability behavior live there,
  not in the feedback/decay suites; omitting it lets the checkpoint pass while leaving the reverse
  guard uncollected and unverified)
- **CLI (unittest):** the image installs `curl`, not Git (`apps/backend/Dockerfile:9-12`), while
  the suite shells out to `git` directly (`cli_lifecycle_tests.py:2727-2730`), so the read-only Git
  mounts from the canonical command (`packages/cli/README.md:71-79`) are **mandatory** — omitting
  them fails the repo-url tests on a fresh image:
  ```bash
  docker compose -f deploy/compose/docker-compose.yml run --rm --no-deps \
    -v "$PWD:/workspace" -w /workspace \
    -v /usr/bin/git:/usr/bin/git:ro \
    -v /usr/lib/git-core:/usr/lib/git-core:ro \
    -e PYTHONPATH=/workspace/packages/cli \
    --entrypoint python3 api -m unittest discover -s packages/cli -p '*_tests.py' -v
  ```
- **Bundles (unittest, one per bundle):** the same container form (including the read-only Git
  mounts — the bundles vendor a byte-identical copy of `engram_cli`, so discovery picks up the
  same git-shelling suites) with
  `PYTHONPATH=/workspace/packages/claude-plugin -s packages/claude-plugin` and, separately,
  `PYTHONPATH=/workspace/packages/codex-plugin -s packages/codex-plugin`
  (`.github/workflows/backend.yml:107-110`).

### Backend — serializer + service (API-level; mocks, per view/API rule)

File: `apps/backend/engram/memory/memory_feedback_tests.py` (existing; APIClient-based).

1. `test_memory_feedback_confirmed_sets_last_confirmed_at_and_audit` — POST
   `action=confirmed`; assert 200, `confirmed_at` non-empty, `stale/refuted` false,
   `already_applied` false, `Memory.last_confirmed_at` set, one `MemoryConfirmed`
   `AuditEvent`. (Write first; fails because serializer rejects `confirmed`.)
2. `test_memory_feedback_confirmed_does_not_bump_updated_at` — capture `updated_at` before,
   confirm, assert `updated_at` unchanged and `last_confirmed_at` set.
3. `test_memory_feedback_confirmed_idempotent_same_request_id` — two POSTs, same
   `request_id`; second returns `already_applied=true`, `last_confirmed_at` unchanged, still
   exactly one `MemoryConfirmed` audit event.
4. `test_memory_feedback_confirmed_new_request_id_refreshes_timestamp` — second POST with a
   new `request_id` advances `last_confirmed_at`; two audit events.
5. `test_memory_feedback_confirmed_rejected_on_stale_memory` — stale memory → 400
   `memory_not_confirmable`, memory unmutated.
6. `test_memory_feedback_confirmed_rejected_on_refuted_memory` — refuted memory → 400
   `memory_not_confirmable`.
7. `test_memory_feedback_confirmed_rejected_on_non_approved_status` (finding 3) — a memory
   with `status` in {`CONFLICT`, `ARCHIVED`, `REFUTED`} but `stale=False, refuted=False` →
   400 `memory_not_confirmable` (guards the status check, not just the flags).
7a. `test_memory_feedback_confirmed_rejected_on_decay_ineligible_memory` (finding 2 + finding 8) —
    parametrized over an APPROVED, non-stale, non-refuted memory that is nonetheless
    decay-ineligible: (a) `kind='digest'`, (b) `confidence=None`, (c) `confidence == floor`
    (`ENGRAM_CONFIDENCE_DECAY_FLOOR`), (d) `confidence < floor` — a below-floor legacy/direct row,
    representable because `Memory.confidence` has no minimum constraint (`models.py:726`). Each →
    400 `memory_not_confirmable`, memory unmutated and no `MemoryConfirmed` audit event (guards
    that the confirm guard mirrors the full decay filter, not just status/flags). Case (d)
    specifically guards against an equality-only (`confidence == floor`) implementation that would
    accept a below-floor row and violate the stated `confidence <= floor` contract.
7b. `test_memory_feedback_confirmed_allowed_when_org_decay_disabled` (finding 3) — for an
    organization with `OrganizationSettings.confidence_decay_enabled=False`, confirming an
    otherwise-eligible approved memory still returns 200 with `last_confirmed_at` set and one
    `MemoryConfirmed` audit event. Guards that the per-org decay gate is deliberately **not** part
    of the per-memory confirmability guard (confirm is a durable receipt independent of whether
    decay is currently running).
7c. `test_memory_feedback_confirmed_rejected_on_open_conflict` (round 10 finding 1) — open an
    unresolved `MemoryConflict` against an otherwise-eligible APPROVED, non-stale, non-refuted,
    above-floor memory (via `OpenMemoryConflict`, or by creating an unresolved `MemoryConflict`
    row for the memory), then POST `action=confirmed`; assert 400 `memory_not_confirmable`, the
    memory is unmutated (`last_confirmed_at` still NULL), and **no** `MemoryConfirmed` audit event
    is written. This guards the deliberate divergence from the raw decay filter: the memory passes
    every decay predicate (so a "mirror decay exactly" guard would wrongly accept it) but is
    retrieval-quarantined (`context/services.py:315-322`), so confirm must reject it. Then resolve
    the conflict (`resolved_transition` set) and assert the same memory becomes confirmable
    (200, `last_confirmed_at` set) — proving the guard keys on the **unresolved** conflict, not the
    mere historical existence of a `MemoryConflict` row.
7d. `test_memory_feedback_confirmed_rejected_when_no_caller_retrievable_document` (round 12
    finding 2 + round 14 finding 1) — parametrized over an APPROVED, non-stale, non-refuted,
    above-floor, non-digest, non-conflicted memory that is nonetheless **not retrieval-injectable to
    the caller**: (a) the memory has **no** `RetrievalDocument` at all; (b) the memory's only
    `RetrievalDocument` has `stale=True` (and, separately, `refuted=True`) while the memory's own
    `stale`/`refuted` flags remain `False`; and (c) **document-visibility cases (round 14 finding 1)**
    — the memory's only active (`stale=False, refuted=False`) `RetrievalDocument` has
    `visibility_scope=SESSION`, `visibility_scope=ORGANIZATION`, or `visibility_scope=TEAM` with a
    `team_id` **not** in the caller's `scope.team_ids`, each of which retrieval hard-drops via
    `filter_documents_by_team_visibility` (`context/services.py:280-292`) even though a bare
    `.exists()` would accept it; and (d) **denormalized-scope-drift case (round 15 finding 1)** —
    the memory's only otherwise-active (`stale=False, refuted=False`, `PROJECT`-visible)
    `RetrievalDocument` has had its denormalized `organization_id`/`project_id` drifted away from
    the memory's own `organization`/`project` via `RetrievalDocument.objects.filter(id=...).update(...)`
    (the same construction as `invariant_queries_tests.py:1419-1422`, representable because no DB
    constraint couples these fields to the memory — `models.py:865`), which retrieval hard-drops via
    its `organization=`/`project=` filter (`context/services.py:306-308`) even though a filter
    omitting those two clauses would accept it. Each → 400 `memory_not_confirmable`, memory unmutated
    (`last_confirmed_at` still NULL), and **no** `MemoryConfirmed` audit event. Then add an active
    `RetrievalDocument` that retrieval **would** surface to the caller — `visibility_scope=PROJECT`
    (or `TEAM` with the caller's own team) — and assert the same memory becomes confirmable (200,
    `last_confirmed_at` set). Guards the second retrieval-quarantine predicate: a memory that passes
    every memory-field decay predicate but owns no document retrieval would inject **for this caller**
    must be rejected; the check keys on the **document's** `stale`/`refuted`, `visibility_scope`,
    **and denormalized `organization`/`project`** **independently** of the memory's own flags/scope,
    mirroring `authorized_retrieval_documents` (`context/services.py:306-308`) and
    `filter_documents_by_team_visibility` (`context/services.py:280-292`) exactly. Case (d)
    specifically guards against an implementation that omits the `organization=`/`project=` clauses
    and would issue a false confirmation receipt for a drifted document retrieval excludes.
8. `test_memory_feedback_confirmed_replay_after_stale_returns_original_success` (finding 1) —
   confirm with `request_id=R` (200), then mark the memory stale, then re-POST with the same
   `R`; assert 200, `already_applied=true`, `confirmed_at` equal to the first response's
   value (not a 400). Ledger check precedes the state guard.
9. `test_memory_feedback_confirmed_replay_reports_original_timestamp` (finding 1) — confirm
   `R1`, then confirm `R2` (advances `last_confirmed_at`), then replay `R1`; assert the `R1`
   replay's `confirmed_at` equals the first (older) timestamp, not the `R2` timestamp.
10. `test_memory_feedback_confirmed_audit_has_team_and_redacted_reason` (finding 2) — for a
    team-scoped memory (`visibility_scope == TEAM`, `team_id` set), the `MemoryConfirmed` event has
    `team_id == memory.team_id` and `metadata['reason']` is the redacted reason; a reader scoped to
    a different team does not see the event via `ListInspectionAuditEvents` (team isolation, not
    just event count).
10a. `test_memory_feedback_confirmed_audit_visibility_follows_project_scope` (round 12 finding 3) —
    construct a memory with `visibility_scope == PROJECT` but a **non-null** `team_id` FK (the
    reachable `_create_candidate_memory` state, `transitions.py:1045-1053`); confirm it, then assert
    the `MemoryConfirmed` event has `team_id IS None` and that a project reader **outside** that team
    **does** see the receipt via `ListInspectionAuditEvents`. Guards that the audit team scope follows
    `memory.visibility_scope` (project-global for a PROJECT-visible memory), not the raw
    `memory.team_id` FK — otherwise the confirming project reader could not see its own receipt.
11. `test_memory_feedback_stale_response_includes_confirmed_at` (finding 7) — mark a
    previously-confirmed memory `stale` via the feedback endpoint and assert the **stale**
    response body carries `confirmed_at == memory.last_confirmed_at.isoformat()`; then a
    never-confirmed memory's stale response carries `confirmed_at == ""`. This is the only test
    that exercises `MemoryFeedbackResult.to_response` rendering the new field on the
    stale/refuted path (the confirm-path tests cover only the confirm branch, and the client
    tests use handcrafted bodies), so without it an implementation could omit or mis-render the
    backend field while every other listed test passes.
12. `test_memory_feedback_confirmed_isolated_per_actor` (finding 1) — two callers with distinct
    scopes (different `actor_id`) POST `action=confirmed` on the **same** memory with the **same**
    pinned `request_id`; assert **both** succeed with `already_applied=false`, `last_confirmed_at`
    reflects the second confirm, and there are **two** `MemoryConfirmed` audit events (one per
    actor) — proving the ledger key `(actor_type, actor_id, request_id)` prevents cross-caller
    suppression. A same-actor replay of that `request_id` still returns `already_applied=true`.
13. `test_memory_feedback_confirmed_uses_current_at_processing_semantics` (round 12 finding 1) —
    create an eligible memory, advance its content to a new version (bump `current_version` and
    rewrite `body`, which bumps `updated_at`), then POST `action=confirmed` with **no** version
    parameter; assert 200, `last_confirmed_at` set, one `MemoryConfirmed` event — confirm succeeds
    against the **current** row without any observed-version fence (decision 9). Also assert the
    decay anchor after confirm equals `last_confirmed_at` (the confirm instant), i.e.
    `GREATEST(updated_at, last_confirmed_at) == last_confirmed_at` because the confirm runs at
    `t1 > updated_at`: this documents that a since-revised memory's anchor lands on the confirm
    instant — the same anchor a legitimate confirm of the current version would produce at that
    instant, never a future/amplified value. Guards that confirm deliberately carries no
    agent-observed-version fence, matching the stale/refuted server-side-fence behavior.

### Backend — migration reverse guard (round 8, finding 1)

File: `apps/backend/engram/core/migrations_tests.py` (existing; append the new cases here — this
file must **not** live inside the `migrations/` package, because Django's `MigrationLoader`
imports every non-underscore module in that package and raises `BadMigrationError` on any module
lacking a `Migration` class, which would break the migration graph and backend startup. The
existing `migrations_tests.py` at the `core` package level is the established home for migration
executor tests).

Tests 1–3 **must** follow the established schema-migration-test pattern in this same file
(`test_0039_reverse_preserves_..._refuses_import_provenance`
[migrations_tests.py:2942–3008](/mnt/c/Users/filipp/Desktop/gena/_PACKAGES/engram/apps/backend/engram/core/migrations_tests.py),
`test_0040_curation_decision_round_trip_preserves_schema`
[migrations_tests.py:3011–3035](/mnt/c/Users/filipp/Desktop/gena/_PACKAGES/engram/apps/backend/engram/core/migrations_tests.py)),
which is the only shape that actually verifies the guard is wired into the migration graph. A
plain `@pytest.mark.django_db` case that only calls `_guard_reverse(apps, schema_editor)` directly
runs the predicate but never executes the real `0041 → 0040` reversal, never proves the operation
is appended to `0041.operations` in reverse-run order, and — because it shares the transactional
test schema without owning teardown — can leave the schema stranded at `0040` and break every
later test. Each case therefore:

- is `@pytest.mark.django_db(transaction=True)` (real cross-transaction migrate, not the
  wrapping test transaction);
- captures `leaf_nodes = MigrationExecutor(connection).loader.graph.leaf_nodes()` up front and
  restores them **unconditionally** in a `finally:` via
  `MigrationExecutor(connection).migrate(leaf_nodes)`, so a raised `RuntimeError` (tests 2–3)
  cannot leave the shared schema at `0040`;
- drives the reversal through `MigrationExecutor(connection).migrate(MIGRATE_0040)` from the
  `0041` leaf (add `MIGRATE_0041 = [('core', '0041_memory_last_confirmed_at')]` and
  `MIGRATION_0041_NODE` module constants beside the existing `MIGRATE_0040` /
  `MIGRATION_0040_NODE` at [migrations_tests.py:24–31](/mnt/c/Users/filipp/Desktop/gena/_PACKAGES/engram/apps/backend/engram/core/migrations_tests.py)),
  asserting the guard's effect on the real schema (column dropped vs preserved), not just the
  predicate return.

A supplemental direct `_guard_reverse(apps.get_model-backed apps, schema_editor)` assertion may be
added for faster unit-level coverage of the predicate's two arms, but it does **not** substitute
for the real reversal in any of tests 1–3.

1. `test_reverse_0041_allowed_when_no_confirmation_history` — migrate up to the `0041` leaf; with
   zero `MemoryConfirmed` `AuditEvent`s and every `Memory.last_confirmed_at` NULL, `migrate(MIGRATE_0040)`
   succeeds; assert `last_confirmed_at` is absent from `apps_0040.get_model('core', 'Memory')._meta.fields`
   (column dropped).
2. `test_reverse_0041_blocked_when_last_confirmed_at_set` — a `Memory` with `last_confirmed_at`
   set → `pytest.raises(RuntimeError, match='0041')` around `migrate(MIGRATE_0040)`, and after the
   raise assert the column still exists on the `0041` state and the row's `last_confirmed_at` is
   intact (guard rolled back, no data loss).
3. `test_reverse_0041_blocked_when_memoryconfirmed_ledger_exists` — a surviving `MemoryConfirmed`
   `AuditEvent` (even with all `last_confirmed_at` NULL, the exact orphan-then-silent-restore
   scenario) → `pytest.raises(RuntimeError, match='0041')` around `migrate(MIGRATE_0040)`. Guards
   that the ledger arm of the fail-closed predicate, not just the column arm, is enforced.

### Backend — decay interplay

File: `apps/backend/engram/memory/confidence_decay_tests.py` (existing; DB-level).

7. `test_recently_confirmed_memory_is_not_decayed` — memory aged in `updated_at` (>min age)
   but `last_confirmed_at = now`; run `DecayMemoryConfidence().execute()`; confidence
   unchanged. (Fails until the `Greatest` anchor lands.)
8. `test_unconfirmed_aged_memory_still_decays` — control: aged `updated_at`,
   `last_confirmed_at=None`; decays by one step (guards against the anchor accidentally
   excluding NULL-confirmed rows).
9. `test_decay_rechecks_anchor_under_lock` (finding 4) — single-threaded: a row that passes the
   initial candidate filter but whose `last_confirmed_at` is advanced to `now` before the
   per-row locked write is **not** decremented (the `select_for_update` +
   `decay_anchor__lt=cutoff` re-check skips it). Exercises the conditional re-check that closes
   the read-then-write race. (Proves the predicate re-check, but **not** the row-lock
   serialization — see test 10.)
10. `test_confirm_and_decay_serialize_on_row_lock` (finding 5 + finding 1, round 6) — real
    concurrency proof that confirm and decay block on the **same** `select_for_update` row lock.
    **A barrier that only synchronizes the two service-call *starts* is insufficient** and is
    explicitly **not** used here: confirm writes only `last_confirmed_at` (`services.py:309-311`)
    while decay writes only `confidence`/`updated_at` (`confidence_decay.py:77-80`), so a broken
    **lockless / no-re-check** decay that reads the candidate, lets a confirm commit, then writes a
    stale decrement anyway produces `confidence` decremented **and** `last_confirmed_at` set — a
    final state **indistinguishable** from the legitimately-permitted "decay-first" outcome. An
    "assert either outcome" test therefore passes even against the exact bug this slice fixes and
    proves nothing. Instead this test **forces the interleaving deterministically** using the
    repository's canonical pause/block lock pattern
    (`transitions_tests.py:218-290`: `@pytest.mark.django_db(transaction=True)`,
    `@pytest.mark.skipif(connection.vendor != 'postgresql', …)`, a monkeypatched pause seam that
    holds the row lock while signalling a `threading.Event`, a second thread asserted to **block**,
    `close_old_connections()` per worker), in two directional cases:

    - **Confirm-holds-lock, decay must block then skip.** Monkeypatch an injectable pause into the
      confirm path that fires **after** `lock_memory_for_update` has acquired the row lock but
      **before** the confirm transaction commits. The seam **must be isolated to the confirm path**:
      wrap `RecordMemoryFeedback._lock_memory` so the pause fires on the value it **returns** (the
      already-locked row, obtained inside `execute`'s `transaction.atomic()`), i.e. patch the bound
      method to call the real `_lock_memory`, then set `confirm_locked` and wait on `release_confirm`
      before returning. **Do not** use `timezone.now()` as the seam (neither the step-4
      `memory.last_confirmed_at = timezone.now()` nor any other `now()`): `services` and
      `confidence_decay` both do `from django.utils import timezone`, so they resolve the **same**
      `django.utils.timezone.now`, and decay evaluates `timezone.now()` at the top of `_decay_project`
      (`confidence_decay.py:61`, computing `cutoff`) **before** it reaches any `select_for_update`.
      A monkeypatch of `services.timezone.now` (or `django.utils.timezone.now`) would therefore also
      pause thread B at line 61 **before the lock**, so `not decay_done.wait(...)` would pass even
      against a lockless decay — the assertion would prove nothing. `_lock_memory` is called only by
      the feedback service (decay locks via its own `Memory.objects.select_for_update()` and never
      calls it), so wrapping it pauses confirm-after-lock without touching decay. So the confirm txn
      sets `confirm_locked` and waits on `release_confirm` while **holding** the lock.
      Thread A runs the real `RecordMemoryFeedback` confirm → pauses holding the lock. Main waits
      `confirm_locked`, then starts thread B running `DecayMemoryConfidence().execute()` against the
      **same** aged, decay-eligible memory. Assert `not decay_done.wait(timeout=1)` — B's per-row
      `select_for_update` is **blocked** on A's lock and cannot complete the trivial single-row
      decay; this is the actual serialization proof (blocking **after** lock acquisition), the same
      assertion shape as `mutation_acquired.wait(timeout=1)` at `transitions_tests.py:277`. Then
      `release_confirm.set()`; A commits (`last_confirmed_at` advanced). B unblocks, re-locks,
      re-evaluates `decay_anchor__lt=cutoff` (now **false**) and **skips**. Deterministic
      assertions: `confidence` **unchanged**, `last_confirmed_at` set, decay result `memories == 0`,
      exactly one `MemoryConfirmed` audit event and **zero** `MemoryConfidenceDecayed`. This proves
      both that decay blocked on the shared lock and that the locked re-check suppressed the stale
      decrement — the precise bug an "either outcome" test misses.
    - **Decay-holds-lock, confirm must block then still record.** Symmetrically, monkeypatch a pause
      into `_decay_project`'s per-row locked write that fires **after** the `select_for_update`
      returns the locked row and the re-check passes but **before** `locked.save()` commits (wrap
      `locked.save`), holding the lock while it sets `decay_locked` and waits on `release_decay`.
      Thread A runs `DecayMemoryConfidence` → pauses holding the lock, about to decrement. Main waits
      `decay_locked`, then starts thread B running the `RecordMemoryFeedback` confirm; assert
      `not confirm_done.wait(timeout=1)` — confirm's `lock_memory_for_update` is **blocked** on A's
      lock. Then `release_decay.set()`; A commits exactly one decrement. B unblocks and sets
      `last_confirmed_at`. Deterministic assertions: `confidence` decremented by **exactly one**
      step, `last_confirmed_at` set, one `MemoryConfidenceDecayed` and one `MemoryConfirmed` audit —
      no double decrement, no lost confirmation.

    Between the two directional cases the row is decremented **at most once** and `last_confirmed_at`
    is always set, and each case's outcome is **forced and asserted**, not accepted as one of two
    alternatives. This is the evidence the single-threaded test 9 cannot provide.

### Client — enum + passthrough

- `packages/cli/engram_cli/mcp_server_tests.py`:
  `test_memory_feedback_tool_enum_includes_confirmed` — the `engram_memory_feedback`
  inputSchema `action` enum contains `confirmed`.
  **Update the existing exact enum assertion (finding 6):** `test_tools_list_feedback_schema`
  (`mcp_server_tests.py:139-142`) asserts the `action` enum equals `["stale", "refuted"]`
  verbatim; it must be updated in lockstep to `["stale", "refuted", "confirmed"]` or it is a
  guaranteed red test.
  **Update the existing exact-description assertion (finding 7 + finding 5):** the description-map
  test at `mcp_server_tests.py:164-167` asserts the full `engram_memory_feedback` description string
  verbatim. Because step "Client changes" rewrites that description, this assertion **must** be
  updated in lockstep to the **single authoritative final string** given in "Client changes" above —
  which **inserts** the confirm sentence and **preserves** the `Clean memory improves…` closing
  sentence: "Step 3 - close the loop: the moment you discover an injected or retrieved memory is
  outdated or wrong, mark it stale or refuted with a reason. Confirm a memory when you have verified
  it is still accurate — this resets its confidence decay clock. Clean memory improves every future
  session; do not silently ignore bad memory." Leaving it unchanged is a guaranteed red test.
- `packages/cli/engram_cli/mcp_tools_tests.py`:
  `test_submit_memory_feedback_passes_confirmed_action` — with a stub/fake transport,
  `action="confirmed"` is accepted and the posted payload carries `action=confirmed`;
  `test_submit_memory_feedback_rejects_unknown_action` — an unknown action still returns
  the friendly validation string with no request.
  **Update the existing action-validation assertion (finding 6):** `test_feedback_validates_action`
  (`mcp_tools_tests.py:426-434`) asserts the rejection text contains the substring
  `stale or refuted`, which the new wording (`stale, refuted, or confirmed`) removes; this
  assertion must be updated to the new substring or it is a guaranteed red test.
  **Render coverage (finding 7):** the existing `test_feedback_posts_and_renders`
  (`mcp_tools_tests.py:436-455`) does not assert `confirmed_at`. Add
  `test_submit_memory_feedback_confirmed_renders_confirmed_at` — a `confirmed` response body with
  `confirmed_at="2026-07-19T10:00:00+00:00"` renders `confirmed_at=2026-07-19T10:00:00+00:00`
  in the success line — and `test_submit_memory_feedback_stale_renders_empty_confirmed_at` — a
  `stale` response body with `confirmed_at=""` renders `confirmed_at=` (the `''` null
  representation), guarding both the populated and null rendering paths of the extended success
  line (`mcp_tools.py:367-372`).

### Bundle sync

- `packages/claude-plugin/bundle_sync_tests.py` and
  `packages/codex-plugin/bundle_sync_tests.py` — existing byte-match tests; must pass after
  syncing `mcp_server.py` + `mcp_tools.py` into both bundles.

## Out of Scope

- Confidence boosts on confirm (v1 records the timestamp only).
- Auto-restore / auto-unstale of stale or refuted memories (console decision).
- Batch confirm across multiple memories.
- Console UI for confirmation.
- Any change to the `MemoryTransition` contract or `transition_contract_version` defaults.
- A CLI `feedback` subcommand (none exists; MCP-only).
- Re-scoping feedback authorization (design decision 8): widening the interactive wizard's
  default capabilities or minting a dedicated `memories:confirm` capability. The endpoint keeps
  requiring `memories:review` for all feedback actions; keys used for feedback must be separately
  provisioned with it (unchanged from the existing stale/refuted contract). Follow-up.

## Review Reconciliation

(append-only; initially empty)

- Round 1: reviewer (Codex) could not be invoked (configuration error: "failed to
  load configuration: No such file or directory"). Zero numbered findings were
  produced, so there is nothing to fix or refute for this round. Spec unchanged.

- Round 2, finding 1: fixed — reordered confirm path so the audit-ledger lookup precedes the
  state guard (replay survives a later stale/refuted), and the replay's `confirmed_at` is read
  from the original event's `metadata['confirmed_at']` not the mutated memory row; the lighter
  (no fingerprint-collision) guarantee vs the transition contract is documented and accepted
  because confirm mutates only a timestamp.
- Round 2, finding 2: fixed — `MemoryConfirmed` audit event now sets `team_id=memory.team_id`
  (null team would leak a team-scoped confirmation as project-global per
  `inspection/services.py:52-53`) and stores a redacted `reason` in metadata, matching the
  transition-path audit (`transitions.py:1248-1261,379-386`); added a team-isolation test.
- Round 2, finding 3: fixed — confirm guard now requires `status == MemoryStatus.APPROVED`
  (plus `not stale and not refuted`), exactly mirroring the decay eligibility filter, so
  noncanonical `ARCHIVED`/`REFUTED`/`CONFLICT` rows with false flags are no longer confirmable;
  added a non-approved-status rejection test.
- Round 2, finding 4: fixed — decay now re-locks each candidate (`select_for_update`) and
  re-evaluates `decay_anchor < cutoff` inside a per-row transaction before decrementing, so a
  confirm committing between the candidate read and the write cannot lose a decay step;
  confirm and decay serialize on the same row lock. Added a locked-anchor re-check test.
- Round 2, finding 5: fixed — spec now states plainly that the MCP inputSchema exposes neither
  `request_id` nor `correlation_id` and that each MCP invocation generates a fresh
  `request_id` (a deliberate re-confirm therefore refreshes the decay clock, which is
  intended); `request_id` idempotency is scoped as an API-contract guarantee for HTTP callers
  and exercised by backend tests, and v1 does not add these fields to the MCP schema.
- Round 2, finding 6: fixed — corrected the efficiency claim: the `GREATEST` filter uses the
  `(organization, project, status)` index prefix but not the `updated_at` range component; the
  residual per-project scan over the approved set is explicitly accepted for a low-frequency
  scheduled batch, with an expression index noted as a future option.

- Round 3: reviewer output contained no numbered findings — only a status message that the
  background Codex review task (ID `bzolepptv`) had not completed when the output was captured.
  Nothing to fix or refute for this round. Spec unchanged.

- Round 4, finding 1 (blocker): fixed — audit metadata reason changed from the transition-path
  form `str(redact_value(data.reason).value)[:1024]` to `str(redact_value(data.reason))[:1024]`;
  this module's local `redact_value` (`services.py:698-699`) already returns the unwrapped
  value, so the `.value` access would have raised `AttributeError` and rolled back every first
  confirm. Documented the wrapper divergence from `transitions.py:380`.
- Round 4, finding 2 (major): fixed — confirm guard extended to mirror the **full** decay filter
  (`confidence_decay.py:71-74`): now also rejects `confidence is None`, `confidence <= floor`,
  and `kind == 'digest'`, so no decay-ineligible approved row can trigger a meaningless
  audit/timestamp write; added rejection test 7a.
- Round 4, finding 3 (major): fixed — corrected the overstated freshness claim: the MCP schema
  has no `additionalProperties: false`, the server forwards undeclared args
  (`mcp_server.py:232`), and `_new_request_id` honors a caller-supplied `request_id`
  (`mcp_tools.py:395-397`, per `docs/guides/mcp.md:189-191`). Reframed freshness as a
  convention; a pinned key only makes the caller's own repeats idempotent no-ops (no corruption,
  timestamp-only mutation under lock+ledger), so v1 accepts it rather than hardening the shared
  feedback path.
- Round 4, finding 4 (major): fixed — clarified the replay-body contract: only `action`,
  `already_applied`, and the original `confirmed_at` are stable across replays;
  `stale`/`refuted`/`team_id` render from the live memory row (`services.py:209-210`), so a
  confirm→stale→replay truthfully reports `stale: true`. Idempotency is scoped to the confirm
  receipt, not unrelated state flags.
- Round 4, finding 5 (major): fixed — added a real threaded serialization test
  (`test_confirm_and_decay_serialize_on_row_lock`) modeled on `transitions_tests.py:181-206`
  (transaction=True, postgres-only skipif, barrier, ThreadPoolExecutor) proving confirm and
  decay block on the same row lock; kept the single-threaded re-check test 9 and labeled its
  narrower scope.
- Round 4, finding 6 (minor): fixed — bundle sync now mandates
  `python scripts/sync_plugin_bundle.py` (+ `--check`) instead of hand-copying, matching the
  canonical lockstep generator (`sync_plugin_bundle.py:40-47`, README:105-107) that rebuilds
  both bundles and removes stale files.
- Round 4, finding 7 (minor): fixed — added the required client test updates: the exact
  description assertion at `mcp_server_tests.py:164-167` must be updated in lockstep, and two new
  render tests cover `confirmed_at` (populated) and the `''` null representation for
  stale/refuted responses.
- Round 4, finding 8 (minor): fixed — corrected the false justification: `AuditResult.ALLOWED`
  **is** used by write-side memory audits (`services.py:1069,1180`); `RECORDED` is chosen for
  semantic alignment with the mutation-audit peers (`MemoryTransitionCommitted` `transitions.py:1258`,
  decay batch), not because `ALLOWED` is unavailable.
- Round 4, finding 9 (minor): fixed — added a client-change bullet to update the stale public
  catalog doc `docs/mcp-tools.md:39` (feedback subset + row description) to include `confirmed`;
  noted `docs/guides/mcp.md:189-191` is already accurate.
- Round 4, finding 10 (minor): fixed — corrected the "un-indexable" claim: a JSON metadata key
  is expression-indexable in Postgres but materially worse (untyped `->>` extraction + per-compare
  `::timestamptz` cast, non-ORM-native, manual NULL/format handling); the typed nullable column
  is retained as the simpler design.

- Round 5, finding 1 (major): fixed — the confirm audit ledger now keys on
  `(actor_type, actor_id, request_id)` instead of `request_id` alone, so two distinct authorized
  callers pinning the same `request_id` no longer collide (each gets its own confirm, audit
  attribution and receipt); the false "no cross-caller interference" claim is replaced with an
  enforced per-actor isolation guarantee, and test 12 proves it. Verified 403/404 and MCP
  request-id behavior against `mcp_tools.py:394-397`.
- Round 5, finding 2 (major): fixed — decay anchor changed to
  `Greatest('updated_at', Coalesce('last_confirmed_at', 'updated_at'))` in both the candidate
  filter and the locked re-check, with `Coalesce` imported. Verified SQLite is a supported config
  (`settings.py:44-55`, `test_settings.py:10-23`, and `VectorField` degrades to `None` so the
  Memory table exists on SQLite); without `Coalesce`, `Greatest`'s scalar-`MAX` NULL semantics on
  SQLite would stop every never-confirmed row from decaying. Backend-neutral anchor documented.
- Round 5, finding 3 (major): fixed — resolved the ambiguity by ruling: the per-org
  `confidence_decay_enabled` gate (`confidence_decay.py:39-46`) is deliberately **not** part of the
  confirmability guard; a memory in a decay-disabled org is still confirmable (durable receipt
  independent of whether decay runs). "Full decay eligibility filter" is scoped to the per-row
  predicate; added rejection/allow test 7b and clarified wording.
- Round 5, finding 4 (major): fixed — Error Handling corrected: an out-of-scope `project_id` is
  rejected **before** the lock and returns HTTP 403 `project_scope_denied` (proven by
  `memory_feedback_tests.py:343-372`), not 404; the 404 path is now scoped to an authorized
  project whose memory lookup misses.
- Round 5, finding 5 (major): fixed — the test plan no longer prescribes a single
  `app pytest` command for everything: backend paths run via `app pytest`, and the CLI + both
  bundles run via the `deploy/compose` `unittest discover` invocations
  (`packages/cli/README.md:71-79`, `.github/workflows/backend.yml:104-110`), since the root `app`
  service mounts only `apps/backend` (`docker-compose.yml:15-16`).
- Round 5, finding 6 (minor): fixed — added the two existing client assertions that the changes
  break: `test_tools_list_feedback_schema` (`mcp_server_tests.py:139-142`, enum `["stale","refuted"]`)
  and `test_feedback_validates_action` (`mcp_tools_tests.py:426-434`, substring `stale or refuted`)
  must both be updated in lockstep, not merely supplemented.
- Round 5, finding 7 (minor): fixed — added backend test 11 asserting `confirmed_at` on the
  stale/refuted response body (populated from `last_confirmed_at`, `""` when never confirmed),
  closing the coverage gap where no backend or client test exercised
  `MemoryFeedbackResult.to_response` rendering the new field on the stale/refuted path.
- Round 5, finding 8 (minor): fixed — extended test 7a with a `confidence < floor` (below-floor)
  case; `Memory.confidence` has no minimum constraint (`models.py:726`), so an equality-only
  (`== floor`) implementation would accept a below-floor row and violate the `confidence <= floor`
  contract. The below-floor case now guards the inequality.

- Round 6, finding 1 (major): fixed — redesigned test 10. Verified the reviewer's claim against
  the code: confirm writes only `last_confirmed_at` (`services.py:309-311`) and decay only
  `confidence`/`updated_at` (`confidence_decay.py:77-80`), so a lockless/no-re-check decay's stale
  write is indistinguishable from the permitted decay-first outcome, and the barrier-only
  "assert either outcome" design proves nothing. Replaced it with the repository's deterministic
  pause/block lock pattern (`transitions_tests.py:218-290`): two directional cases that force one
  side to hold the row lock, assert the other **blocks** (`not …_done.wait(timeout=1)`), then assert
  the forced outcome (confirm-first → decay skips via locked re-check, confidence unchanged;
  decay-first → exactly one decrement then confirm records). Dropped the barrier reference.
- Round 6, finding 2 (minor): fixed — backend pytest paths corrected from
  `apps/backend/engram/memory/...` to `engram/memory/...`; the `app` service runs in `/srv/app`
  with `./apps/backend` bind-mounted there (`docker-compose.yml:10,15-16`), so the old paths
  resolved to `/srv/app/apps/backend/...` and failed collection.
- Round 6, finding 3 (minor): fixed — added the mandatory read-only Git mounts
  (`-v /usr/bin/git:/usr/bin/git:ro -v /usr/lib/git-core:/usr/lib/git-core:ro`) to the CLI gate,
  matching the canonical command (`packages/cli/README.md:71-79`); the image installs `curl` not
  Git (`Dockerfile:9-12`) while the suite shells to `git` (`cli_lifecycle_tests.py:2727-2730`).
  Noted the bundle gates need the same mounts since they vendor a byte-identical `engram_cli` copy.

- Round 7, finding 1 (major): fixed — the confirm-holds-lock seam in test 10 no longer wraps
  `timezone.now()`. Verified the defect: `services` and `confidence_decay` both do
  `from django.utils import timezone` and resolve the same `django.utils.timezone.now`, and decay
  computes `cutoff = timezone.now()` at `confidence_decay.py:61` **before** any `select_for_update`,
  so a monkeypatch of `services.timezone.now` would also pause thread B pre-lock and let
  `not decay_done.wait(...)` pass against a lockless decay. Replaced the seam with an isolated
  wrap of `RecordMemoryFeedback._lock_memory` (pauses on its returned, already-locked row); decay
  locks via its own `Memory.objects.select_for_update()` and never calls `_lock_memory`, so the
  seam pauses confirm-after-lock without touching decay, restoring the serialization proof.

- Round 8, finding 1 (major): fixed — added a fail-closed reverse guard to migration `0041`.
  Verified the defect against code: `AuditEvent` is an independent table (`core/models.py:1057`)
  untouched by the `AddField`, so a reverse drops `last_confirmed_at` while `MemoryConfirmed`
  receipts survive, and a later replay hits the surviving ledger and returns success without
  restoring the anchor — silent decay resurrection, explicitly kept in scope by the operator
  directive. Fix mirrors the existing in-repo precedent `0039_import_provenance.py:7-10,102`:
  append `migrations.RunPython(migrations.RunPython.noop, _guard_reverse)` where `_guard_reverse`
  raises `RuntimeError` if any `Memory.last_confirmed_at` is set **or** any `MemoryConfirmed`
  `AuditEvent` exists; reverse on a clean history is an allowed no-op. Added three migration
  reversal tests (empty history allowed; column-set blocked; ledger-only blocked).

- Round 9, finding 1: fixed — the migration reverse-guard test file was specified inside the
  `migrations/` package (`migrations/0041_memory_last_confirmed_at_tests.py`), where Django's
  `MigrationLoader` imports every non-underscore module and raises `BadMigrationError` on any
  lacking a `Migration` class, breaking migrations and backend startup; relocated the three cases
  to the existing `apps/backend/engram/core/migrations_tests.py`, the established home for
  `MigrationExecutor` tests, matching the in-repo precedent for `0033`/`0038`/`0039`/`0040`.

- Round 10, finding 1 (major): fixed — verified the reviewer's claim against code:
  `OpenMemoryConflict` leaves the memory row `status=APPROVED, stale=False, refuted=False` and
  creates a separate unresolved `MemoryConflict` row (`transitions.py:2387-2461`,
  `_lock_declared_memories` locks the memory at `transitions.py:728`), proven by
  `curation_tests.py:886-890`; nothing writes `status=CONFLICT` to a memory, so the guard's
  `status==APPROVED` check was inert against real conflicts, and the raw decay filter
  (`confidence_decay.py:65-74`) actually *does* decay open-conflict rows while retrieval
  quarantines them (`context/services.py:315-322`). Corrected the false "status check rejects
  CONFLICT" claim and added an explicit open-conflict rejection clause
  (`MemoryConflict.objects.filter(memory_id=memory.id, resolved_transition__isnull=True).exists()`,
  the single-row form of the retrieval quarantine, evaluated under the confirm row lock). This is
  the one deliberate place confirm is **stricter** than the raw decay filter (decay-eligible AND
  retrieval-injectable): a quarantined memory is never injected so no agent could have verified it,
  and refreshing the decay clock of a disputed memory is the wrong signal. Added test 7c
  (rejected while conflict unresolved; confirmable once resolved). Decay's own treatment of
  conflicted rows is unchanged (out of scope).
- Round 10, finding 2 (major): fixed — verified the capability gap: the feedback endpoint requires
  `memories:review` (`views.py:60`) while the interactive wizard mints keys with only
  `memories:read`/`observations:write`/`search:query` (`commands.py:57`, pinned by
  `cli_lifecycle_tests.py:2929-2937`), so a default wizard key gets 403. Made the decision
  explicit (design decision 8 + Data Flow + Out of Scope): this is **pre-existing and unchanged by
  S5** — the endpoint already gates stale/refuted on `memories:review` today and the wizard
  already omits it — so confirm inherits the identical auth contract; keys used for any feedback
  action must be separately provisioned with `memories:review`. Rejected both alternatives for v1:
  widening the wizard default (grants every plug-and-play agent org-wide memory review/mutation, a
  security regression larger than this slice) and a new `memories:confirm` capability (fragments
  the endpoint's single `required_capability`, needs a backend authz change, leaves stale/refuted
  still on `memories:review`). Re-scoping feedback authorization is a cross-cutting change to the
  existing feedback surface, recorded as a follow-up.

- Round 11, finding 1 (major): fixed — confirmed against code: the live eligibility query
  excludes `kind='digest'` (`confidence_decay.py:74`) and `Memory.kind` is re-synced from
  `metadata` on every `save()`, appended to `update_fields` even when the caller omits it
  (`core/models.py:781,784-785`), so a selected row flipped to `kind='digest'` before lock
  acquisition would otherwise still be decremented by the locked re-check. Added
  `.exclude(kind='digest')` to the locked re-check query (finding 4 block) so the re-check
  repeats the full eligibility predicate and digests are skipped even after a mid-flight kind
  change. Steady-state correctness, not a deploy artifact.

- Round 12, finding 1 (major): fixed — verified against code that confirm's two sibling feedback
  actions are **not** observed-version-fenced either: `RecordMemoryFeedback._apply_transition` builds
  `build_memory_fence(memory)` from the **server-side just-locked row** (`services.py:294`, memory
  locked at `services.py:226`), not from an agent-supplied version — a same-request concurrency guard,
  not an "agent saw this version" assertion. Chose the reviewer-offered resolution: added design
  decision 9 explicitly adopting **current-at-processing** semantics for confirm (consistent with
  stale/refuted), with the rationale that confirm writes `last_confirmed_at = now` and the anchor is
  `GREATEST(updated_at, last_confirmed_at)`, so any confirm lands the anchor on the processing instant
  and never beyond — a stale-read confirm produces the identical anchor a legitimate current-version
  confirm would produce at the same instant, gaining no extra decay life, and corrupts no stored
  assertion (confirm writes only a timestamp, no content/flags/boost). Added test 13. An
  observed-version fence across the feedback surface is out of scope.
- Round 12, finding 2 (major): fixed — verified the reviewer's claim: retrieval requires an
  `RetrievalDocument` with the **document's own** `stale=False, refuted=False`
  (`context/services.py:309-313`, filtered independently of the memory's flags), and no constraint
  guarantees an approved memory owns an active document (`core/models.py:713,865`), so a legacy/
  noncanonical approved non-stale non-refuted memory with no document (or only stale/refuted
  documents) passed the guard yet is never injectable — a false "verified" receipt + decay reset,
  the exact defect the round-10 open-conflict clause exists to prevent. The confirm guard's stated
  "retrieval-injectable" requirement enforced only the open-conflict predicate, half of the retrieval
  quarantine. Added the second predicate:
  `RetrievalDocument.objects.filter(memory_id=memory.id, stale=False, refuted=False).exists()` (updated
  design decision 6, the service step-3 guard, error handling, and the error message to
  "…retrieval-injectable memory"); document visibility was claimed not to need re-checking (team-scope
  guard already ran) — **that claim was wrong and is corrected by round 14 finding 1** (retrieval
  hard-drops SESSION/ORGANIZATION documents and keys on the document's scope, which the team-scope
  guard does not cover). Added test 7d (no document, stale-only document, refuted-only document all
  rejected; becomes confirmable once an active document exists — extended in round 14 with the
  document-visibility cases).
- Round 12, finding 3 (major): fixed — verified the reachable state: `_create_candidate_memory`
  stores `team_id = candidate.team_id` while taking `visibility_scope` from the (possibly
  collapsed-to-PROJECT) `scope_override` (`transitions.py:1045-1053`), and `Memory` has no constraint
  coupling `team_id` to `visibility_scope` (`core/models.py:716,720-724,761-769`), so a PROJECT-visible
  memory can retain a non-null `team_id`. Retrieval exposes its PROJECT document to every project
  reader (`context/services.py:287-288`) while inspection hides a non-null-team event from readers
  outside that team (`inspection/services.py:52-53`) — so keying the audit on raw `memory.team_id`
  hides the confirming project reader's own receipt. Changed the `MemoryConfirmed` audit to
  `team_id = memory.team_id if memory.visibility_scope == VisibilityScope.TEAM else None` (VisibilityScope
  already imported, `services.py:38`), which resolves **both** the round-2 leak (TEAM memory keeps
  its team scope) and this asymmetry (PROJECT memory gets a project-global receipt visible to exactly
  the audience that can confirm it). Added test 10a. The identical latent asymmetry in the shipped
  `MemoryTransitionCommitted` audit (`transitions.py:1251`) is a cross-cutting realignment left out of
  scope; confirm is written correctly from the start.
- Round 12, finding 4 (major): fixed — verified the repo posture: `select_for_update()` is a no-op on
  SQLite (Django docs) and every existing row-lock concurrency test is postgres-only skipif
  (`transitions_tests.py:181-182`), as is this slice's test 10. Chose the reviewer-offered resolution:
  added an explicit "Backend scope of the row-lock serialization guarantee" note scoping the
  block-and-recheck ordering guarantee to PostgreSQL (the deployed + pgvector test backend), and
  documenting that on SQLite the DB-level write locking gives serializable isolation so the
  read-then-write decay race surfaces as a fail-loud `SQLITE_BUSY`/`OperationalError`, never a silent
  lost decrement, while the idempotency ledger (SELECT+INSERT) is backend-neutral for the
  sequential-replay contract (**the "needs no row-level lock / identical on both backends" wording was
  too strong for the concurrent case and is corrected by round 14 finding 2**). No SQLite concurrency
  test added — SQLite is never run under concurrent load; the `Coalesce`/`Greatest` decay-query
  neutrality (decision 3) remains separately tested.
- Round 12, finding 5 (minor): fixed — resolved the description ambiguity by pinning the **single
  authoritative final string** in "Client changes": the confirm sentence is **inserted** after
  "…mark it stale or refuted with a reason." and the existing "Clean memory improves…" closing
  sentence is **preserved**. Updated both the "Client changes" bullet and the description-test-update
  bullet to quote the identical complete string, so the verbatim `mcp_server_tests.py:164-167`
  assertion has one unambiguous target ("append vs insert vs replace" no longer yields three
  constants).

- Round 13, finding 1 (major): fixed — the reviewer is mathematically correct that the round-12
  decision-9 sub-bullet was false: with a revision at `t0` and a stale-read confirm at `t1 > t0`,
  `GREATEST(updated_at, last_confirmed_at)` moves the anchor from `t0` to `t1`, so the confirm does
  extend decay by `t1 − t0` — it is NOT a no-op, and "cannot extend beyond the content change" was
  wrong. Verified the mechanics against code: revision bumps `updated_at` via `auto_now`
  (`core/models.py:20`, written by `_advance_memory_pointer` `transitions.py:1295-1308`), while
  feedback carries no observed version (`serializers.py:37-39`, `services.py:184-193`). Corrected the
  spec rather than adding a version fence: replaced the false "no-op" argument in decision 9 with the
  accurate and stronger one — confirm can only write `now`, so the anchor lands on the processing
  instant `t1` and **never beyond**, and a stale-read confirm produces the *identical* anchor a
  legitimate current-version confirm would produce at the same instant (no amplification, no unbounded
  extension, no gain from staleness). This is the accepted current-at-processing semantics (decision
  9), which the operator/round-12 disposition already adopts; confirm still writes only a timestamp
  (no content/flags/confidence), so it corrupts no stored assertion. Also corrected test 13's anchor
  assertion (now asserts `GREATEST == last_confirmed_at` at `t1 > updated_at`, not the prior misleading
  "content bump already moved the anchor") and the round-12 reconciliation entry to match. An
  observed-version fence across the feedback surface remains out of scope for this additive slice.

- Round 14, finding 1 (major): fixed — the reviewer is correct that the round-12-finding-2
  active-document predicate `RetrievalDocument…filter(stale=False, refuted=False).exists()` plus the
  claim "document visibility need not be re-checked" **overstated** the retrieval-injectability
  guarantee (superseding that clause of the round-12-finding-2 entry above). Verified against code:
  retrieval's surfaceable set is produced by `filter_documents_by_team_visibility`
  (`context/services.py:280-292`), which appends **only** documents whose **own** `visibility_scope`
  is `PROJECT`, or `TEAM` with `team_id in scope.team_ids`, and **hard-drops** every `SESSION`/
  `ORGANIZATION` document (`VisibilityScope` carries both, `core/models.py:52-56`; promotion persists
  the candidate scope verbatim absent an override, `transitions.py:1440-1455`). The team-scope guard
  (`ensure_memory_team_scope`, `services.py:838-844`) does **not** cover this: it rejects only `TEAM`
  memories with an unauthorized team (lets `SESSION`/`ORGANIZATION` through) and keys on the
  **memory's** scope, not the **document's** (they can diverge, no coupling constraint). So a memory
  whose only active document is `SESSION`/`ORGANIZATION`-scoped, or `TEAM`-scoped to an unseen team,
  passed both the guard and the bare `.exists()` yet is never injected — a false "verified" receipt.
  Fixed by mirroring `filter_documents_by_team_visibility` exactly in the confirm predicate
  (`…filter(Q(visibility_scope=PROJECT) | Q(visibility_scope=TEAM, team_id__in=scope.team_ids)).exists()`,
  `Q` added to the `django.db.models` import, `VisibilityScope` already imported), updated decision 6,
  the service step-3 guard, error handling, and extended test 7d with the three document-visibility
  rejection cases (SESSION / ORGANIZATION / unseen-TEAM) plus the PROJECT/own-TEAM allow case.
- Round 14, finding 2 (major): fixed — the reviewer is correct that "The idempotency ledger … needs
  no row-level lock and behaves identically on both backends" was too strong. Verified: `AuditEvent`
  has no uniqueness constraint on the ledger identity (`core/models.py:1057`) and SQLite is a
  supported (degraded) config (`settings.py:41`), where `select_for_update` is a no-op. Corrected the
  round-12-finding-4 note to state accurately: the ledger's serialization rides on the **enclosing
  memory row lock** (`lock_memory_for_update`, acquired before the ledger lookup in step 5); the
  **sequential-replay** contract (the guarantee the API promises and the tests exercise) is genuinely
  backend-neutral (plain `SELECT`, no lock), while the **concurrent** same-`(actor, request_id)` case
  is PostgreSQL-only — on SQLite a genuinely concurrent same-request confirm surfaces **fail-loud** as
  `SQLITE_BUSY`, never a silent double-apply, and even a hypothetical double-write leaves the receipt
  uncorrupted (replay returns the first-by-`(created_at, id)` event). No design change needed
  (PostgreSQL is the deploy + pgvector test backend); a spec-accuracy correction, not a weakening —
  the concurrency guarantee is now scoped honestly, matching decay's block-and-recheck scoping in the
  same note.
- Round 14, finding 3 (minor): fixed — verified the reviewer's claim: the spec directed updating only
  the `engram_memory_feedback` table row in `docs/mcp-tools.md`, leaving the following paragraph
  (`docs/mcp-tools.md:41-43`) asserting "Any actor whose API key resolves read/write capability … can
  call them", false for feedback — the endpoint requires the distinct `memories:review` capability
  (`views.py:60-62`) and a read-only key gets 403 (`memory_feedback_tests.py:269`). Since this slice
  already edits that doc, extended the Public-docs bullet to also correct the paragraph (carve out
  `engram_memory_feedback` as requiring `memories:review`), so the doc does not publish a false
  authorization contract for `confirmed`. The inaccuracy is pre-existing (stale/refuted already need
  `memories:review`); the fix is bundled here because the doc is already in this slice's edit set.

- Open question for reviewers: the team brief said "mirror the MarkMemoryStale/RefuteMemory
  transition pattern". This spec deliberately keeps confirm OUT of `_execute_memory_state`
  (no `MemoryTransition`/`RetrievalDocument` write) because confirmation changes no
  retrievable content or state — routing it through the transition service would write a new
  retrieval document per confirm. Confirm still reuses the same lock/team-scope/audit/
  idempotency discipline inside `RecordMemoryFeedback`. Flagged for explicit accept/deny.

- Round 15, finding 1 (major): fixed — the active-document guard filtered `RetrievalDocument` by
  `memory_id` only, while retrieval (`authorized_retrieval_documents`, `context/services.py:306-308`)
  filters by the document's denormalized `organization`/`project`. A document whose denormalized scope
  drifted from its memory (Python-only validation, no DB constraint, exercised by `QuerySet.update` in
  `invariant_queries_tests.py:1414`) would be counted by confirm but not surfaced by retrieval — a
  false receipt that resets decay. Added `organization=memory.organization, project=memory.project`
  to the guard filter so it mirrors retrieval exactly, and documented the drift-rejection in the
  clause text.
- Round 15, finding 2 (major): refuted:very-rare-edge-case — the leak requires a `PROJECT`-visible
  memory whose only active document is `TEAM`-scoped, i.e. document `visibility_scope` diverging from
  memory `visibility_scope`. Not reachable via live code: the sole `RetrievalDocument` writer sets
  `document.visibility_scope = memory.visibility_scope` (`projections.py:101,161`) and stales all
  prior documents (`projections.py:179`), and `Memory.visibility_scope` is set once at promotion
  (`transitions.py:1057`) with no rescope path mutating it — so the one active document always shares
  the memory's scope, and the memory-scoped audit key admits exactly the document-scoped retrieval
  audience. Unlike finding 1's org/project drift (which retrieval defends against with an invariant
  test), no live path or invariant test treats a visibility_scope divergence as a defended state.
  Added the scope-agreement invariant to the audit-scope discussion as supporting justification (no
  authorization/audit logic changed).
- Round 15, finding 3 (minor): fixed — the backend gate command ran only the feedback and decay
  suites, but the required migration reverse-guard cases live in `engram/core/migrations_tests.py`,
  so the checkpoint could pass without collecting the fail-closed `0041` durability tests. Appended
  `engram/core/migrations_tests.py` to the backend gate command and noted why its inclusion is
  mandatory.
- Round 16, finding 1 (minor): fixed — the design's confirm guard already filters the active
  document on `organization=memory.organization, project=memory.project` (spec:486-487), but test 7d
  covered only missing/stale-refuted/visibility cases, so an implementation omitting those two
  clauses would pass every planned test while issuing a false confirmation receipt and resetting
  decay for a denormalized-scope-drifted document retrieval excludes (drift constructable via
  `RetrievalDocument.objects.filter(id=...).update(...)`, no coupling DB constraint —
  `invariant_queries_tests.py:1419-1422`, `models.py:865`; retrieval defends at
  `context/services.py:306-308`). Added drift case (d) to test 7d and updated its closing rationale
  to require the org/project clauses.

- Round 17, finding 1 (major): fixed — confirmed against the file: the two existing
  schema-migration tests (`test_0039_reverse_...` migrations_tests.py:2942–3008,
  `test_0040_..._round_trip` :3011–3035) both use `@pytest.mark.django_db(transaction=True)`,
  drive real reversal through `MigrationExecutor(connection).migrate(...)`, and restore
  `leaf_nodes` unconditionally in `finally`. The migration-reverse-guard section only required a
  plain `@pytest.mark.django_db` and allowed direct `_guard_reverse` invocation as the primary
  shape, which runs the predicate but never executes the `0041→0040` reversal, never proves the
  guard is appended to `0041.operations`, and can strand the shared transactional schema at `0040`.
  Rewrote tests 1–3 to require `transaction=True`, real `MigrationExecutor` reversal from the
  `0041` leaf (with new `MIGRATE_0041`/`MIGRATION_0041_NODE` constants), unconditional `finally`
  leaf restoration, and schema-effect assertions (column dropped vs preserved + row intact on the
  rollback path); direct `_guard_reverse` demoted to supplemental-only coverage.
