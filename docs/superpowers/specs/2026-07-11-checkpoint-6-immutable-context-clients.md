# Checkpoint 6: Immutable Context And Complete Client Lifecycle

**Status:** implementation-ready design against `master` at `79ddb15a5d872f963c6464847c800b798d78caef`.

**Roadmap authority:** Checkpoint 6 in `2026-07-09-autonomous-memory-loop-roadmap.md`, especially C6.1 through C6.4, P10, P14, and fault F18.

**Goal:** every authorized context request creates or replays one bounded, fingerprint-compatible, immutable context artifact, while the canonical CLI and the Claude/Codex adapters capture every required lifecycle signal without coupling capture success to recall success.

## Delivery Boundary

Checkpoint 6 is four serial slices:

1. C6.1 adds request identity, immutable bundle/item snapshots, and explicit replay conflicts.
2. C6.2 replaces permissive packing with a strict whole-artifact budget and preserves exact authorized results when semantic retrieval degrades.
3. C6.3 freezes one closed lifecycle contract in the canonical CLI for the two supported runtimes.
4. C6.4 keeps both plugin packages thin, synchronizes generated connector modules, and proves the runtime contracts in fixture and E2E tests.

C6.1 and C6.2 share the context service and therefore merge serially. C6.3 starts only after the backend response/error contract is frozen. Claude, Codex, documentation, and E2E owners may then work in disjoint files. One integration owner alone runs bundle generation and Git operations.

This specification chooses typed bundle/item snapshots over one opaque response JSON blob. Existing relational ids, citations, scope evidence, `rendered_text`, and warnings remain useful and queryable, while only fields that are currently read from mutable rows are duplicated.

The strict counter remains the existing deterministic `ceil(len(text) / 4)` estimator. Adding a model tokenizer would add a runtime dependency, make budgets model-specific, and still not preserve old bundles across tokenizer upgrades. “Exact” in C6 means that the stored whole artifact never exceeds its declared budget under the versioned Engram counter.

The client contract is a closed Claude/Codex mapping, not a runtime registry, base adapter hierarchy, callback bus, local queue, or generic agent framework.

## Preconditions

- Checkpoints 1 through 5 are merged in roadmap order.
- CP4 exposes immutable `MemoryVersion` rows and coherent current retrieval documents; C6 does not repair split semantic state.
- CP5 exposes canonical open-conflict evidence; context warnings must query that authority rather than treating `MemoryLink` as canonical.
- The C1.3 shared session-end/reactivation primitive is the only server writer used by terminal hooks.
- The native Codex harness represented by `2026-07-09-codex-harness.md` is merged or explicitly handed to the C6 client owner. The baseline already contains its four-event manifest and isolated real-Codex E2E.
- The implementation branch is refreshed from the post-CP5 `master`. The numeric migration prefix is resolved from that actual leaf before the first migration RED is written.

If any prerequisite interface differs after the earlier checkpoints merge, the C6 spec owner refreshes names without weakening the contracts below. C6 does not copy or bypass an earlier checkpoint primitive.

## Locked Invariants

1. Authorization and project/team resolution occur before candidate reads, ranking, packing, snapshot lookup disclosure, or replay.
2. `(organization, project, request_id)` still names at most one bundle.
3. A request id is reusable only when its canonical request fingerprint is identical and the stored snapshot is currently authorized.
4. Mutable memory, policy, warning, and repository state never alter a committed snapshot.
5. Every v1 item names the selected immutable `MemoryVersion` and contains the exact redacted fields returned on replay.
6. Every v1 bundle stores a SHA-256 of its exact UTF-8 `rendered_text` bytes.
7. For a non-null budget, `rendered_token_count <= token_budget` always holds.
8. The budget covers the complete `rendered_context`, including headings, separators, truncation markers, empty-state text, and rendered warnings.
9. The JSON envelope and item metadata are not part of the injection budget; clients must inject server `rendered_context` verbatim and must not rebuild a second context body from `items`.
10. Semantic/provider failure cannot remove an already-authorized exact match.
11. A capture failure cannot suppress an independently authorized recall, and a recall failure cannot erase an accepted capture.
12. Replaying one native hook occurrence is idempotent; two occurrences of the same prompt or command remain distinct when the runtime supplies distinct occurrence ids.
13. Generated plugin connector modules byte-match the canonical CLI source.
14. Runtime-specific fields exist only at the manifest/formatter boundary.

## Non-Goals

- No CP7/CP8 repository snapshot, impact plan, temporal revalidation, memory delta, branch universe, or revision-addressable historical API.
- No CP9 retrieval-query consolidation or benchmark-driven optimization.
- No historical bundle reconstruction. Legacy rows lack enough original request and rendered-item evidence to backfill truthfully.
- No new memory store, client-side database, durable retry queue, worker, summarizer, vector index, or background process.
- No general client SDK, transport abstraction rewrite, hook registry, or third runtime.
- No changes to MCP tool count or MCP authorization semantics.
- No production execution, SSH, deployment, D2 work, bulk repair, or destructive migration.

## C6.1: Canonical Request Identity

### Fingerprint constants and encoding

Add these constants in new `apps/backend/engram/context/request_identity.py`:

```python
CONTEXT_REQUEST_FINGERPRINT_VERSION = 1
CONTEXT_SNAPSHOT_SCHEMA_VERSION = 1
CONTEXT_RETRIEVAL_POLICY_VERSION = 'cp6-v1'
CONTEXT_TOKEN_COUNTER_VERSION = 'chars4-v1'
```

The canonical JSON byte sequence is:

```python
json.dumps(
    payload,
    sort_keys=True,
    separators=(',', ':'),
    ensure_ascii=False,
).encode('utf-8')
```

The semantic document includes `fingerprint_version=1`; `sort_keys=True` determines serialized key order. The lowercase hexadecimal SHA-256 input is the ASCII domain separator `engram-context-request-v1\n` followed by those JSON bytes. No Python `repr`, UUID object, unordered set, database timestamp, or locale-dependent value enters the hash.

### Exact fingerprint inputs

The canonical payload contains these keys and no others:

```json
{
  "fingerprint_version": 1,
  "purpose": "session_start|user_prompt_submit|task",
  "agent": {
    "runtime": "claude_code|codex|unknown",
    "version": "validated string",
    "external_id": "resolved explicit id or <runtime>:default",
    "session_id": "native session id"
  },
  "scope": {
    "principal_identity_id": "resolved identity uuid",
    "organization_id": "uuid",
    "project_id": "resolved uuid",
    "team_id": "resolved uuid or null",
    "project_ids": ["sorted uuid strings"],
    "team_ids": ["sorted uuid strings"],
    "capabilities": ["memories:read"],
    "required_capability": "memories:read"
  },
  "repository": {
    "url": "canonical project repository URL",
    "root": "validated request value",
    "branch": "validated request value",
    "revision": "validated opaque revision or empty string",
    "cwd": "validated request value"
  },
  "retrieval": {
    "query": "exact validated string",
    "file_paths": ["exact values in request order"],
    "symbols": ["exact values in request order"],
    "kinds": ["sorted unique values"],
    "limit": 5,
    "token_budget": 2000
  },
  "policy": {
    "retrieval_version": "cp6-v1",
    "token_counter_version": "chars4-v1",
    "hybrid_retrieval_enabled": true,
    "lexical_fusion_enabled": false,
    "lexical_recall_enabled": false,
    "require_provenance": false,
    "embedding_policy_id": "uuid or null",
    "embedding_policy_version": 1,
    "embedding_provider": "provider or empty string",
    "embedding_model": "model or empty string",
    "embedding_resolution": "configured|missing|disabled"
  }
}
```

`repository_revision` is a new optional request field with maximum length 255. The CLI always supplies `git rev-parse HEAD` when the workspace is a Git checkout; direct older callers may send an empty string. Empty revision is fingerprinted explicitly and adds warning `repository_revision_missing`. C6 does not interpret the revision or create a repository-state row.

The configured embedding policy id/version/provider/model and deterministic `configured|missing|disabled` resolution enter the policy snapshot before retrieval. Secret availability, provider health, and provider output do not. Secret/provider recovery therefore replays a degraded bundle; adding or changing configured policy creates a different fingerprint.

The following are intentionally excluded: `request_id` (the lookup key), raw API key, API-key/credential id, unrelated capabilities, `correlation_id`, `trace_id`, request timestamp, provider call id, current candidate/memory ids, current warning state, and provider success/failure. The stable principal identity remains bound while key rotation and unrelated grants do not create false conflicts; correlation and trace ids remain per-attempt metadata.

The raw canonical payload exists only in process memory. Before persistence it is passed through the existing recursive `redact_value`; the redacted result is stored as `request_snapshot`. The fingerprint still binds the exact original validated values, so redaction cannot merge two requests.

### Identity interfaces

`request_identity.py` exports only:

```python
@dataclass(frozen=True, slots=True)
class ContextRetrievalPolicySnapshot:
    retrieval_version: str
    token_counter_version: str
    hybrid_retrieval_enabled: bool
    lexical_fusion_enabled: bool
    lexical_recall_enabled: bool
    require_provenance: bool
    embedding_policy_id: uuid.UUID | None
    embedding_policy_version: int | None
    embedding_provider: str
    embedding_model: str
    embedding_resolution: str

@dataclass(frozen=True, slots=True)
class ContextRequestIdentity:
    fingerprint: str
    request_snapshot: dict[str, object]
    authorization_scope: dict[str, object]
    repository_state: dict[str, str]
    policy_snapshot: dict[str, object]

def resolve_context_retrieval_policy(
    organization: Organization,
    project: Project,
    team: Team | None,
) -> ContextRetrievalPolicySnapshot: ...

def build_context_request_identity(
    data: ContextBundleInput,
    *,
    scope: EffectiveScope,
    project: Project,
    team: Team | None,
    policy: ContextRetrievalPolicySnapshot,
) -> ContextRequestIdentity: ...
```

This module may call the existing organization settings and model-policy resolvers; it normalizes a known missing-policy result into `embedding_resolution=missing` rather than failing identity construction. It does not query memories, call a provider, persist a row, or know about Claude/Codex response formats.

## C6.1: Immutable Snapshot Schema

### `ContextBundle` additions

Add:

| Field | Type | v1 meaning |
|---|---|---|
| `snapshot_schema_version` | positive small integer, database default `0` | `1` for all C6 writer rows; `0` means legacy/unreplayable |
| `request_fingerprint` | nullable `CharField(64)` | canonical lowercase SHA-256; null only on legacy rows |
| `request_snapshot` | JSON, default object | redacted canonical identity payload |
| `rendered_sha256` | `CharField(64)`, blank default | hash of exact UTF-8 `rendered_text` |
| `rendered_token_count` | positive integer, default `0` | `estimate_tokens(rendered_text)` |

Keep `authorization_scope`, `token_budget`, `rendered_text`, `selected_count`, `status`, `metadata`, and `retrieval_latency_ms`. Store the original correlation and trace ids under `metadata.request_correlation`; echo the current attempt ids from `ContextBundleResult`, so a replay can be correlated without mutating the snapshot.

