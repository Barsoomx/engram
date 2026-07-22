# Distillation Reduce Rework (SLICE R1)

Status: design (approved binding decisions A+B+C; this doc expands, does not redesign)
Date: 2026-07-21
Scope: backend distillation reduce stage only. Extract untouched.

Dogfood operator directive in force: no prod deploy, no back-compat, no rolling-deploy,
no deploy feature-flags; deploy is stop-the-world (down -> migrate -> up); in-flight
distillation work is droppable at zero cost. Steady-state correctness (idempotency,
deterministic replay, reconciliation) is NOT waived. `contract_version=0/1` defaults are
LIVE semantics and are not touched. Retry routing is by `failure_code -> failure_class`
map (see Error Handling), not by any `retryable` flag.

---

## 1. Problem and Evidence

Sessions with more than ~100 observations fail distillation ~100% (prod 2026-07-21:
1 success / 153 works). 1306 `provider_output_malformed` runs across 77 sessions since
07-15, all on REDUCE stages; extract is healthy. Root cause is a chain, all verified in
code:

1. Reduce batches are packed by INPUT chars, not output size.
   `build_reduction_batches` (`memory/distillation_reduction.py:297-338`, packing loop
   `:305-322`) grows a group until the *input* JSON of the drafts exceeds
   `prompt_budget`. `prompt_budget = window.chunk_char_budget`, default 40000
   (`memory/distillation_window.py:46` `_DEFAULT_CHUNK_CHAR_BUDGET = 40000`), passed at
   `memory/distillation.py:779` and `:814`. There is no count bound, so a batch can carry
   ~25-40 drafts.

2. The reduce contract demands large detail-preserving output.
   `_REDUCE_SYSTEM_PROMPT` (`memory/distillation_reduction.py:26-51`) requires merged
   bodies up to `MAX_BODY = 3000` chars (`:22`) plus a verbatim echo of every draft id
   (64-hex) into `source_ids`. For 25-40 drafts the required completion is ~6-14k tokens.

3. The completion cap is fixed and small.
   `resolve_max_tokens` (`model_policy/services.py:1489-1505`) returns
   `_MAX_TOKENS_BY_KIND['distill_reduce.v1'] = 8192` (`:1176-1183`) and, because
   `distill_reduce.v1` is in `_FIXED_MAX_TOKEN_KINDS` (`:1183`), it ignores any policy
   metadata override. deepseek clamps completion to 4096 in some modes. Prod shows 132
   calls at exactly 8192 output tokens and 190 at exactly 4096.

4. Truncated JSON is never detected as truncation.
   Neither gateway reads a finish reason. OpenAI-compatible `_chat_completion` returns
   only `response['choices'][0]['message']['content']` (`model_policy/services.py:1726`).
   Anthropic `_anthropic_content_text` (`:1798-1807`) and `_messages` (`:1963`) never read
   `stop_reason`. `ProviderCallResult` (`:136-143`) has no finish-reason field. A truncated
   body reaches `ReductionStageContract.normalize_output`
   (`memory/distillation_reduction.py:736-758`), `json.loads` fails, and it is reported as
   `ProviderStageOutputError` -> `_MalformedOutcome`
   (`memory/distillation_provider_stage.py:914-917`).

5. The malformed outcome retries the identical frozen batch forever.
   `_malformed_failure()` (`:1010-1011`) classes it `PROVIDER_TRANSIENT` /
   `provider_output_malformed`, which retries with backoff (`work_failures.py:25`). The
   batch's `target_key` is a pure function of its input refs (`reduction_batch_key`,
   `memory/distillation_reduction.py:283-294`), so every retry re-plans the exact same
   oversized batch at temperature 0.2. `_record_malformed` (`:953-988`) keeps only
   `response_hash` + `response_size`; the body is discarded and the classified failure
   carries no `redacted_detail`, so `WorkflowRun.failure_reason`
   (`work_execution.py:849` = `failure.redacted_detail`) is empty.

6. Small sessions hid the bug. Reduce only runs when the draft count exceeds
   `reduction_target`. `_evaluate_draft_reduction_state`
   (`memory/distillation_reduction.py:577-613`, guard `:590`
   `if len(current) <= reduction_target: return final`) skips reduce for <=12 drafts, so
   tests and small sessions never triggered it. `reduction_target = 12` is frozen on the
   window (`window.reduction_target`, `_DEFAULT_REDUCTION_TARGET = 12`,
   `distillation_window.py:51`).

7. Additional latent hazards in the parser:
   - Every batch is forced to strictly shrink and to fit `max(target, ceil(n/2))`
     (`memory/distillation_reduction.py:255-259`), and each level must shrink
     (`:609-610`). A batch of genuinely distinct drafts cannot satisfy this and errors.
   - Memories whose `source_ids` are all unknown are silently dropped
     (`:238` filters, `:247-248` `continue`); if that empties the batch, `:251-252`
     raises "reduction output is empty". Draft loss is silent up to that point.

---

## 2. Design

Three binding decisions, expanded. Karpathy simplicity: the primary fix is (A); (B) and
(C) make the loop honest and give a bounded safety net. No new database columns.

### A. Output-budgeted batching + index references

Batch fan-in is chosen from a **worst-case OUTPUT** estimate, not input chars. A batch of
`n` drafts can, in the worst case, produce `n` full-size memories (pass-through, no
merge). We size `n` so that the estimated worst-case output fits under the provider's real
completion cap minus a 30% margin. This makes truncation **rare-by-planning**, not
impossible: `_OUTPUT_TOKENS_PER_CHAR = 0.4` (~2.5 chars/token) is a conservative estimate
for JSON+English, but it is NOT a true upper bound — CJK-dense bodies and heavy JSON
escaping (quotes, backslashes, control chars) can push real tokens/char above 0.4 and make
an in-limit-by-estimate batch still truncate. Progress does not depend on the estimate
being an upper bound: the decision-C split-backstop guarantees forward progress regardless.
A truncation is detected as a first-class outcome, bumps the generation (halving the budget
and fan-in), and at the floor a two-draft batch that still would not fit is emitted as two
size-1 pass-throughs with no provider call, after which no-shrink termination fires. So the
estimator's job is to make truncation uncommon; the backstop, not the estimator, is what
makes the loop converge.

Draft identity on the wire changes from a verbatim 64-hex id echo to **1-based integer
indices**. Drafts are numbered `1..n` in the prompt; the model returns integer `source_refs`;
code maps indices back to draft ids. An out-of-range, duplicate, or non-integer index is a
contract error, never a silent drop. The persisted reduce snapshot keeps `source_ids`
(draft ids) unchanged, so `_snapshot_drafts`/lineage/finalization are untouched — the
index contract is confined to the provider wire and the parser.

- Rejected: raise the caps. Explicit non-goal; treats the symptom, still unbounded.
- Rejected: keep char-budget batching and just lower it. Char budget does not bound output;
  a single 40-draft batch of terse drafts still demands >8k output tokens.
- Rejected: keep 64-hex echo but shrink batches. The echo alone is ~65 chars/draft of
  mandatory output and is the main output amplifier; indices remove it.

### B. Reduce = dedup, not compression

Intermediate reduce merges only near-duplicates; **pass-through of distinct drafts is
allowed** — a batch's output count may equal its input count. The per-batch forced-shrink
rule and the `max(12, ceil(n/2))` cap are removed. The reduction target scales with draft
count instead of being frozen at 12:

```
effective_target(total_drafts) = clamp(ceil(total_drafts / 4), window.reduction_target, 48)
```