Add constraints:

- `snapshot_schema_version IN (0, 1)`;
- version 0 permits absent identity fields;
- version 1 requires non-null fingerprint, non-empty rendered hash, non-empty request snapshot, and non-empty authorization scope;
- `token_budget IS NULL OR rendered_token_count <= token_budget`.

Keep the existing unique constraint on organization/project/request id. Add a non-unique organization/project/fingerprint index for diagnostics; fingerprint does not replace request id as the idempotency key.

### `ContextBundleItem` additions

Add:

| Field | Type | v1 meaning |
|---|---|---|
| `snapshot_schema_version` | positive small integer, database default `0` | `1` for C6 items |
| `memory_version` | nullable FK to `MemoryVersion`, `PROTECT` | exact selected immutable version; null only for legacy |
| `title_snapshot` | `CharField(255)`, blank default | redacted title returned by the API |
| `body_snapshot` | text, blank default | redacted full selected `MemoryVersion.body` returned by the API |
| `confidence_snapshot` | nullable decimal matching `Memory.confidence` | selected confidence |
| `kind_snapshot` | `CharField(40)`, blank default | selected kind |
| `validity_snapshot` | JSON, default object | exact eligibility facts below |
| `rendered_block` | text, blank default | exact full or bounded block embedded in bundle text |
| `representation` | `CharField(20)`, blank default | `full` or `truncated` for v1 |

The v1 `validity_snapshot` has exactly:

```json
{
  "eligibility": "current",
  "transition_contract_version": "0|1", "current_transition_id": "uuid or null",
  "current_memory_version_id": "uuid", "projection_contract_version": "0|1",
  "exact_projection_hash": "sha256 or empty",
  "memory_status": "approved",
  "memory_stale": false,
  "memory_refuted": false,
  "retrieval_document_stale": false,
  "retrieval_document_refuted": false
}
```

Open CP5 conflicts are withheld or represented as bundle warnings before packing; they are not mislabeled as a current selected item. CP8 may introduce a new snapshot schema version rather than silently adding temporal meanings to v1.

Keep `memory`, `retrieval_document`, rank, citation, inclusion reason, scope evidence, and metadata. Store score and redacted matched terms in existing metadata. Add a DB check allowing version 0 or requiring v1 memory version, non-empty validity/scope evidence/rendered block, and `full|truncated` representation; extend `clean()` so the version's memory and retrieval document match item scope.

### Migration contract

Create one additive core migration named `context_bundle_immutable_snapshot`, with the first numeric prefix after the actual CP5 leaf. It adds the fields, checks, and index above. The migration:

- leaves every historical bundle/item at schema version 0;
- does not copy current mutable memory fields into historical snapshots;
- does not synthesize a request fingerprint from incomplete metadata;
- does not delete, rerender, or mark a historical row injected;
- is schema-reversible on a development database that has no v1 rows.

A request that collides with a version-0 bundle returns HTTP 409 code `context_snapshot_legacy_conflict` and detail instructing the caller to use a new request id. It never serves the legacy live joins. After v1 writes exist, rollback is behavior-forward: retain the additive schema and deploy a reader that understands v1. Dropping immutable evidence is not a rollback.

## C6.1: Create And Replay Transactions

`BuildContextBundle.execute` uses this order:

1. Resolve API-key scope with `memories:read`.
2. Resolve and authorize project, then resolved team.
3. Resolve the policy snapshot and build the request identity.
4. Look up organization/project/request id without reading response bodies.
5. If present, authorize its persisted scope evidence, validate schema/hash/ budget, compare fingerprints with `hmac.compare_digest`, then replay.
6. If absent, authorize candidate documents, apply eligibility, rank, and pack.
7. In one transaction, lock selected document/memory/version rows in UUID order, recheck scope, CP4 current transition/version/projection hash, and eligibility, create the final bundle/items, and write the retrieval audit.
8. Return a result reconstructed only from persisted v1 fields.

The transaction creates the bundle in final form. It does not create a `created` placeholder and later update `rendered_text`; a crash before commit leaves no bundle, item, or retrieval audit. A detected selected-row change raises an internal retryable `ContextSnapshotChanged` before any snapshot row is committed. The service retries retrieval once, then returns a bounded 409 `context_snapshot_changed` rather than looping.

Two concurrent creators may perform duplicate read/provider work, but only one snapshot commits under the existing unique constraint. The loser catches the post-rollback `IntegrityError`, loads the winner, and runs the same authorization and fingerprint checks. Equal fingerprints replay; unequal fingerprints return the same 409 as a sequential collision. No second audit or item survives.

Replay authorization reads persisted `authorization_scope` and item `scope_evidence`, never mutable retrieval-document visibility. Project items require the resolved project; team items require the stored team in current effective team ids; session items additionally require the same external session id; unknown evidence fails closed. Authorization happens before a fingerprint mismatch response so foreign callers cannot probe snapshot content.

Replay integrity checks:

- schema version is 1;
- every item is v1 and names a memory version;
- SHA-256 of stored `rendered_text.encode('utf-8')` equals `rendered_sha256`;
- recomputed token count equals `rendered_token_count`;
- the stored budget constraint holds;
- selected count equals persisted item count and ranks are contiguous.

Failure returns HTTP 500 code `context_snapshot_corrupt`, writes an error audit with ids but no content, and never recomputes or partially returns context.