`window.reduction_target` (12) stays as the immutable stored floor and the lower clamp
bound; `effective_target` is derived deterministically at evaluation time from the frozen
extract draft count. The loop terminates when `len(current) <= effective_target` OR a full
level yields no shrink (explicit termination, not a raise), with a hard bound of 4 tree
levels per generation to cap cost. Coverage (every observation in some memory union or in
`no_signal`) is preserved because the parser enforces a **partition**: every input index
appears in exactly one output memory; pass-through carries a draft forward unchanged.

- Rejected: drop reduce entirely. Explicit non-goal; loses genuine dedup and lineage
  rollup that finalization depends on.
- Rejected: keep forcing shrink but raise the cap per level. Distinct drafts can't be
  merged without inventing facts; forcing it is the contract violation we are removing.

### C. Truncation is first-class + bounded deterministic split

Both gateways read the finish reason (`finish_reason` for OpenAI-compatible, `stop_reason`
for Anthropic). A truncated completion raises a DISTINCT code `provider_output_truncated`
(never conflated with malformed). Remediation is deterministic batch-splitting expressed as
a **generation** dimension that lives entirely inside the existing fenced stage-identity
machine, needs no schema change, and is derived from persisted stage rows:

- Truncation is persisted by setting the failing REQUIRED reduce stage's existing
  `last_failure_class` column to `provider_output_truncated`.
- Reduce stage `level` encodes the generation band:
  `level = generation * GENERATION_LEVEL_STRIDE + tree_level`, `tree_level in 1..4`,
  `GENERATION_LEVEL_STRIDE = 16`, `generation in 0..3`. Reduce `level > 0` (model constraint
  `core_distill_stage_reduce_shape_ck`) always holds. Max level = `3*16 + 4 = 52`, well
  within `PositiveSmallIntegerField`.
- The planning generation is derived, not stored:
  `generation = 0` if no REQUIRED reduce stage carries the truncated marker, else
  `max(marker.level // STRIDE) + 1`. Exceeding `MAX_GENERATION = 3` is a clean hard fail
  (`INVALID_INPUT` / `distillation_reduction_truncation_exhausted`).
- Each generation halves the output budget used for batching
  (`budget >> generation`), which shrinks max fan-in, which subdivides the offending batch.
  At the floor, a two-draft batch that still would not fit is emitted as two size-1
  pass-throughs (no provider call), so truncation cannot recur and the loop converges via
  no-shrink termination. New-generation batches sit in a disjoint level band, so old-band
  REQUIRED/COMPLETE rows never collide on the `(window, stage_kind, level, ordinal, policy,
  policy_version, policy_role)` coordinate and are simply not re-selected.

Malformed AND truncated responses retain a redacted ~2k prefix in
`ProviderCallRecord.metadata['response_prefix']` (via the existing redaction helpers) and
set the classified failure's `redacted_detail` so `WorkflowRun.failure_reason` is populated.
`_FIXED_MAX_TOKEN_KINDS` is changed to honor a policy metadata `max_tokens` override (ops
lever, no deploy).

- Rejected: per-batch coordinate splitting (each truncated batch -> two child stages at the
  same level). The coord-uniqueness constraint has no status predicate, so a lingering
  REQUIRED parent row keeps its `(level, ordinal)`; any stable child-ordinal scheme either
  collides with the parent or renumbers siblings and orphans completed work. The
  generation-band re-plan sidesteps all of it with one derived integer.
- Rejected: a new `DistillationReductionSplit` table or a `reduction_split_generation`
  column on the window. Unnecessary — the marker persists in `last_failure_class` and the
  band is encoded in `level`. Directive prefers no schema change.
- Rejected: immediate in-attempt re-plan without persistence. Would not survive a worker
  crash mid-window; violates deterministic replay.

---

## 3. API / Contract Changes

### 3.1 New wire contract `distill_reduce.v2`

`REDUCE_PROMPT_CONTRACT` and `ReductionStageContract.prompt_contract` /
`.response_kind` become `distill_reduce.v2`. The response_kind string used by the gateways
and by `curation_schema_prompt_prefix` becomes `distill_reduce.v2`. (The persisted reduce
snapshot schema stays `{memories:[{title,body,confidence,source_ids,kind?}]}`; only the
provider wire changes.)

The rename is a full replacement of the `distill_reduce.v1` response_kind (no back-compat;
directive). EVERY site that keys on the literal string must flip to `distill_reduce.v2` —
missing any one of them silently corrupts the real-gateway path. Exhaustive list (verified
present in code):

- `memory/distillation_reduction.py`: `REDUCE_PROMPT_CONTRACT` (`:24`),
  `_REDUCE_SYSTEM_PROMPT` text (`:27`), `ReductionStageContract.prompt_contract` (`:707`),
  `.response_kind` (`:708`).
- `model_policy/services.py`: `fake_generated_content` dispatch (`:1121`);
  **`_STRUCTURED_RESPONSE_KINDS` (`:1153-1155`)**; `_MAX_TOKENS_BY_KIND` key (`:1180`);
  `_FIXED_MAX_TOKEN_KINDS` (`:1183`); the schema-prefix dispatch (`:1309`); the
  `_ANTHROPIC_STRUCTURED_TOOLS` key (`:1396`).

`_STRUCTURED_RESPONSE_KINDS` is load-bearing and MUST include `distill_reduce.v2`:
`_completion_body`/`_completion_title` (`:1810-1821`) return the raw JSON body only when
`response_kind in _STRUCTURED_RESPONSE_KINDS`; otherwise they push the JSON through
`_split_completion` (title/body text split), mangling it. Both real gateways call these
(`OpenAICompatibleGateway` `:1591-1592`, `AnthropicMessagesGateway` `:1851-1852`), so if
`distill_reduce.v2` is absent from the set, `normalize_output` sees mangled input and every
real-provider (deepseek prod) reduce call fails `provider_output_malformed`. The
fake-provider reduce tests cannot catch this — `FakeProviderGateway.call` builds
`generated_body` directly and never touches `_completion_body` — so a real-gateway
reduce-body test is required (see test 24).

Prompt input object (built in `ReductionStageContract.prepare_call`, replacing the current
`{drafts, reduction_target}` at `memory/distillation_reduction.py:725-729`):

```json
{"drafts":[{"index":1,"title":"...","body":"...","confidence":"0.90","kind":"gotcha"}, ...]}
```