Fingerprint mismatch returns HTTP 409 code `context_request_conflict` with the stable body keys `code`, `error_code`, and `detail`. It does not return the old or new fingerprint, bundle body, selected ids, or authorization snapshot.

### Response additions

Keep every current response key and add:

```json
{
  "request_fingerprint": "sha256-hex",
  "snapshot_schema_version": 1,
  "rendered_sha256": "sha256-hex",
  "token_budget": 2000,
  "rendered_token_count": 321,
  "replayed": false,
  "correlation_id": "current-attempt-id",
  "trace_id": "current-attempt-id",
  "retrieval": {
    "strategy": "exact|hybrid|lexical_recall|exact_degraded|filter_only",
    "semantic_attempted": true,
    "semantic_status": "succeeded|unavailable|disabled|not_needed",
    "selected_count": 2,
    "dropped_for_budget": 1,
    "truncated_for_budget": 0
  }
}
```

On replay only `replayed`, correlation id, and trace id describe the current attempt. All artifact fields, items, warnings, and retrieval metadata come from the first committed snapshot.

`ContextBundleResult` becomes:

```python
@dataclass(frozen=True, slots=True)
class ContextBundleResult:
    bundle: ContextBundle
    items: tuple[ContextBundleItem, ...]
    replayed: bool
    correlation_id: str
    trace_id: str
```

`to_response()` exposes item `validity`, `representation`, and existing ids/reasons from snapshot fields. It never reads `Memory.title`, `Memory.body`, `Memory.confidence`, `Memory.kind`, current flags, or current warnings.

## C6.2: Retrieval Outcome And Exact Degradation

Replace the four-value `_rank_matches` tuple with:

```python
@dataclass(frozen=True, slots=True)
class RetrievalOutcome:
    matches: tuple[RetrievalMatch, ...]
    strategy: str
    semantic_attempted: bool
    semantic_status: str
    embedding_result: EmbeddingCallResult | None
```

The retrieval order is fixed:

1. query only `authorized_retrieval_documents` for the resolved scope;
2. remove semantically ineligible/current-conflict rows;
3. compute and sort exact matches deterministically;
4. stop with `filter_only` when the request has no query/path/symbol terms;
5. stop with `exact` when exact matches fill `limit`;
6. stop with `exact` and semantic status `disabled` when hybrid retrieval is disabled;
7. otherwise attempt the resolved embedding policy once;
8. on success, run the existing semantic and optional lexical legs;
9. on a `missing` policy snapshot, `ModelPolicyError`, or `ProviderSecretError`, return the unchanged exact tuple as `exact_degraded` and add `semantic_unavailable`;
10. let unexpected programming/database errors fail the request rather than disguising them as degradation.

The degraded warning is emitted whenever the semantic leg was required and unavailable, even when one or more exact matches exist. Its stable public text is `semantic retrieval unavailable; exact authorized matches returned`. Provider error details remain in redacted logs/provider-call records, not in the context body.

The fallback performs no second document query. Exact results, stale/refuted warning scans, and conflict warning scans all consume the already-resolved organization/project/team boundary or an equivalently scoped queryset. No foreign id may appear in an item, warning, drop count, or audit sample.

## C6.2: Strict Whole-Artifact Packing

Move rendering and packing helpers from `services.py` into new `apps/backend/engram/context/packing.py`. Export:

```python
@dataclass(frozen=True, slots=True)
class SnapshotCandidate:
    match: RetrievalMatch
    memory_version: MemoryVersion
    title: str
    body: str
    confidence: Decimal | None
    kind: str
    validity: dict[str, object]
    scope_evidence: dict[str, str]

@dataclass(frozen=True, slots=True)
class PackedContextItem:
    candidate: SnapshotCandidate
    citation: str
    rendered_block: str
    representation: str

@dataclass(frozen=True, slots=True)
class PackedContext:
    items: tuple[PackedContextItem, ...]
    rendered_text: str
    rendered_sha256: str
    rendered_token_count: int
    warnings: tuple[RetrievalWarning, ...]
    dropped_for_budget: int
    truncated_for_budget: int

def estimate_tokens(text: str) -> int: ...

def pack_context(
    candidates: tuple[SnapshotCandidate, ...],
    *,
    purpose: str,
    limit: int,
    token_budget: int | None,
    warnings: tuple[RetrievalWarning, ...],
) -> PackedContext: ...
```

`estimate_tokens` retains `(len(text) + 3) // 4`. Tests call it the Engram counter, not an exact provider tokenizer.

Stable rendering strings are:

```text
# Engram context

- [M1] <title><optional kind/confidence annotation>
  <body>

> Warnings:
> - [<code>] <message>
```

The session-start no-match text remains `# Engram context\n\nNo approved memory matched this request.`; prompt-submit no-match remains empty unless a warning fits. If the session stub cannot fit after warnings it is omitted whole, never cut mid-line. Warning code is rendered so a degraded response remains actionable.

Packing is deterministic:

1. Consider only the first `limit` ranked candidates.
2. Build the complete structured warning list. Budget warnings are recalculated until dropped/truncated counts stabilize; the loop is bounded by `limit + 1` because kept count only decreases.
3. Reserve space for complete warning header/lines in stable warning order. Structured warnings are never deleted; a warning gains `rendered: false` when its complete line cannot fit.
4. Greedily consider candidates in rank order with contiguous citations based on kept order.
5. Keep a full block when the whole artifact remains within budget.
6. If the first kept candidate does not fit, attempt one bounded representation that preserves citation and as much redacted title/body as possible plus `… [truncated for budget]`.
7. If the minimum `- [M1] [truncated for budget]` representation cannot fit, omit the candidate.
8. A later oversized candidate is omitted; continue scanning so a smaller lower-ranked candidate may still fit without reordering kept items.
9. Add `budget_truncated` with the memory id for a bounded first item and `budget_dropped` with the authorized dropped count.
10. Hash and count the final string, then assert the budget before persistence.

Warning order is `budget_truncated`, `budget_dropped`, `semantic_unavailable`, `repository_revision_missing`, stale/refuted warnings, then canonical CP5 conflict warnings. Existing caps remain unless CP5 replaces them with a stricter bound. `RetrievalWarning.to_dict()` keeps `code`, `message`, and `memory_id`, and adds boolean `rendered`.

`token_budget=None` preserves current item-count behavior: keep up to `limit`, render every structured warning, store the count, and never truncate. Every canonical client request supplies a positive budget.

## C6.3: Closed Shared Lifecycle Contract

Add `packages/cli/engram_cli/lifecycle.py`. It is a small immutable table for two runtimes, not an extension mechanism:

```python
@dataclass(frozen=True, slots=True)
class LifecycleRoute:
    runtime: str
    native_event: str
    hook_command: str
    endpoint: str
    canonical_event_type: str
    recall_path: str | None = None
    completion_scope: str | None = None
    forced_tool_outcome: str | None = None

def lifecycle_route(
    runtime: str,
    native_event: str,
    hook_command: str,
) -> LifecycleRoute: ...

def stable_native_event_id(
    route: LifecycleRoute,
    input_payload: dict[str, object],
    canonical_payload: dict[str, object],
) -> str: ...

def classify_tool_outcome(
    route: LifecycleRoute,
    input_payload: dict[str, object],
) -> str: ...
```

No runtime may override endpoint, event type, or response formatter through hook input.

### Claude Code matrix

| Native event | CLI command | Server disposition | Recall |
|---|---|---|---|
| `SessionStart` | `session-start` | capture `session_start` | `/v1/context/session-start` |
| `UserPromptSubmit` | `user-prompt-submit` | capture `user_prompt_submit` | optional `/v1/context/user-prompt-submit` after current query heuristic |
| `PostToolUse` | `post-tool-use` | capture `post_tool_use`, `tool_outcome=success` | none |
| `PostToolUseFailure` | `post-tool-use` | capture `post_tool_use`, `tool_outcome=failure` | none |
| `Stop` | `session-end` | capture `session_end`, `completion_scope=turn`, `completion_reason=stop` | none |
| `SessionEnd` | `session-end` | capture `session_end`, `completion_scope=session`, preserve native reason | none |

`PreToolUse`, `PermissionRequest`, `Notification`, `SubagentStop`, `TeammateIdle`, `TaskCompleted`, configuration, and worktree events have the documented disposition `ignored_by_checkpoint_6`: none is needed to satisfy capture/recall or terminal/failure coverage, and none appears in the manifest. Direct legacy CLI `error` and `decision` commands remain callable but are not native Claude manifest routes.

### Codex matrix

| Native event | CLI command | Server disposition | Recall |
|---|---|---|---|
| `SessionStart` | `session-start` | capture `session_start` | `/v1/context/session-start` |
| `UserPromptSubmit` | `user-prompt-submit` | capture `user_prompt_submit` | optional `/v1/context/user-prompt-submit` |
| `PostToolUse` | `post-tool-use` | capture `post_tool_use`; classify success/failure from `tool_response` | none |
| `Stop` | `session-end` | capture `session_end`, `completion_scope=turn`, `completion_reason=stop` | none |

The pinned Codex harness exposes no native `Error`, `Decision`, or `SessionEnd`; those names remain absent. A nonzero integer `exit_code`, explicit `success=false`, or non-empty `error` in a dictionary `tool_response` is failure. `stderr` alone is not failure. Later activity may reactivate a session ended by turn-scoped Stop through the C1.3 primitive.

### Stable occurrence identity

Explicit `event_id`, `idempotency_key`, and `content_hash` still win. Otherwise stable material includes canonical event type, runtime, session id, request id, payload schema version, sequence number, redacted canonical payload and observation, resolved project/team, native hook name, native `turn_id`, native `tool_use_id`, repository URL/root/revision/branch/cwd, and agent external id.

`event_id = 'engram-cli-' + sha256(canonical stable material)`. The default idempotency key is event id. The content hash uses the same canonical material. Replaying identical native input is stable. Codex repeated turns differ by `turn_id`; repeated tool calls differ by `tool_use_id`; Claude tool success and failure differ by native hook name/tool-use id. A runtime payload that supplies none of its documented occurrence ids remains content-idempotent; C6 does not invent a persistent local sequence.

Context request id is explicit `request_id` when present, otherwise `<event_id>:context:<purpose>`. Correlation id is explicit when present, otherwise event id. Capture and recall for one dual-leg hook therefore share a stable correlation id but retain independent server records.

`repository_revision` is copied from native input or derived once with `git -C <repository_root-or-cwd> rev-parse HEAD`. Failure yields the explicit empty value and warning; it never blocks capture or recall.

### Capture and recall independence

`run_hook` treats dual-leg hooks as two attempts:

```text
capture attempt -> record capture result
recall decision -> recall attempt even when capture failed
format successful recall, or empty fail-open response
emit each failed leg with its own code/correlation/remediation
exit 0 to the runtime
```

Capture still runs first so accepted prompt/session evidence precedes ordinary recall, but non-2xx capture no longer raises before recall. Recall non-2xx does not change an accepted capture response or retry capture. Transport retry stays bounded at the existing two attempts and 10-second per-call timeout.