`reduction_target` is removed from the wire (termination is orchestration's job now).
`index` is the 1-based position; `confidence` is a string as today; `kind` omitted when blank.

Provider output object:

```json
{"memories":[{"title":"...","body":"...","confidence":0.9,"source_refs":[1,3],"kind":"gotcha"}, ...]}
```

New `_REDUCE_SYSTEM_PROMPT` (exact text; `memory/distillation_reduction.py:26-51`):

```
You consolidate engineering-memory drafts under the distill_reduce.v2 contract. Return
exactly one JSON object and nothing else: no prose, no markdown code fences. The object
must contain exactly the key memories (array of objects) and no additional properties.
Each memories entry must contain exactly these keys and no additional properties: title
(non-blank string, at most 255 characters); body (non-blank string, at most 3000
characters); confidence (a JSON number between 0 and 1, never a string); source_refs
(non-empty array of unique positive integers); kind (optional, one of: decision,
convention, gotcha, architecture, incident; omit it when none applies). The user message is
one JSON object with the single key drafts: an array of {index, title, body, confidence,
kind?} where index is a positive integer. Every source_refs value must be a draft index
copied verbatim from the input. Partition the drafts: assign every input index to exactly
one memory, never repeat an index across memories or within one memory, and never omit an
index. Task: merge only drafts that record the same or a near-duplicate durable fact,
decision, or behavior into one memory whose title and body preserve the concrete details
(identifiers, paths, versions, numbers) of every merged draft; never invent facts absent
from the drafts. A draft that is distinct from every other draft must pass through as its
own memory referencing that single index; do not force unrelated drafts together and do not
drop any draft. The number of memories may therefore equal the number of drafts. Give each
memory a confidence no higher than the highest confidence among its source drafts.
```

New `_DISTILL_REDUCE_SCHEMA_INSTRUCTIONS` (`model_policy/services.py:1286-1299`), the
prefix prepended for OpenAI/deepseek — mirror the above, exact text:

```
Return exactly one JSON object and nothing else: no prose, no markdown code fences. The
object must contain exactly the key memories (array of objects) and no additional
properties. Each memories entry must contain exactly these keys and no additional
properties: title (non-blank string, at most 255 characters); body (non-blank string, at
most 3000 characters); confidence (a JSON number between 0 and 1); source_refs (non-empty
array of unique positive integers); kind (optional, one of: decision, convention, gotcha,
architecture, incident). Only use draft indices copied verbatim from the input drafts.
Partition the drafts: assign every input index to exactly one memory, never repeat an index
and never omit one. Merge only near-duplicate drafts; a distinct draft passes through as its
own memory, so the number of memories may equal the number of input drafts.
```

Anthropic structured tool `distill_reduce.v2` (`_ANTHROPIC_STRUCTURED_TOOLS`,
`model_policy/services.py:1396-1430`): key renamed to `distill_reduce.v2`; `source_ids`
(array of strings) replaced by `source_refs` (`{type:array, items:{type:integer,
minimum:1}, minItems:1, uniqueItems:true}`); `required: [title, body, confidence,
source_refs]`; the `maxItems: 12` bound on `memories` is REMOVED (pass-through may equal
input count).

### 3.2 Worst-case output estimator (`memory/distillation_reduction.py`)

Constants:

```
_OUTPUT_TOKENS_PER_CHAR    = 0.4     # conservative upper bound (~2.5 chars/token) for JSON+English
_OUTPUT_ENVELOPE_CHARS     = 32      # {"memories":[ ... ]}
_PER_MEMORY_JSON_OVERHEAD  = 128     # keys, quotes, braces, confidence number, kind
_PER_MEMORY_INDEX_CHARS    = 8       # per index reference (digits + comma), 1 per draft under a partition
_TRUNCATION_MARGIN         = 0.30    # reserve 30% of the completion cap
```

Formulas (`MAX_TITLE = 255`, `MAX_BODY = 3000`):

```
per_memory_chars               = MAX_TITLE + MAX_BODY + _PER_MEMORY_JSON_OVERHEAD + _PER_MEMORY_INDEX_CHARS
worst_case_output_tokens(n)    = ceil(_OUTPUT_TOKENS_PER_CHAR * (_OUTPUT_ENVELOPE_CHARS + n * per_memory_chars))
output_budget_tokens(cap)      = floor(cap * (1 - _TRUNCATION_MARGIN))
max_reduction_fanin(budget)    = max(1, largest n with worst_case_output_tokens(n) <= budget)
```

`per_memory_chars = 3391`; `worst_case_output_tokens(n) = ceil(12.8 + 1356.4 * n)`.
Worked values: deepseek cap 4096 -> budget 2867 -> fan-in 2; cap 8192 -> budget 5734 ->
fan-in 4. Fan-in is intentionally small and safe; ops widen it by raising `max_tokens`
metadata for a provider that honors a larger completion (see Ops).

Accepted cost (decision A): a small forced fan-in multiplies reduce provider-call volume. On
deepseek (fan-in 2) a 100-draft session emits ~50 level-1 + ~25 level-2 ≈ 75 provider calls
(vs the old ~4-6 char-budget batches), spread over many attempt/lease cycles bounded by
`max_provider_calls_per_attempt`. This is the inherent, approved consequence of output-budgeted
small batching — not a regression (the old char-budget path was ~100% failing on these
sessions) — and is mitigated by the §7 ops lever (raise `completion_clamp`+`max_tokens` on a
provider honoring larger completions, which lifts fan-in). Flagged here so the magnitude is
explicit; no design change.

### 3.3 Effective completion cap and the fixed-kind override

`model_policy/services.py`:

```
_PROVIDER_COMPLETION_CLAMP_DEFAULTS = {'deepseek': 4096}

def provider_completion_clamp(policy) -> int | None:
    md = policy.metadata if isinstance(policy.metadata, dict) else {}
    raw = md.get('completion_clamp')
    if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
        return raw
    return _PROVIDER_COMPLETION_CLAMP_DEFAULTS.get(policy.provider)

def effective_completion_cap(policy, response_kind) -> int:
    cap = resolve_max_tokens(policy, response_kind)     # honors metadata max_tokens for distill_reduce.v2 only
    if response_kind != 'distill_reduce.v2':
        return cap                                      # extract/curation: unclamped, exactly today's value (§8)
    clamp = provider_completion_clamp(policy)
    return min(cap, clamp) if clamp is not None else cap
```

`resolve_max_tokens` (`:1489-1505`) change: the `metadata['max_tokens']` override is honored
ONLY for `response_kind == 'distill_reduce.v2'` (a positive-integer override; else fall back
to `_MAX_TOKENS_BY_KIND[kind]`). It is deliberately NOT applied to the other members of
`_FIXED_MAX_TOKEN_KINDS` (`distill_extract.v1`, `curation_decision_v1`) — those kinds are out
of scope (§8) and may resolve through the same `ModelPolicy` as reduce (reduce resolves via
`_resolve_primary_policy`); a reduce-only override prevents the ops fan-in lever from
silently changing extract/curation caps. The `provider_completion_clamp` reduction is scoped
the SAME way: `effective_completion_cap` returns the unclamped `resolve_max_tokens` for every
non-reduce kind and applies the clamp only for `distill_reduce.v2`. This symmetry matters
because `provider_completion_clamp` keys on `policy.metadata['completion_clamp']` and does not
take `response_kind`; without the scope guard, an ops edit that LOWERS `completion_clamp` on a
policy shared by extract+reduce would silently clamp extract's honored completion too — and
extract has no truncation detection/remediation in this slice (§8), so it would truncate into
`provider_output_malformed`. With the guard, extract/curation requests are left at exactly
today's `resolve_max_tokens` value (deepseek extract stays at its fixed 8192 request, which the
provider already honors up to its own 4096 limit — unchanged from current healthy behavior);
only the reduce request, whose estimator also consumes the clamp (§3.7), is ever clamped. All
other kinds keep current behavior. The value sent to the provider (`extra['max_tokens']` at
`:1577`, and `max_tokens=` at anthropic `:1844`) becomes `effective_completion_cap(policy,
response_kind)` so the request never asks for more than the estimator assumed.

### 3.4 Truncation detection and `ProviderCallResult`

`ProviderCallResult` (`:136-143`) gains `finish_reason: str = ''`.

- `_chat_completion` (`:1705-1726`) returns `(content, usage, finish_reason)` where
  `finish_reason = str(response['choices'][0].get('finish_reason') or '')`.
- `_messages` (`:1940-1963`) returns `(content, usage, stop_reason)` where
  `stop_reason = str(response.get('stop_reason') or '')`; both `call` sites thread it into
  the result.
- Helper `is_truncated_finish_reason(reason) -> bool` returns `reason in {'length','max_tokens'}`.

`memory/distillation_provider_stage.py`:

```
PROVIDER_OUTPUT_TRUNCATED = 'provider_output_truncated'
```

Both outcomes are frozen dataclasses whose implemented field order is authoritative
(`memory/distillation_provider_stage.py:181-197`):
`_MalformedOutcome(response_hash, response_size, response_prefix='', error_detail='',
provider_call_ids=(), started_calls=1)` — the existing malformed outcome gains BOTH a
`response_prefix` and an `error_detail` field — and
`_TruncatedOutcome(response_hash, response_size, response_prefix, provider_call_ids=(),
started_calls=1)`. In `_attempt_stage` (`:920-951`), after obtaining `result` and before
`normalize_output`, compute the redacted prefix (capped at `_RESPONSE_PREFIX_LIMIT = 2000`)
and check truncation:

```
prefix = str(redact_value(result.generated_body).value)[:2000]
if is_truncated_finish_reason(result.finish_reason):
    return _TruncatedOutcome(response_hash, response_size, prefix, (str(result.call_record_id),))
```

`normalize_output` failures return `_MalformedOutcome(response_hash, response_size, prefix,
str(error), (str(result.call_record_id),))` — positional order `(response_hash,
response_size, response_prefix, error_detail, provider_call_ids)` matching the dataclass;
the `ProviderStageOutputError` message is captured in `error_detail` and threaded into
`_malformed_failure(outcome.error_detail).redacted_detail`, which becomes
`WorkflowRun.failure_reason`. The redacted `response_prefix` is persisted to
`ProviderCallRecord.metadata['response_prefix']` by `_record_stage_failure_diagnostics`
(shared by `_record_malformed`/`_record_truncated`).

`_run_stage` (`memory/distillation_provider_stage.py:1107-1191`) MUST gain an explicit
`_TruncatedOutcome` branch, placed after the `_MalformedOutcome` branch and BEFORE the final
provider-error fall-through. Without it a `_TruncatedOutcome` falls through to the
provider-error path and dereferences `outcome.error` (`:1168`), which `_TruncatedOutcome`
does not have -> `AttributeError`. The branch mirrors the malformed one but with no
fallback:

```
if isinstance(outcome, _TruncatedOutcome):
    truncated_now = _fresh_now(now)
    _record_truncated(
        stage, claim, now=truncated_now,
        response_hash=outcome.response_hash,
        response_size=outcome.response_size,
        response_prefix=outcome.response_prefix,
        provider_call_ids=outcome.provider_call_ids,
    )
    return StageExecutionResult(
        STAGE_RETRY, stage, failure=_truncated_failure(),
        provider_call_ids=prior_provider_call_ids + outcome.provider_call_ids,
        started_provider_calls=prior_started_calls + outcome.started_calls,
    )
```

`_record_truncated` also sets the stage's `last_failure_class = PROVIDER_OUTPUT_TRUNCATED`
(the persisted marker §2C reads) in addition to writing `metadata['response_prefix']`. No
`_fallback_permitted`/`_try_fallback` call: truncation is remedied by the generation bump,
not by switching providers (§5). `_run_stage`'s return-type annotation is widened to include
`_TruncatedOutcome` from `_attempt_stage`.

### 3.5 Parser and batching rewrite (`memory/distillation_reduction.py`)

`parse_reduction_output(payload, inputs, *, ...)` — index/partition semantics:

- `inputs` is ordered; index `i` (1-based) -> `inputs[i-1]`; `n = len(inputs)`.
- payload must be exactly `{'memories': [...]}`.
- each memory: keys subset of `{title, body, confidence, source_refs, kind}`, required
  `{title, body, confidence, source_refs}`; title/body non-blank within limits; confidence
  numeric in `[0,1]`; kind valid or absent.
- `source_refs`: non-empty list of ints, each `1 <= i <= n`, duplicate-free within the
  memory. Any non-int / out-of-range / duplicate -> `ReductionContractError` (NO silent drop).
- Coverage/partition: the union of all `source_refs` across memories must equal `{1..n}`
  exactly and each index appears in exactly one memory. Missing or repeated index across
  memories -> `ReductionContractError`.
- The shrink rule (`:255-256`) and the `max(target, ceil(n/2))` cap (`:257-259`) are
  REMOVED. Output count may be `1..n`.
- Map indices to `inputs[i-1].draft_id` to build each `ReducedMemory.source_ids`.