Use leg-specific diagnostic codes `context_capture_failed` and `context_recall_failed`, retaining the nested server code and detail. Cooldown markers are keyed by server URL, leg, and server code so a capture outage does not hide a later recall error. Add remediation for `context_request_conflict`, `context_snapshot_legacy_conflict`, and `context_snapshot_corrupt` without exposing secrets or fingerprints.

### Runtime response formatting

Before injection the CLI verifies `rendered_sha256` and `rendered_token_count`; mismatch becomes `context_snapshot_corrupt` and no context is injected.

For both runtimes, `hookSpecificOutput.additionalContext` is exactly the server's `rendered_context`. Remove the session-start item re-renderer and its 400-character client truncation. The server now owns citations, truncation, warnings, bytes, and budget.

`--response-format server` continues to return the entire response unchanged. Claude/Codex formatters retain only fields accepted by that runtime. Their bounded `systemMessage` includes selected count, context bundle id, current correlation id, and warning codes. Every warning is also emitted to stderr as one redacted line with code/message/memory id/correlation id; warnings do not change exit status. The `engram_context` MCP tool uses the same request builder, positive default budget, repository revision, hash/count validator, verbatim rendered bytes, and actionable 409 formatting.

When recall succeeds after capture failure, inject the verified context and report capture failure separately. When capture succeeds and recall fails, return `{}` for Claude or `{"continue": true}` for Codex and report recall failure. When a successful response has zero items but non-empty warning-only `rendered_context`, inject that warning context instead of discarding it.

## C6.4: Thin Runtime Adapters

Canonical behavior lives only in `packages/cli/engram_cli`. The plugin hook entrypoints continue to import `engram_cli.main`; no adapter duplicates payload, identity, retry, validation, packing, or formatting logic.

Claude `hooks/hooks.json` adds `PostToolUseFailure` and `Stop` with the commands in the matrix, timeouts 120 and 60 seconds respectively. Existing events and the SessionStart matcher remain. Its contract test asserts the exact six-event manifest and the intentional ignored-event list.

Codex `hooks/hooks.json` remains the exact four-event native matrix from the active harness. Its contract test continues to reject Error, Decision, and SessionEnd, and now also asserts the tool-response failure fixture and terminal disposition.

After canonical CLI tests pass, the sole bundle owner runs `scripts/sync_plugin_bundle.py` once. Neither runtime owner edits files under `packages/*-plugin/hooks/engram_cli/` by hand. Both drift tests and `scripts/sync_plugin_bundle.py --check` remain mandatory.

## Serial PR Spine And Disjoint File Ownership

### C6.1 backend identity/snapshot owner

Owns only:

- `apps/backend/engram/core/models.py`;
- the single next core migration named `context_bundle_immutable_snapshot`;
- `apps/backend/engram/core/migrations_tests.py` for its migration cases;
- new `apps/backend/engram/context/request_identity.py` and `request_identity_tests.py`;
- new `apps/backend/engram/context/snapshot_tests.py`;
- serialized edits to `apps/backend/engram/context/serializers.py`, `views.py`, and `services.py`.

### C6.2 packing/degradation owner

Starts after C6.1 merges and owns only:

- new `apps/backend/engram/context/packing.py` and `apps/backend/engram/context/packing_tests.py`;
- `apps/backend/engram/context/retrieval_warnings.py` and new `apps/backend/engram/context/retrieval_warnings_tests.py`;
- serialized edits to `apps/backend/engram/context/services.py` and new `degraded_retrieval_tests.py`;
- `apps/backend/engram/memory/invariant_queries.py` and `apps/backend/engram/memory/invariant_queries_tests.py` for P10 only.

### C6.3 canonical client owner

Starts after the backend response is frozen and owns only:

- new `packages/cli/engram_cli/lifecycle.py` and `packages/cli/engram_cli/lifecycle_contract_tests.py`;
- `packages/cli/engram_cli/commands.py`, `http.py`, `mcp_tools.py`, `cli_lifecycle_tests.py`, `hook_payload_tests.py`, `hook_error_cooldown_tests.py`, and `mcp_tools_tests.py` under that same canonical directory;
- no generated plugin copy and no runtime manifest.

### C6.4 adapter and E2E owners

- Claude owner: `packages/claude-plugin/hooks/hooks.json`, `packages/claude-plugin/claude_plugin_contract_tests.py`, `packages/claude-plugin/bundle_sync_tests.py`, `packages/claude-plugin/README.md`, and `scripts/e2e_claude_plugin.py`.
- Codex owner: `packages/codex-plugin/hooks/hooks.json`, `packages/codex-plugin/codex_plugin_contract_tests.py`, `packages/codex-plugin/bundle_sync_tests.py`, `packages/codex-plugin/README.md`, and `scripts/e2e_codex_plugin.py`.
- Cross-runtime fixture owner: new `scripts/e2e_context_client_faults.py` and `scripts/e2e_context_client_faults_tests.py`.
- Bundle owner: `scripts/sync_plugin_bundle.py` and both generated `packages/*-plugin/hooks/engram_cli/` trees, only after canonical green.
- Documentation owner: `docs/api-reference.md`, `docs/search-and-retrieval.md`, `docs/agent-integrations.md`, `docs/guides/cli.md`, `docs/reliability/memory-loop-invariants.md`, and `docs/reliability/memory-loop-fault-matrix.md` after contracts freeze.

No two owners edit `core/models.py`, the core migration graph, `context/services.py`, canonical `commands.py`, or generated bundle trees concurrently. The main integration/Git owner alone stages, commits, pushes, and reconciles generated output.

## Required RED Tests

### C6.1 identity, migration, replay, and F18

Write these failing tests before implementation:

1. `test_context_request_fingerprint_is_canonical_and_binds_all_locked_inputs`.
2. `test_transport_ids_and_provider_availability_do_not_change_fingerprint`.
3. `test_same_request_id_with_changed_query_returns_context_request_conflict`.
4. `test_same_request_id_with_changed_budget_policy_or_revision_conflicts`.
5. `test_context_replay_is_byte_stable_after_memory_and_projection_change`.
6. `test_replay_items_use_snapshots_after_title_body_kind_confidence_change`.
7. `test_context_snapshot_never_packs_or_replays_foreign_memory`.
8. `test_legacy_bundle_collision_returns_context_snapshot_legacy_conflict`.
9. `test_corrupt_render_hash_fails_closed_without_retrieval_recompute`.
10. `test_concurrent_equal_fingerprints_commit_one_bundle_items_and_audit`.
11. `test_concurrent_different_fingerprints_commit_one_and_return_one_conflict`.
12. `test_context_replay_is_byte_stable_authorized_and_budget_exact` faults before item creation and after the first item insert, proves rollback, then mutates current memory and proves exact replay.
13. MigrationExecutor cases prove fresh apply, legacy rows remain version 0, v1 checks reject incomplete evidence, reverse works before v1 writes, and migration freshness is clean.

The replay test compares UTF-8 bytes, hash, exact JSON item snapshots, warnings, selected ids, authorization decision, budget, and audit count. Merely comparing bundle ids is insufficient.

### C6.2 packer and degradation

1. `test_whole_artifact_budget_counts_header_items_and_warnings`.
2. `test_oversized_first_item_is_bounded_and_never_exceeds_budget`.
3. `test_budget_too_small_for_bounded_item_omits_it`.
4. `test_later_oversized_item_is_skipped_and_smaller_item_can_fit`.
5. `test_warning_fixed_point_stabilizes_within_limit_plus_one_passes`.
6. `test_unrendered_warning_remains_in_structured_response`.
7. `test_none_budget_preserves_full_limit_behavior`.
8. `test_unicode_counter_and_sha_are_deterministic`.
9. Replace the current permissive top-match test with an assertion that `rendered_token_count <= 1` and no oversized block escapes.
10. `test_provider_failure_returns_authorized_exact_matches_with_warning`.
11. `test_missing_policy_returns_exact_degraded_without_foreign_scan`.
12. `test_exact_matches_filling_limit_do_not_call_semantic_provider`.
13. `test_degraded_warning_and_exact_snapshot_replay_verbatim_after_recovery`.

### C6.3 canonical lifecycle

1. Exact Claude six-row and Codex four-row route table fixtures.
2. Identical native input yields identical event/content/idempotency ids.
3. Distinct Codex turns/tool ids and distinct Claude tool-use ids do not collapse repeated content.
4. Claude `PostToolUseFailure` maps to `post_tool_use` failure with bounded redacted failure evidence.
5. Codex nonzero/false/error tool responses map to failure while stderr-only remains success.
6. Claude Stop, Claude SessionEnd, and Codex Stop preserve their exact completion scopes and reasons.
7. Capture 500 followed by context 200 still makes both calls and injects the verified response.
8. Capture 202 followed by context 500 preserves capture and returns the runtime's empty fail-open response.
9. Capture and recall failures produce independent leg/correlation diagnostics.
10. Session and prompt formatters use server bytes verbatim, preserve warning diagnostics and ids, and reject hash/count mismatch.
11. Warning-only prompt context is injected; a truly empty response stays empty.
12. Git revision derivation is stable, bounded, and fails to empty without blocking either leg; `engram_context` supplies the default budget/revision, validates the snapshot, and preserves 409/warnings.

### C6.4 package and E2E

1. Claude contract asserts exactly SessionStart, UserPromptSubmit, PostToolUse, PostToolUseFailure, Stop, and SessionEnd with commands/timeouts.
2. Codex contract asserts exactly SessionStart, UserPromptSubmit, PostToolUse, and Stop.
3. Both bundle drift suites byte-compare every canonical runtime module and reject extra/test modules.
4. The real Claude scenario executes one successful tool, one harmless command that exits 7, Stop, and SessionEnd; persisted events prove success/failure and terminal dispositions with stable ids.
5. The pinned real Codex scenario executes success and a harmless nonzero tool result; `PostToolUse.tool_response` proves failure and Stop proves the turn-completion checkpoint.
6. Each real runtime still proves session, prompt, and explicit `engram_context` MCP bytes reached the model, not merely that a hook or tool command was invoked.
7. Supplemental installed-bundle fault E2E returns capture 503/context 200 and capture 202/context 503 for both response formats, proving independent legs.
8. Same request/same fingerprint replays exact bytes; same id/changed query gets the actionable 409 through canonical and bundled CLIs.
9. Isolated homes/profiles remain unchanged outside their temp roots; manifests and output contain no credentials.

Direct hook invocation is acceptable only for the supplemental fault harness. The existing real-runtime E2Es remain the authority that native runtimes emit their documented events.

## Invariant And Observability Contract

C6 converts P10 from unconditional missing observability to a scoped evaluator for schema-v1 bundles. It counts:

- missing fingerprint/request/auth/hash evidence;
- missing item version/snapshot evidence;
- selected-count or contiguous-rank mismatch;
- stored token count above budget;
- version-1 bundles whose stored rendered hash/count fails verification in the replay integrity service.