`ReductionStageContract.normalize_output` is the SECOND caller of `parse_reduction_output`
(after `reduce_multilevel`, §3.6) — it is the production real-gateway normalization path that
failed 1306 times (§1), so it must be re-signatured in lockstep. Current code
(`memory/distillation_reduction.py:743-744`) reads `stage.window.reduction_target` and passes
`reduction_target=` to the parser; both are DROPPED — `normalize_output` calls
`parse_reduction_output(payload, inputs)` with no `reduction_target` (the parser no longer
accepts the kwarg, so leaving it in place is a `TypeError` on every real-gateway reduce body).
The `stage.window.reduction_target` read at `:743` is deleted (termination is orchestration's
job now; `normalize_output` only parses one batch's payload). Nothing else in `normalize_output`
changes — the persisted `{memories:[{title,body,confidence,source_ids,kind?}]}` shape is
unchanged (§3.1). Test 24 (real-gateway body -> `normalize_output` parses) covers this edit.

`build_reduction_batches(drafts, *, max_fanin, level)` — count-based grouping replaces the
input-char packing loop (`:305-338`):

- Partition `drafts` into consecutive groups of size `min(max_fanin, remaining)`.
- A group of size >= 2 is a provider batch (`provider_required=True`); a group of size 1 is
  pass-through (`provider_required=False`).
- If `max_fanin == 1`, every group is a size-1 pass-through.
- `input_hash`, `target_key`, ordinal derivation unchanged (`reduction_input_hash`,
  `reduction_target_key`, ordinal = index in the level's batch list).

### 3.6 Reduction loop and generation (`memory/distillation_reduction.py`)

Constants: `_MAX_TREE_LEVELS = 4`, `_GENERATION_LEVEL_STRIDE = 16`, `_MAX_GENERATION = 3`.

```
def effective_reduction_target(total_drafts, floor) -> int:
    return max(floor, min(48, ceil(total_drafts / 4)))

class ReductionTruncationExhausted(ReductionContractError):
    pass


def compute_reduction_generation(truncated_levels: Sequence[int]) -> int:
    if not truncated_levels:
        return 0
    g = max(l // _GENERATION_LEVEL_STRIDE for l in truncated_levels) + 1
    if g > _MAX_GENERATION:
        raise ReductionTruncationExhausted('reduction truncation generations exhausted')

    return g
```

`_evaluate_draft_reduction_state(initial_drafts, accepted_rows, *, reduction_target_floor,
output_budget_tokens, generation)`:

- `target = effective_reduction_target(len(initial_drafts), reduction_target_floor)`.
- `if len(initial_drafts) <= target: return final = initial_drafts`.
- `budget = output_budget_tokens >> generation`; `max_fanin = max_reduction_fanin(budget)`;
  `level_base = generation * _GENERATION_LEVEL_STRIDE`.
- For `tree_level in 1.._MAX_TREE_LEVELS` while `len(current) > target`:
  - `level = level_base + tree_level`.
  - `batches = build_reduction_batches(current, max_fanin=max_fanin, level=level)`.
  - pass-through batches feed their inputs forward; a provider batch with no accepted row
    is returned as the pending batch; accepted rows expand as today.
  - `if len(next_level) == len(current): return final = current`  (no-shrink termination;
    the old `>=` raise at `:609-610` becomes an equality-terminate).
  - `current = next_level`.
- `return final = current` when the target is met or `_MAX_TREE_LEVELS` is reached.

`derive_first_pending_reduction_target` / `derive_final_reduction_drafts` /
`_evaluate_reduction_state` take `reduction_target_floor`, `output_budget_tokens`,
`generation` instead of `reduction_target` + `prompt_budget`. `provider_stage_target`
and `resolve_reduction_stage` are unchanged except that the batch's `level` already carries
the band.

`reduce_multilevel(drafts, *, reduction_target_floor, output_budget_tokens, generation,
provider)` (`memory/distillation_reduction.py:379-406`) is re-signatured to match. It
threads `reduction_target_floor`/`output_budget_tokens`/`generation` into
`_evaluate_draft_reduction_state` (replacing the old `reduction_target`/`prompt_budget`
kwargs at `:389-390`) and calls `parse_reduction_output(provider(state.pending),
state.pending.input_drafts)` with NO `reduction_target` (the parser no longer accepts it,
§3.5; the current `reduction_target=` argument at `:397` is dropped). It derives no floor
internally — the caller supplies `reduction_target_floor` and `generation`; effective target
and `max_fanin` are computed inside `_evaluate_draft_reduction_state`. Test 6 drives this
signature end-to-end.

### 3.7 Orchestration (`memory/distillation.py:772-815`)

Before deriving reduce batches:

```
reduce_policy = resolve_reduction_policy(window)                # public wrapper of _resolve_primary_policy
cap           = effective_completion_cap(reduce_policy, 'distill_reduce.v2')
output_budget = output_budget_tokens(cap)
# DISTINCT query, NOT the COMPLETE-only accepted set (`reduction_stages` /
# `_accepted_stage_rows(window, REDUCE)`, distillation.py:635-644 filters status=COMPLETE).
# This one reads v2 REQUIRED reduce stages that carry the truncation marker:
truncated_marker_rows = DistillationStage.objects.filter(
    window=window,
    stage_kind=REDUCE,
    status=DistillationStageStatus.REQUIRED,
    prompt_contract='distill_reduce.v2',
    last_failure_class=PROVIDER_OUTPUT_TRUNCATED,
)
truncated_levels = [s.level for s in truncated_marker_rows]
generation    = compute_reduction_generation(truncated_levels)  # raises -> invalid failure
```

The accepted reduce set MUST exclude non-v2 rows. The REDUCE call to `_accepted_stage_rows`
(`memory/distillation.py:773`) gains a `prompt_contract='distill_reduce.v2'` filter
(the EXTRACT call at `:772` is unchanged; extract is out of scope §8). This filter is
load-bearing and is the ONLY place cross-contract disjointness is enforced for the
accepted-set path: accepted reduce rows are matched by `_AcceptedReduction.batch_key =
reduction_batch_key(level, ordinal, input_hash)` (`memory/distillation_reduction.py:565-567`,
`:283-291`), which is prompt_contract-INDEPENDENT, and v1 char-batched reduce rows share the
`level` band 1..4 with v2 generation-0 batches. Without the filter a residual COMPLETE v1
reduce row whose `(level, ordinal, input_hash)` coincides with a v2 gen-0 batch would either
duplicate a `batch_key` (`_evaluate_draft_reduction_state:586` raise ->
`distillation_reduction_plan_invalid`) or be consumed as that batch's accepted result
(`:605`), injecting an old-contract (possibly non-partition, draft-lossy) snapshot. The
per-stage `stage_target_key`/`core_distill_stage_coord_uniq` path DOES key on
`prompt_contract` and is safe on its own; the accepted-set `batch_key` path does not, so the
filter is where "old v1 reduce rows are never re-selected" is actually made true. (The
REQUIRED-stage truncation-marker query for `truncated_levels` above must likewise read only
v2 REQUIRED rows so a stale v1 REQUIRED row cannot inflate `generation`.)

`derive_first_pending_reduction_target(extraction_stages, reduction_stages,
reduction_target_floor=window.reduction_target, output_budget_tokens=output_budget,
generation=generation)`; same args for `derive_final_reduction_drafts`. Policy-resolution
failure at planning -> `_configuration_failure`. Failure routing at the reduce-planning
`try` (`:781-785`) is split into two `except` arms so the exhaustion case gets its promised
distinct code (both are `INVALID_INPUT` via `_invalid_distillation_failure`, which accepts an
arbitrary code string):

```
except ReductionTruncationExhausted as error:
    raise _invalid_distillation_failure(
        'distillation_reduction_truncation_exhausted', str(error)) from error
except ReductionContractError as error:
    raise _invalid_distillation_failure(
        'distillation_reduction_plan_invalid', str(error)) from error
```

`ReductionTruncationExhausted` subclasses `ReductionContractError`, so the specific arm MUST
precede the general one. This is the single source of the `distillation_reduction_truncation_exhausted`
code promised in §2C and §5; a plain `ReductionContractError` (bad partition, empty output,
etc.) still yields `distillation_reduction_plan_invalid`.

`resolve_reduction_stage`/`execute_reduction_stage` continue to use
`ReductionStageContract()`; `execute_reduction_stage` sets the contract as today.

---

## 4. Data Flow

1. Extract stages complete (unchanged), producing per-chunk draft snapshots.
2. Orchestrator resolves the reduce policy, computes `output_budget`, reads truncated
   markers, computes `generation`.
3. `_evaluate_draft_reduction_state` derives `effective_target` and `max_fanin`, builds
   level `generation*16 + tree_level` batches by count, and returns the first unaccepted
   provider batch (pending) or the final drafts.
4. A pending batch resolves to a `DistillationStage` (`level` in the current band), a
   provider call runs with `max_tokens = effective_completion_cap`, and the gateway returns
   `finish_reason`.
5. Truncated -> `_TruncatedOutcome` -> stage marked REQUIRED + `last_failure_class =
   provider_output_truncated`, prefix retained, `PROVIDER_TRANSIENT` retry. Next attempt
   sees the marker, bumps generation, halves the budget, re-plans a smaller band. Malformed
   -> `_MalformedOutcome` with `redacted_detail` populated.
6. Well-formed -> parse (index->id, partition-checked) -> snapshot persisted with
   `source_ids`; accepted rows feed the next tree level.
7. Loop terminates at `effective_target` or on no-shrink; finalization runs on the final
   drafts (unchanged), coverage preserved because the partition forwards every draft.

---

## 5. Error Handling

`failure_code -> failure_class` routing (`work_failures.py`, `_malformed_failure` and new
`_truncated_failure` in `distillation_provider_stage.py`):

- `provider_output_malformed` -> `PROVIDER_TRANSIENT`, `redacted_detail` = the contract
  error message (now non-empty), retried with backoff `(30, 1800)`. With output-budgeted
  batching this should no longer fire from size; if it does it is a real contract defect and
  the retained prefix + failure_reason expose it.
- `provider_output_truncated` -> `PROVIDER_TRANSIENT`, `redacted_detail` =
  `'reduction provider output was truncated at the completion cap'`; retry bumps generation.
  Does NOT trigger provider fallback (truncation is not a policy fault; generation split is
  the remedy). At `generation > _MAX_GENERATION` -> `INVALID_INPUT` /
  `distillation_reduction_truncation_exhausted` (terminal, window fails cleanly with a
  populated failure_reason).
- Parser contract violations (bad index, non-partition coverage, malformed keys) surface as
  `ReductionContractError` -> `ProviderStageOutputError` -> `provider_output_malformed`
  (a model contract violation is genuinely malformed output).
- `_record_malformed` and `_record_truncated` both write
  `metadata['response_prefix']` (<=2000 chars, redacted) and
  `metadata['response_hash']`/`['response_size']` on each `ProviderCallRecord`, reusing
  `redact_value`.

Invariants:
- Determinism/replay: batching is a pure function of `(drafts, reduction_target floor,
  output_budget, generation)`. Of these, drafts and the floor are window-frozen and
  generation is derived from persisted stage rows, so a mid-window worker crash replays to
  the same plan under a stable policy. `output_budget` is NOT window-frozen: it is
  re-derived every attempt from the LIVE policy
  (`effective_completion_cap(resolve_reduction_policy(window), 'distill_reduce.v2')`, §3.7).
  An ops edit to `metadata['max_tokens']`/`['completion_clamp']` mid-window therefore changes
  `max_fanin` -> new `target_key`s and abandons the in-flight reduce stages. This is benign
  and intended under the dogfood "in-flight work is droppable" directive — the invariant is
  determinism GIVEN a fixed policy, not immunity to a live policy edit. Absent such an edit,
  identical inputs -> identical `target_key`/`stage_key`.
- No infinite loop: generation is bounded by 3; each generation strictly halves the budget;
  the floor is size-1 pass-through (no provider call), after which no-shrink termination
  fires. `_MAX_TREE_LEVELS = 4` bounds per-generation cost.
- No coord collision: distinct generation bands occupy disjoint `level` ranges; old-band
  rows are never re-selected. `contract_version` defaults untouched.
- Coverage: parser-enforced partition guarantees every observation reaches finalization via
  some memory (merged or pass-through); no silent draft drop.

---

## 6. Test Plan (TDD, colocated `*_tests.py`, pytest, stubs over mocks except views)

Order: write each failing test first, then the implementation slice that satisfies it.

### 6.1 `memory/distillation_reduction_tests.py`

1. `worst_case_output_tokens` monotonic and matches the closed form for n in {1,2,4,25};
   `output_budget_tokens(4096) == 2867`, `output_budget_tokens(8192) == 5734`.
2. `max_reduction_fanin(2867) == 2`, `max_reduction_fanin(5734) == 4`,
   `max_reduction_fanin(<worst_case_output_tokens(1)) == 1`.
3. `build_reduction_batches` count-based: 5 drafts, `max_fanin=2` -> groups (2,2,1); the
   size-1 tail is `provider_required=False`; `max_fanin=1` -> all pass-through.
4. `effective_reduction_target`: 12->12, 48->12, 100->25, 200->48, 12-floor honored.
5. Termination: a level whose batches all pass through (distinct drafts) terminates with
   `current` as final (no raise); a mergeable set shrinks to `effective_target`.
6. Pass-through coverage: distinct drafts survive end-to-end via `reduce_multilevel`, output
   count == input count, union of source ids == all input observation ids.
7. Index mapping: `parse_reduction_output` maps `source_refs` indices to draft ids;
   out-of-range / duplicate-across-memories / missing-index / non-integer each raise
   `ReductionContractError`; a valid partition parses; output count may equal input count.
8. `compute_reduction_generation`: `[]->0`; markers in band 0 -> 1; band 1 -> 2; overflow
   past `_MAX_GENERATION` raises `ReductionTruncationExhausted` (distinct subclass, so the
   orchestration arm maps it to `distillation_reduction_truncation_exhausted`, not
   `distillation_reduction_plan_invalid`). Generation halves the budget and lowers `max_fanin`.
9. Generation banding: `build_reduction_batches` at generation 1 emits `level` in `17..20`;
   batches disjoint from generation-0 `target_key`s for the same drafts.
10. Update the reduce prompt-contract snapshot for the new `_REDUCE_SYSTEM_PROMPT`,
    `REDUCE_PROMPT_CONTRACT == 'distill_reduce.v2'`, and prepared-call shape
    (`drafts` carry `index`, no `reduction_target`).

### 6.2 `memory/distillation_provider_stage_tests.py`

11. Truncation detection: a stub gateway returning `finish_reason='length'` (and one with
    `stop_reason='max_tokens'`) drives `_attempt_stage` -> `_TruncatedOutcome` ->
    `last_failure_class == provider_output_truncated`, `PROVIDER_TRANSIENT`, no fallback.
12. Diagnostics retention: malformed and truncated both persist
    `metadata['response_prefix']` (redacted, <=2000) and set `redacted_detail` so the run
    failure_reason is non-empty.
13. Split remediation: two sequential attempts — attempt 1 truncates a generation-0 batch,
    attempt 2 (marker present) plans a generation-1 smaller band and completes; assert the
    generation-0 stage is not re-selected and no coord collision.

### 6.3 `model_policy/services_tests.py`

14. `resolve_max_tokens(policy_with_metadata_max_tokens, 'distill_reduce.v2')` honors the
    override; without metadata returns 8192. The SAME policy metadata does NOT change
    `resolve_max_tokens(policy, 'distill_extract.v1')` or `'curation_decision_v1'` (override
    scoped to the reduce kind).
15. `provider_completion_clamp`: deepseek default 4096; `metadata['completion_clamp']`
    override wins; other providers None.
16. `effective_completion_cap`: for `distill_reduce.v2`, `min(kind cap, clamp)` — deepseek ->
    4096, openai -> kind cap; for a non-reduce kind (`distill_extract.v1`) on the SAME deepseek
    policy carrying a low `completion_clamp`, the returned cap is the unclamped
    `resolve_max_tokens` (clamp is reduce-scoped, extract untouched).
17. `finish_reason` threading: OpenAI-compatible gateway surfaces
    `choices[0].finish_reason` and Anthropic gateway surfaces `stop_reason` into
    `ProviderCallResult.finish_reason`.
18. Update snapshot `test_distill_reduce_schema_prefix_states_parser_enforced_rules`
    (`:457-473`) and `test_openai_distill_reduce_prompt_carries_schema_instructions`
    (`:408-433`) to the new `source_refs`/partition prefix and `distill_reduce.v2`.
19. Anthropic tool schema for `distill_reduce.v2` exposes `source_refs` (integers), drops
    `maxItems` on `memories`, requires `source_refs`.
20. `generated_distill_reduce_payload` (fake provider) emits `source_refs` integer indices
    that partition the input drafts (one memory per draft or a single merged partition),
    valid under the new parser.
24. Real-gateway reduce body: drive `_completion_body(content, 'distill_reduce.v2')` (and
    `_completion_title`) with a raw reduce JSON string and assert it returns the JSON
    verbatim (NOT split-mangled) — i.e. `distill_reduce.v2 in _STRUCTURED_RESPONSE_KINDS`.
    A companion assertion feeds the same JSON through an `OpenAICompatibleGateway` /
    `AnthropicMessagesGateway` call with a stub opener returning the reduce body and a
    non-truncating `finish_reason`, and asserts the resulting `generated_body` parses under
    `ReductionStageContract.normalize_output`. This is the only test that exercises the real
    gateway body path; fake-provider tests bypass it.

### 6.4 `memory/distillation_tests.py` (integration + regression)

21. Synthetic large session: 100+ extract drafts, fake provider, distills end-to-end with
    ZERO `provider_output_malformed` and zero `provider_output_truncated`; every observation
    is covered; final count <= `effective_target` or explained by no-shrink pass-through.
22. Regression: a <=12-draft session never calls reduce and produces the same result as
    before (small-session path unchanged).
23. A session whose fake provider forces one truncation resolves via a generation bump and
    still finalizes (no infinite retry), asserting bounded generations.
25. Cross-contract accepted-set isolation: seed a non-terminal window with a COMPLETE
    reduce stage carrying `prompt_contract='distill_reduce.v1'` at a level in `1..4` (crafted
    so its `(level, ordinal, input_hash)` would collide with a v2 gen-0 batch), then run the
    v2 reduce planner. Assert the v1 row is NOT pulled into the accepted set (the
    `_accepted_stage_rows` REDUCE query filters `prompt_contract='distill_reduce.v2'`), no
    `distillation_reduction_plan_invalid` duplicate-key raise fires, and the v2 gen-0 batch is
    planned fresh (pending), not resolved from the v1 snapshot.

Execution (per Worktree Quickstart, unique compose project):

```
docker compose -p engram-r1 run --rm app pytest -q \
  engram/memory/distillation_reduction_tests.py \
  engram/memory/distillation_provider_stage_tests.py \
  engram/memory/distillation_tests.py \
  engram/model_policy/services_tests.py
```

---

## 7. Ops

- No migration. Generation and truncation state ride existing columns (`level`,
  `last_failure_class`). `manage.py migrate` still required only for a clean db lifecycle.
- New policy metadata keys (per `ModelPolicy.metadata`, no deploy):
  - `max_tokens` (int > 0): now honored for `distill_reduce.v2` ONLY (not extract/curation,
    which stay on their fixed caps — §8). Raising it enlarges the requested completion AND
    the estimator's fan-in — set only for a provider that actually honors the larger
    completion.
  - `completion_clamp` (int > 0): caps the effective completion the estimator assumes for
    `distill_reduce.v2` ONLY, regardless of `max_tokens`. Default map: `deepseek -> 4096`. Set
    to the real honored completion of the deployed model. It does NOT touch extract/curation:
    `effective_completion_cap` applies the clamp only for the reduce kind (§3.3), so a shared
    policy's extract/curation requests keep their unclamped fixed caps even if this is lowered.
- To make deepseek batches bigger without a deploy: raise both
  `metadata['completion_clamp']` and `metadata['max_tokens']` on the reduce policy to the
  provider's true completion limit; fan-in follows from `effective_completion_cap`.
- Rollout is stop-the-world: `down -> up`. In-flight reduce work is droppable; on restart
  the new plan supersedes any half-done old-contract reduce stages. A normal restart does NOT
  wipe stage rows (no migration drops them), so residual COMPLETE/REQUIRED v1 reduce rows can
  persist on any non-terminal window. They are made harmless by the reduce accepted-set
  `prompt_contract='distill_reduce.v2'` filter (§3.7): v1 reduce rows are never pulled into
  the accepted set, so they are never matched by `batch_key` and never re-selected.
  IMPORTANT: `target_key`/`reduction_batch_key` is prompt_contract-INDEPENDENT (`level`,
  `ordinal`, `input_hash` only), so disjointness comes from that accepted-set filter, NOT
  from the key differing per contract — only the per-stage `stage_target_key`/coord path keys
  on `prompt_contract`. Within v2, distinct generation bands are disjoint because `level` is
  part of `batch_key`.
- Existing FAILED sessions are NOT auto-recovered by this deploy. The 77 malformed sessions
  from §1 have each retried ~17 times and are already in `TERMINAL_FAILURE`
  (`work_execution.py:887-888`, streak >= 12); terminal works are absorbed and never
  re-claimed (`_short_circuit_state`/`_absorbs_redelivered_terminal_run`,
  `work_execution.py:463/530`). The reduce fix only changes forward behavior. Recovering
  them is a separate explicit ops step: reset the affected distillation works
  (`execution_state = READY` — the `WorkflowWorkExecutionState.READY` enum value at
  `core/models.py:1216`, there is no `PENDING` member — `failure_streak = 0`, clear
  `next_retry_at`), after which the
  new plan re-runs them. That reset is out of scope for SLICE R1 (see §8) and, under the
  dogfood directive, dropping the terminalized sessions entirely is also acceptable.
- Observability: `provider_output_truncated` is a distinct failure code in
  `WorkflowRun.failure_code`; `ProviderCallRecord.metadata['response_prefix']` gives a
  redacted view of any malformed/truncated body for triage.

---

## 8. Out of Scope

- Raising `_MAX_TOKENS_BY_KIND` caps as the fix (explicit non-goal).
- Removing the reduce stage (explicit non-goal).
- Any change to extract batching, extract prompt, or extract snapshot contract.
- Any change to finalization, curation, retrieval, or the candidate-decision path.
- Per-batch coordinate splitting and any new table/column (rejected; see Design C).
- Backfilling / resetting the existing `TERMINAL_FAILURE` reduce works from §1. This slice
  fixes forward behavior only; recovering the 77 terminalized sessions is a separate ops
  action (§7) or they are simply dropped under the dogfood directive.
- Tuning `_OUTPUT_TOKENS_PER_CHAR` empirically per model (safe conservative constant now;
  future work if fan-in proves too small in practice).

---

## 9. Review Reconciliation

(append-only)

Round 1 (2026-07-21, adversarial spec review):

- Finding 1 [BLOCKER] `distill_reduce.v2` corrupts real-gateway body via
  `_STRUCTURED_RESPONSE_KINDS` omission — FIXED. §3.1 now enumerates the full v1->v2 rename
  surface, flags `_STRUCTURED_RESPONSE_KINDS` (`services.py:1153-1155`) as load-bearing for
  `_completion_body`/`_completion_title` (`:1810-1821`), and §6.3 adds real-gateway
  reduce-body test 24 (fake-provider tests bypass `_completion_body`).
- Finding 2 [MAJOR] Contradictory terminal code for truncation-exhaustion — FIXED. §3.6
  raises a distinct `ReductionTruncationExhausted(ReductionContractError)`; §3.7 splits the
  planning `except` so it maps to `distillation_reduction_truncation_exhausted` while other
  `ReductionContractError` keep `distillation_reduction_plan_invalid`; test 8 updated.
- Finding 3 [MAJOR] 77 already-failed sessions not recovered (no backfill) — FIXED (as
  explicit scope statement). Confirmed terminalization (`work_execution.py:887-888`) and
  no-re-claim (`:463/530`). Spec made no backfill claim; §7 now states the fix is
  forward-only and recovery needs an explicit reset (PENDING + streak 0) or drop under the
  dogfood directive; §8 lists backfill as out of scope for R1.
- Finding 4 [MINOR] `_run_stage` `_TruncatedOutcome` branch unspecified — FIXED. Confirmed
  fall-through would hit `outcome.error` (`:1168`) AttributeError; §3.4 now specifies the
  branch (record marker, `_truncated_failure`, STAGE_RETRY, no fallback, widened return type).
- Finding 5 [MINOR] `max_tokens` override bleeds into extract/curation — FIXED. §3.3/§7/test
  14 scope the override to `distill_reduce.v2` only, keeping extract/curation on fixed caps
  per §8.
- Finding 6 [MINOR] Determinism invariant overstates frozenness — FIXED. §5 now states
  `output_budget` is re-derived from the live policy (not window-frozen); a mid-window policy
  edit re-plans and drops in-flight stages (benign under the droppable directive); invariant
  reworded to "determinism given a fixed policy".

Round 2 (2026-07-21, adversarial spec review):

- Finding 1 [MINOR] §7 "never re-selected" claim relies on the wrong mechanism; accepted-set
  `batch_key` is prompt_contract-independent so a residual COMPLETE v1 reduce row can collide
  with a v2 gen-0 batch — CONFIRMED, FIXED. Verified `_accepted_stage_rows`
  (`distillation.py:635-644`) has no prompt_contract filter, `_AcceptedReduction.batch_key`
  (`distillation_reduction.py:565-567`) via `reduction_batch_key` (`:283-291`) excludes
  prompt_contract, duplicate raise at `:586` / accepted-row consumption at `:605`, v1 levels
  overlap v2 gen-0 levels 1..4. §3.7 now adds a `prompt_contract='distill_reduce.v2'` filter
  to the REDUCE accepted-set (and the REQUIRED truncation-marker) query as the real
  disjointness enforcement; §7 corrected to stop attributing safety to a per-contract
  `target_key`; test 25 added.
- Finding 2 [MINOR] `reduce_multilevel` new signature unspecified while callees re-signatured
  and parser drops `reduction_target` — CONFIRMED, FIXED. Verified `reduce_multilevel`
  (`distillation_reduction.py:379-406`) passes `reduction_target=` to both
  `_evaluate_draft_reduction_state` (`:389`) and `parse_reduction_output` (`:397`). §3.6 now
  specifies its `(reduction_target_floor, output_budget_tokens, generation, provider)`
  signature and that it calls the parser with no `reduction_target`.

Round 3 (2026-07-21, adversarial spec review):

- Finding 1 [MINOR] `normalize_output` is an unenumerated second caller of the re-signatured
  `parse_reduction_output` — CONFIRMED, FIXED. Verified `normalize_output`
  (`distillation_reduction.py:743-744`) reads `stage.window.reduction_target` and passes
  `reduction_target=` to the parser; under §3.5 (parser drops the kwarg, `:212`) this is a
  `TypeError` on every real-gateway reduce body. §3.5 now enumerates `normalize_output` as the
  second caller and specifies the exact edit (drop `reduction_target=`; delete the
  `stage.window.reduction_target` read); test 24 covers it.
- Finding 2 [MINOR] §3.7 pseudocode references an undefined `reduce_stage_rows`; binding it to
  the COMPLETE-only accepted set silently disables truncation remediation — CONFIRMED, FIXED.
  Verified `_accepted_stage_rows` (`distillation.py:635-644`) hard-filters `status=COMPLETE`, so
  `s.status == REQUIRED` would always be False, pinning `generation` at 0 and re-planning the
  frozen truncated batch forever (the §1.5 defect). §3.7 pseudocode now binds a distinct
  `truncated_marker_rows` query (v2 REQUIRED reduce stages with
  `last_failure_class=PROVIDER_OUTPUT_TRUNCATED`), explicitly separate from the accepted set;
  tests 13/23 exercise the generation bump.

Round 4 (2026-07-21, adversarial spec review):

- Finding 1 [MINOR] `completion_clamp` is an unscoped ops lever that also clamps the
  out-of-scope extract/curation request, unlike its deliberately reduce-scoped `max_tokens`
  twin — CONFIRMED, FIXED. Verified the request `max_tokens` is set at the SHARED call site
  `services.py:1577` for every `response_kind`, and §3.3 makes it `effective_completion_cap`,
  whose `provider_completion_clamp` (reads `policy.metadata['completion_clamp']`) takes no
  `response_kind`; extract fixed cap 8192, deepseek clamp default 4096 (`:1179`/§3.3). On a
  shared policy a lowered `completion_clamp` would clamp extract, which has no truncation
  detection this slice (§8), into `provider_output_malformed`. §3.3 now scopes the clamp to
  `distill_reduce.v2` only (symmetry with the override): `effective_completion_cap` returns the
  unclamped `resolve_max_tokens` for non-reduce kinds, leaving extract/curation at exactly
  today's request value; §7 `completion_clamp` bullet and test 16 updated accordingly.
- Finding 2 [MINOR] reduce provider-call volume rises ~15-25x per window on deepseek — CONFIRMED
  (magnitude), ACCEPTED as by-design cost of decision A, no code change. Reviewer states it is
  not a defect and requires no implementation change; the old char-budget path was ~100% failing
  so it is not a regression from a working state. §3.2 now records the magnitude explicitly
  (~75 calls for a 100-draft deepseek session, bounded per attempt by
  `max_provider_calls_per_attempt`, mitigated by the §7 fan-in lever) so the plan is honest.

Round 5 (2026-07-21, codex cross-check of the implemented slice):

- Finding 1 [BLOCKER] v1/v2 reduce stage coordinate collision: `core_distill_stage_coord_uniq`
  (`core/models.py:2041`) excluded `prompt_contract`, so the v2 planner's `get_or_create`
  (`distillation_provider_stage.py:562`, keyed on the v2 `stage_key`) misses a residual COMPLETE
  v1 reduce stage sharing `(window, stage_kind, level, ordinal, policy, policy_version,
  policy_role)` and the INSERT raises `IntegrityError` on any re-driven window — CONFIRMED, FIXED.
  Migration `0047_distill_stage_coord_prompt_contract` (verified next free number on this branch;
  leaf was `0046_merge_20260721_1032`) rebuilds the constraint with `prompt_contract` added, so v1
  and v2 rows coexist at one coordinate; the v2 accepted-set filter (§3.7) already ignores v1 rows.
  Tests: `core/migrations_tests.py::test_0047_coord_uniqueness_admits_distinct_prompt_contracts_at_one_coordinate`
  (MigrationExecutor: pre-0047 duplicate-contract INSERT raises IntegrityError, post-0047 distinct
  contracts coexist while a same-contract duplicate still collides);
  `distillation_tests.py::test_reduce_stage_coordinate_permits_distinct_prompt_contracts` (ORM
  coexistence at one coordinate) and `::test_reduce_planner_redrives_v2_over_residual_v1_stage_cleanly`
  (full redrive over a residual v1 stage finalizes SUCCEEDED with a fresh v2 gen-0 stage at the same
  coordinate). Red before the migration was the IntegrityError those tests now avoid.
- Finding 2 [MAJOR] Confidence provenance was model-trusted, not enforced — FIXED. `parse_reduction_output`
  (`distillation_reduction.py`) now deterministically CLAMPS each memory's confidence to
  `min(model_confidence, max(source draft confidences))` — a clamp, never a reject; the reduce schema
  prefix `_DISTILL_REDUCE_SCHEMA_INSTRUCTIONS` (`model_policy/services.py`) mirrors the system-prompt
  rule sentence ("Give each memory a confidence no higher than the highest confidence among its source
  drafts"). Tests: `distillation_reduction_tests.py::test_parse_reduction_output_clamps_confidence_to_source_draft_ceiling_without_rejecting`
  and the updated `services_tests.py::test_distill_reduce_schema_prefix_states_parser_enforced_rules`.
- Finding 3 [MAJOR] No fingerprint pin on the prompt contract — FIXED. `services_tests.py` adds
  `test_reduce_prompt_contract_components_are_fingerprint_pinned_editing_them_requires_a_contract_version_bump`
  and `test_extract_..._requires_a_contract_version_bump`, pinning sha256 of `_REDUCE_SYSTEM_PROMPT`,
  `_DISTILL_REDUCE_SCHEMA_INSTRUCTIONS`, `_EXTRACT_SYSTEM_PROMPT`, `_DISTILL_EXTRACT_SCHEMA_INSTRUCTIONS`
  against literal constants; the test names carry the contract-version-bump obligation (no comments).
- Finding 4 [MAJOR] Spec overclaimed truncation is impossible — FIXED (spec wording) + test. §2.A now
  states truncation is made rare-by-planning (0.4 tokens/char is not a true upper bound: CJK density
  and JSON escaping can exceed it) and the split-backstop, not the estimator, guarantees progress.
  `distillation_tests.py::test_escape_heavy_reduce_batch_truncates_once_then_recovers_via_split_backstop`
  drives an escape-heavy batch that truncates once and recovers to SUCCEEDED via a generation bump.
- Finding 5 [MINOR] Spec used a non-existent `execution_state = PENDING` — FIXED. §7 recovery step now
  uses the real `WorkflowWorkExecutionState.READY` value (`core/models.py:1216`; there is no PENDING member).
- Finding 6 [MINOR] `_MalformedOutcome` final shape / spec drift — FIXED (spec) + verified sound (code).
  The implemented truncation-as-first-class code is the source of truth: `_MalformedOutcome` carries
  `response_prefix` + `error_detail`; `error_detail` is threaded through
  `_malformed_failure(...).redacted_detail` into `WorkflowRun.failure_reason` (`work_execution.py:849`),
  and the ~2k redacted `response_prefix` is written to `ProviderCallRecord.metadata['response_prefix']`
  by `_record_stage_failure_diagnostics`. §3.4 realigned to the exact dataclass field order (including
  the corrected `_MalformedOutcome(response_hash, response_size, response_prefix, error_detail,
  provider_call_ids)` positional order) and to the error_detail->failure_reason threading. No code
  change required.
- code-review round 3, finding 1, verdict fixed — binding formula reconciled to the implemented floor-above-cap semantics: max(floor, min(48, ceil(total_drafts / 4))); a configured window.reduction_target above 48 is preserved (regression-tested), the 48 cap applies only to the scaled component.