Any anomaly is `violated` with bounded bundle-id samples. A clean v1 cohort is `healthy`. Version-0 bundles are a separate `legacy_unverifiable_count`; they are never served and remain repair/retention work for CP10. A development gate uses a fresh database and must be healthy; a historical environment may remain `missing_observability` until its legacy count reaches zero and must report that state honestly.

The CP6-owned P14 boundary is proved by the negative scope test before reads, ranking, packing, provider calls, and replay. C6 must not claim that the global P14 evaluator is healthy if earlier/later source-to-sink boundaries still lack evidence.

Add metrics/log fields for context request conflicts, legacy collisions, snapshot corruption, budget drops/truncations, rendered tokens/budget ratio, semantic degradation, capture-leg failures, recall-leg failures, runtime/native event, bundle id, request id, and correlation id. Logs contain no raw request, memory body, warning excerpt, fingerprint payload, or credential.

## Documentation Supersession

After code contracts freeze, update:

- `docs/api-reference.md` for new request/response fields and 409 codes;
- `docs/search-and-retrieval.md` for exact-degraded and strict budget semantics;
- `docs/agent-integrations.md` and `docs/guides/cli.md` for matrices, independent legs, warnings, and remediation;
- both plugin READMEs for the exact native event disposition;
- `docs/reliability/memory-loop-invariants.md` and `memory-loop-fault-matrix.md` for P10/F18 evidence and honest legacy state.

Do not rewrite the Codex harness as a generic client design. Add only a status note if its four-event contract remains unchanged.

## Verification And CI Gate

All Python, CLI, plugin, backend, and E2E tests run in containers once Docker/Compose is available. Host commands are limited to Git, read-only inspection, and native tool/version checks. Do not run concurrent broad suites.

Focused backend gate from the repository root:

```text
docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && pytest -q engram/context/request_identity_tests.py engram/context/snapshot_tests.py engram/context/packing_tests.py engram/context/context_api_tests.py engram/memory/invariant_queries_tests.py engram/core/migrations_tests.py"
docker compose -f deploy/compose/docker-compose.yml run --rm api sh -ec "python manage.py migrate --noinput && python manage.py makemigrations --check --dry-run && python manage.py check"
docker compose -f deploy/compose/docker-compose.yml run --rm api sh -ec "ruff check engram/context engram/core/models.py engram/core/migrations engram/memory/invariant_queries.py && ruff format --check engram/context engram/core/models.py engram/core/migrations engram/memory/invariant_queries.py"
```

Containerized canonical/package gate:

```text
docker run --rm --mount type=bind,src="$PWD",dst=/workspace,readonly -w /workspace -e PYTHONPATH=packages/cli python:3.12-slim python -m unittest discover -s packages/cli -p "*_tests.py" -v
docker run --rm --mount type=bind,src="$PWD",dst=/workspace,readonly -w /workspace -e PYTHONPATH=packages/claude-plugin python:3.12-slim python -m unittest discover -s packages/claude-plugin -p "*_tests.py" -v
docker run --rm --mount type=bind,src="$PWD",dst=/workspace,readonly -w /workspace -e PYTHONPATH=packages/codex-plugin python:3.12-slim python -m unittest discover -s packages/codex-plugin -p "*_tests.py" -v
docker run --rm --mount type=bind,src="$PWD",dst=/workspace,readonly -w /workspace python:3.12-slim python scripts/sync_plugin_bundle.py --check
```

Required PR checks are the existing backend workflow, Claude plugin E2E, Codex plugin E2E, bundle drift, and supplemental client-fault E2E. Both runtime workflows pin reviewed CLI versions (Codex retains its age gate); the report records commands, exits, pass counts, migration leaf, generated diff, versions, CI status, and unrun checks.

## Acceptance Gate

Checkpoint 6 is complete only when:

- P10 is healthy for the v1 cohort and the CP6 P14 negative boundary passes;
- same id/same fingerprint replays one authorized byte-stable snapshot;
- same id/different fingerprint returns the stable 409 without content;
- legacy collisions are explicit and never use live joins;
- F18 crash boundaries leave either no snapshot or one complete snapshot;
- no non-null-budget artifact exceeds its declared Engram token budget;
- oversized first items are bounded or omitted, never exempted;
- semantic failure returns authorized exact matches plus persisted warning;
- every selected API item comes from immutable snapshot fields;
- Claude and Codex fixture matrices cover every included terminal/failure disposition and every excluded event is documented;
- capture and recall failures are independently observable in canonical, bundled, and supplemental E2E paths;
- both real runtimes inject the server's verified bytes and emit their native success/failure/terminal events;
- generated bundles byte-match canonical source and no secret is present;
- migration, focused tests, full CI, correctness review, and Karpathy simplicity review are recorded and green.

## Stop Conditions

Stop before implementation or integration if:

- CP4 cannot provide one coherent selected version/document/current state;
- CP5 conflict authority is mutable-link-only at the C6 branch base;
- the C1.3 terminal primitive cannot distinguish/replay turn and session completion safely;
- a fingerprint input cannot be obtained without storing unredacted secret material;
- strict packing would require changing the public budget unit or adding a tokenizer dependency;
- a runtime manifest includes an event the pinned runtime does not emit;
- capture/recall independence requires a local durable queue;
- generated bundle ownership overlaps canonical CLI edits;
- a required test cannot run in the container boundary or fails twice for different unclear reasons.

Report the exact branch/SHA, migration leaf, completed slice, first decisive failure, affected invariant, options, and recommended next action. Do not weaken immutability, authorization, budget, or client independence to keep the checkpoint moving.
