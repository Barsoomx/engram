# Memory Loop Systemic Fix

Date: 2026-07-03
Status: IMPLEMENTED — all sections merged to master. §2.1 json-mode gating
(#138) + code-fence stripping companion (#143) = the prod unblock (awaiting
deploy to go live). Follow-ups all landed: §2.3+§2.4 gateway hardening —
failure ProviderCallRecords + real latency + OpenAI max_tokens (#146); §2.7
chunk-budget/timeout decouple + batch prefetch=1 + rabbitmq consumer_timeout
(#147, broker changes apply at deploy/restart); §2.5 distillation reconciler,
cap 2 (#148); §2.2 call-time provider fallback (#149); §2.6 policy health
fields + threshold advisory + engram_validate_policies command (#150) and the
console UI + create-controls (#151). Deploy of master's :latest is the
remaining step to make it all live on prod.
Scope: session distillation reliability, structured-output capability gating,
provider fallback, policy validation/health, promotion gating, Celery/RabbitMQ
alignment, loop observability.
Evidence base: repo master `b463040c`; read-only inspection of prod
`engram.tools.byster.one` (DB queries, container/image inspection, RabbitMQ
state, worker logs, unauthenticated provider probe); controlled real provider
calls from the staging Django shell (ground truth for current behavior).

## 1. Root-cause model

The symptom ("learn-codebase e2e produces zero retrievable memory,
`selected_count=0` in context bundles") is the product of two independent
failures plus one structural gap that made them permanent.

### 1.1 Failure A — per-observation confidence ceiling vs auto-approve threshold (PROVEN)

- `OrganizationSettings.distillation_auto_approve_threshold = 0.700` on prod.
  The field is nullable with no default (`core/models.py:143`); 0.700 was set
  explicitly through the console settings view. The code fallback when NULL is
  `ENGRAM_DISTILLATION_AUTO_APPROVE_THRESHOLD = 0.500`
  (`settings/settings.py:174`).
- `derive_observation_confidence` (`memory/services.py:435`) is additive:
  base 0.50, +0.10 facts, +0.10 files, +0.10 narrative, +0.05 concepts,
  +0.10 durable type. Learn-codebase file-read observations score 0.500-0.600,
  below 0.700, so every per-observation candidate takes the
  `MemoryCandidateHeldForReview` branch (177 held candidates on prod, 44 from
  the e2e session). Held candidates are never promoted, never embedded, never
  indexed into `core_retrievaldocument`.
- This is working as designed *if* distillation is healthy: session
  distillation instructs the model to emit `confidence: 0.9` for verified
  facts, which clears 0.700 and promotes through curation. Per-observation
  candidates are the conservative fallback tier feeding the human review
  queue. The defect is not the threshold itself but that the loop has no
  visibility and no degradation story when the promoting tier (distillation)
  is dead. This is a separate design tension from Failure B and is addressed
  only with health surfacing (§2.6), not with automatic threshold changes.

### 1.2 Failure B — session distillation dead (PROVEN, two eras, one workflow)

**Definitive current root cause (ground truth from controlled staging
probes):** `openai_json_mode_override()` (`model_policy/services.py:874`)
unconditionally sends `response_format: {'type': 'json_object'}` for the
structured response kinds (`candidates`, `curation_judgment`). The
`deepseek-v4-flash` endpoint rejects that parameter with **HTTP 400 in
0.3s**; `deepseek-v4-pro` accepts it. Probe matrix:

| policy / model | response_kind | result |
|---|---|---|
| generation `deepseek-v4-pro` | single | OK |
| curation `deepseek-v4-flash` | single | OK (1.2s) |
| curation `deepseek-v4-flash` | candidates (json mode) | **FAIL: HTTP 400 in 0.3s** |
| digest `deepseek-v4-flash` | single | OK |
| `deepseek-v4-pro` | candidates (json mode) | OK, valid `{"memories":[...]}` |
| `deepseek-v4-flash` | candidates **without** `response_format` | OK, valid parseable `{"memories":[...]}` |

Consequences on current master:

- `json_object` support is a **model-specific capability, not
  provider-wide**; the override treats it as universal.
- The 400 maps to `ModelPolicyError('provider_http_error', 'provider returned
  400', retryable=False)` — **non-retryable**, so `distill_session` gets zero
  Celery retries and the WorkflowRun fails permanently on the first chunk
  call, instantly.
- The curation judge and the distillation reduce call use the same structured
  kinds and hit the same 400 on flash; the judge degrades gracefully to
  `keep_both`, the chunk/reduce path does not.
- The model is *capable* of the task: without `response_format`, flash still
  returns valid, parseable `{"memories":[...]}` — the system prompt already
  mandates a single JSON object and `parse_synthesized_candidates` degrades
  gracefully. `json_object` is an optimization, not a requirement.

**Historical era (the five observed "provider timed out" runs):** those runs
(2026-07-02 18:27-18:32 UTC, each exactly 33s, `provider_call_ids: []`)
executed on the previous image, which predates both #135 (merged 19:45 UTC:
configurable timeouts — before it, `_open` hardcoded `timeout=30` — and
observation chunking) and #136 (21:25 UTC: which *introduced*
`response_format`). So the historical mechanism was hardcoded-30s-timeout +
one giant unchunked prompt (33s = 30s socket timeout + ~3s session load); it
could not have been the json-mode 400 because json mode did not exist in that
code. Two different mechanisms across two image generations killed the same
workflow back-to-back — which is itself evidence for the structural gap in
§1.3. The historical era is moot on the current image; the 400 is what blocks
the loop now.

Supporting facts (verified):

- Zero `deepseek-v4-flash` ProviderCallRecords ever ≠ "model down": records
  are only written after a successful HTTP response, distillation was the
  only volume caller of the curation policy, `curator_llm_judge_enabled`
  defaults to False, and daily digests had no source memories. Failures leave
  **no record at all** and `latency_ms` is hardcoded 0 — the outage was
  invisible in the audit surface.
- Network/broker are healthy: `api.deepseek.com` answers 401 in 0.29s from
  the prod host; RabbitMQ queues empty with consumers attached
  (`consumer_timeout` = default 1800000ms); celery_outbox and dead-letter
  tables empty; distill `time_limit` overridden to 600/660s. Not RabbitMQ,
  not Celery limits.
- The weekly digest that *does* exist is `BuildWeeklyStructuredDigest`
  (`memory/services.py:1409`) — deterministic, no provider call, and it
  creates a Memory but **no MemoryVersion/RetrievalDocument**, which is why
  it is the only Memory row yet retrieval stays empty.

### 1.3 Structural gap — failures are terminal and invisible (PROVEN)

- `distill_session` retries at most 3 times (and not at all for
  non-retryable errors like the 400); afterwards the session is dead forever:
  `SweepStaleSessions` only considers `status=ACTIVE` sessions and the
  SessionEnd hook enqueue (`hooks/services.py:150`) fires once. There is no
  reconciliation for sessions whose distillation failed. The two e2e sessions
  on prod are in this state now.
- No ProviderCallRecord on failure means neither the API nor the console can
  show "this policy has never worked". The loop can silently deliver
  nothing, which is exactly what happened — twice, for different reasons.

## 2. Systemic fix design

Design goal: the loop must survive a model-capability mismatch or the failure
of any single model policy, must retry failures beyond the Celery retry
window, and must make "this tier is unhealthy" visible before the user
notices empty context. Constraint: manual prod DB/policy mutation is blocked
by policy, so every unblock must land as code + deploy. Six small,
independent pieces; no new models, no new services.

### 2.1 Capability-gate `response_format: json_object` per ModelPolicy (the unblock; PR in flight)

Change `openai_json_mode_override(response_kind)` to
`openai_json_mode_override(policy, response_kind)`: emit
`{'response_format': {'type': 'json_object'}}` only when the policy's
provider is known to support it — default **ON for `openai`**, default
**OFF for `deepseek`** and any other provider — with
`metadata.supports_json_response_format: true|false` overriding the default
in either direction.

Why: proven by probes — flash produces valid `{"memories":[...]}` from the
prompt contract alone, and the parser already tolerates non-JSON, so
`json_object` is an optimization. A provider-aware default converts the hard
400 into at worst a parse-fallback while keeping the enforcement where it is
known safe, and the per-policy override handles model-level exceptions
within a provider (e.g. explicitly enabling it for v4-pro). The Anthropic
gateway is untouched (tool-forcing is a different, working mechanism). This
one change self-heals the deployed config with the existing policy rows — no
DB change, no policy repoint — and is the only slice deploying immediately.

### 2.2 Call-time provider fallback (make `fallback_enabled` real)

`ModelPolicy.fallback_enabled` exists (default False) and `UpdateModelPolicy`
already accepts it, but nothing reads it. Semantics:

- Applies where a provider failure is fatal to the workflow: distillation
  chunk + reduce calls, and `GenerateDigest`. (The curation judge and
  embedding paths already degrade gracefully and stay as they are.)
- Trigger: **any** `ModelPolicyError` from the resolved policy's call —
  including non-retryable ones. The observed 400 is non-retryable
  (400 ∉ {429, 5xx}); a retryable-only trigger would NOT have fired for this
  incident and would not fix it. §2.1 makes this specific 400 moot, but the
  any-error trigger stays load-bearing for the rest of the class
  (capability-shaped 4xx from other models/params, e.g. a future
  `max_tokens`/`thinking` rejection). If `policy.fallback_enabled` is true,
  re-resolve with `task_type='generation'` and retry the same call once
  through the fallback gateway. If the fallback resolves to the same policy,
  raise as today.
- Sticky within one distillation run: after the first fallback, subsequent
  chunk/reduce calls in that run use the fallback gateway directly, so the
  worst case pays the primary failure once, not per chunk.
- Audit `ProviderFallbackUsed` (policy ids, task_type, error code) and carry
  fallback provenance in candidate evidence (the `_provenance` dict already
  records provider/model/policy per call).

Why this shape: it reuses the exact resolution ladder distillation already
uses when a curation policy is *absent* (`DistillSession._resolve_policy`
falls back to generation at resolve time); we extend the same ladder to
call-time failure. It encodes the observed reality: the generation policy
(`deepseek-v4-pro`, 813 successes, json-mode capable) is the proven-healthy
sibling. `allowed_providers`/`blocked_providers` chains are deliberately left
unused — a one-hop fallback to the generation policy removes the single point
of failure without inventing a routing DSL; the fields stay reserved for a
future slice. Cost is bounded: ≤ (max_chunks + reduce) = 9 fallback calls per
session distill.

Time-budget note: worst case per run is
`1 primary failure + 9 fallback calls ≈ 10 × ENGRAM_PROVIDER_HTTP_TIMEOUT
(60s) = 600s`, exactly the soft time limit. `SoftTimeLimitExceeded`
propagates through `run_session_distillation_with_tracking`'s
`except Exception`, marks the run FAILED, and §2.5 retries later. Document
the envelope: `ENGRAM_DISTILL_SOFT_TIME_LIMIT ≥
(ENGRAM_DISTILL_MAX_CHUNKS + 2) × ENGRAM_PROVIDER_HTTP_TIMEOUT`.

### 2.3 Record failed provider calls

In both gateways, when `_open` raises `ModelPolicyError`, create a
ProviderCallRecord before re-raising, with `result` set to a new
choices value (`'error'` added alongside allowed/denied/recorded),
`metadata={'error_code': ..., 'http_status': ...}` and measured `latency_ms`
(also fix `latency_ms=0` on the success path). No schema change beyond the
choices addition (no-op migration).

Why: the highest-leverage observability fix — the entire incident produced
zero rows of provider evidence, and the historical-vs-current mechanism
confusion (§1.2) existed only because failures were unrecorded. A recorded
`provider returned 400` row on 2026-07-03 would have made root cause a
one-query diagnosis. Also feeds §2.6 health without polling or paid calls.

### 2.4 Bound provider generation time (`max_tokens` on OpenAI-compatible calls)

`resolve_max_tokens` (8192 for `candidates`, 1024 for `curation_judgment`,
per-policy override via `metadata.max_tokens`) exists but is wired only into
`AnthropicMessagesGateway`. Add it to `OpenAICompatibleGateway`
`_chat_completion` payloads.

Why: hardening, not the unblock — bounds completion latency/cost for large
distill chunks and is required by DeepSeek's own guidance when JSON output is
expected; prevents the unbounded-generation timeout class from returning once
json mode is re-enabled per policy.

### 2.5 Distillation reconciler (repair path for dead sessions)

New beat task `engram.memory.retry_failed_distillations` (every 30 min, batch
queue): enqueue `distill_session` for sessions where

- `status=ENDED`, observation count > 0,
- no SUCCEEDED `session_distillation` WorkflowRun for the session,
- latest run FAILED and `finished_at` older than a cooldown (30 min),
- total failed runs below a cap (default 10 — WorkflowRun rows are the
  attempt counter; no new fields).

Why: today a session gets exactly one enqueue and at most three in-process
retries (zero for non-retryable errors); any outage or capability mismatch
permanently loses that session's memory. The reconciler converts terminal
failure into eventual delivery and will heal the two dead prod sessions
automatically once the §2.1 fix deploys — important because manual
re-enqueue/DB mutation on prod is blocked by policy. Idempotency is already
guaranteed by candidate `content_hash` dedupe and propose-only re-runs
(`_classify_existing`). The cap bounds paid retries against a permanently
broken config; hitting the cap is visible as N failed runs plus §2.6 health.

### 2.6 Model-policy validation and loop-health surfacing

- Model-policy list serializer gains read-only computed fields per policy:
  `last_success_at`, `recent_error_count` (from ProviderCallRecords including
  §2.3 failure rows). Console Models page shows a "never succeeded / failing"
  badge — a policy like flash-curation can no longer be silently dead.
- `engram_validate_policies` management command (operator-triggered, also
  callable from the console model-setup flow): for each active policy, run
  one tiny provider call **in the same shape the workflows use** — i.e. a
  structured `candidates` call for curation policies, honoring the json-mode
  flag — and report pass/fail per policy. This catches exactly this class of
  capability mismatch at setup time instead of at first distillation.
  Cost-bounded (one minimal call per policy, on demand only).
- Console settings PATCH for `distillation_auto_approve_threshold` returns an
  advisory (non-blocking) when the value exceeds the per-observation ceiling
  reachable by `derive_observation_confidence` for common observation shapes
  (> 0.6): "per-observation candidates will always be held; session
  distillation must be healthy". Documents the two-tier design instead of
  silently arming Failure A.

Deliberately not doing: automatic threshold lowering, LLM-scored
per-observation confidence, scheduled paid health pollers, multi-hop
provider routing (`allowed_providers` DSL), streaming responses, automatic
capability probing on policy save. Each is more machinery than the failure
warrants.

### 2.7 Config decoupling (small but load-bearing)

`_distill_chunk_char_budget` couples chunk size to the HTTP timeout
(`ceiling = provider_http_timeout() * 2000`). This is perverse under
operation: raising the timeout to survive slow chunks *grows the chunks*.
Replace the coupling with an explicit `ENGRAM_DISTILL_CHUNK_CHAR_CEILING`
(default 120000, same effective value as today) while keeping the
context-window-derived budget and the floor. Timeout then only means timeout.

## 3. Blast radius

| Area | Impact | Change type |
|---|---|---|
| Provider gateways | §2.1 json-mode gate + §2.3 failure records + §2.4 max_tokens are all in `model_policy/services.py`; Anthropic gateway untouched except failure recording. Behavior change for OpenAI-compatible structured calls: json mode now provider-aware (openai on, deepseek/other off) with per-policy metadata override; parser fallback already tolerates prompt-contract-only output; fake gateway unaffected. | code |
| Celery routing/queues | None structural. `distill_session` stays on `engram-batch`; reconciler joins the batch beat schedule. | code |
| Celery prefetch | Latent hazard, not this outage: batch worker runs default `prefetch_multiplier=4` + `acks_late=True`; under a burst of ≥ ~3×concurrency long distills a prefetched message can sit unacked > 30 min → RabbitMQ `consumer_timeout` closes the channel, mass-redelivers, and quorum `delivery-limit` (default 20 in RabbitMQ 4) can eventually drop messages. Fix: `--prefetch-multiplier=1` on `worker-batch` in compose. | infra (compose) |
| RabbitMQ | `consumer_timeout` currently default 1800000ms — confirmed not the cause. Set explicitly to 3600000 in `deploy/compose/rabbitmq.conf` for headroom over `time_limit=660` and redelivery storms; requires broker restart (queues are quorum, drainable). | infra (config + restart) |
| Provider retry/cost | Fallback adds ≤ 9 generation-policy calls per failed-primary distill run; `max_tokens` caps output cost; reconciler capped at 10 attempts/session; validate command is on-demand only. Failure records add DB rows bounded by call volume. | code |
| Data model / migrations | One TextChoices addition on `ProviderCallRecord.result` (no-op migration). Policy capability flag and max_tokens live in existing `metadata` JSON — no schema change. No new tables; reconciler state = existing WorkflowRun rows. `OrganizationSettings` untouched. | code (safe migration) |
| API / serializers | Model-policy list gains computed health fields (additive); create/update serializers gain `supports_json_response_format` passthrough into metadata (additive); settings PATCH gains advisory in response (additive). `fallback_enabled` already writable. | code |
| Frontend | Models page: health badge, `fallback_enabled` toggle, json-mode capability checkbox; Settings page: threshold advisory. Additive, non-blocking slice. | code (follow-up) |
| Config/env (prod) | No env change is required for the unblock (the 400 is instant; timeouts are not the binding constraint). Optional hardening after code ships: `ENGRAM_PROVIDER_HTTP_TIMEOUT=120`, `ENGRAM_DISTILL_CHUNK_CHAR_BUDGET=40000`. | env (optional) |
| Prod DB/config | **Manual prod DB mutation is blocked by policy** — no manual ModelPolicy repoint, no shell re-enqueue. The unblock is §2.1 shipped and deployed (self-heals with existing policy rows: metadata `{}` ⇒ deepseek default off); the reconciler (§2.5) then re-runs the dead sessions without manual action. Post-deploy, optional console (API) actions: `supports_json_response_format=true` on the v4-pro policy if json enforcement is wanted there, `fallback_enabled=true` on curation/digest policies once §2.2 ships. Threshold 0.700 can stay: distillation candidates (0.9) clear it; the 177 held candidates are servable through the review-queue UI. | code + deploy, then console API |
| Worker sizing | No change; distillation is I/O-bound. `highmemory` queue unaffected (nothing routed there today). | — |
| Security | Fallback resolution stays inside the org's own policy/secret scope (`ResolveModelPolicy` filters by `organization_id`) — no cross-tenant path. Failure records store error codes/status only, never prompts or key material (`prompt_retained: False` preserved). Validate command uses the policy's own secret through the normal gateway. Secret handling, envelopes, and redaction untouched; audit metadata continues through `redact_value`. | — |

## 4. Concrete change list (ordered, TDD-first)

### Phase 1a — PR: json-mode capability gating (MERGED as PR #138, awaiting deploy)

The only slice deploying immediately; smallest change that revives the loop,
no config prerequisite.

Tests first (`model_policy/services_tests.py`, plus
`model_policy/real_provider_tests.py` regression):

- OpenAI-compatible payload **omits** `response_format` for structured kinds
  when provider is `deepseek` (or unknown); **includes** it for `openai`;
  `metadata.supports_json_response_format` overrides in both directions;
  never included for `single`.
- Regression: gateway raising HTTP 400 on a `candidates` call → non-retryable
  `provider_http_error` — pins the bug shape.
- `memory/distillation_tests.py`: distill chunk succeeds against a gateway
  that would 400 on json mode (deepseek default ⇒ no `response_format` ⇒
  valid `{"memories":[...]}` parsed).

Then code: `openai_json_mode_override(policy, response_kind)` + call sites.

Deploy: with existing policy rows (metadata `{}` ⇒ deepseek default off) the
next distillation attempt succeeds on `deepseek-v4-flash` via the prompt
contract, as proven by the staging probe.

### Phase 1b — PR (follow-up): gateway hardening

- Payload includes `max_tokens` from `resolve_max_tokens` for structured
  kinds, honoring `metadata.max_tokens`.
- A raising `_open` produces a ProviderCallRecord with `result='error'`,
  error metadata, measured `latency_ms`, and still raises; success path
  records real `latency_ms`; `result` choices migration.

### Phase 2 — PR (follow-up): fallback + reconciler

- `memory/distillation_tests.py`:
  - chunk call failing with **non-retryable** `provider_http_error` +
    `fallback_enabled=True` on curation policy + healthy generation policy →
    run succeeds, candidates carry fallback provenance,
    `ProviderFallbackUsed` audited;
  - sticky fallback: second chunk goes straight to the fallback gateway;
  - `fallback_enabled=False` → raises exactly as today;
  - fallback resolving to the same policy → raises.
- `memory/services_tests.py`: `GenerateDigest` same trigger matrix.
- Reconciler tests (`memory/tasks_tests.py` or `memory/reconciler_tests.py`):
  eligibility matrix (ended+failed+cooldown yes; succeeded no; capped no;
  active no; zero-observation no), enqueue idempotence.

Then code: gateway-with-fallback wrapper used by
`DistillSession._call_chunk`/`_reduce_candidates` and `GenerateDigest`;
`memory/tasks.py` + `celeryconfig.py` beat entry.

### Phase 3 — PR: infra alignment + budget decoupling

- `deploy/compose/docker-compose.yml`: `--prefetch-multiplier=1` on
  `worker-batch`.
- `deploy/compose/rabbitmq.conf`: `consumer_timeout = 3600000`.
- `memory/distillation_tests.py`: budget uses
  `ENGRAM_DISTILL_CHUNK_CHAR_CEILING`, not the timeout; then
  `_distill_chunk_char_budget` change.
- `.env.example`: document the
  `SOFT_TIME_LIMIT ≥ (MAX_CHUNKS + 2) × HTTP_TIMEOUT` envelope.

### Phase 4 — PR: validation + health surfacing

- Serializer tests for `last_success_at`/`recent_error_count`; validate
  command tests (per-policy pass/fail against fake and erroring gateways,
  workflow-shaped calls); settings PATCH advisory test.
- Then `model_policy/serializers.py`, management command,
  `console/views/settings.py`, frontend badge/toggle/checkbox slice.
- Post-deploy console (API) actions: `supports_json_response_format=true` on
  the v4-pro policy; `fallback_enabled=true` on curation/digest policies; run
  `engram_validate_policies` and confirm all green.

## 5. Risks & rollback

| Change | Risk | Rollback |
|---|---|---|
| json-mode provider-aware gating | Non-openai models lose json enforcement by default → slightly higher parse-fallback rate on marginal models (mitigated: prompt contract + graceful parser + per-policy override; Anthropic tool-forcing unaffected); an openai-compatible proxy registered under provider `openai` that rejects the param would still 400 (override to false per policy) | Revert PR; or flip `supports_json_response_format` per policy via console (no deploy) |
| Regression-pinning 400 test | None (test-only) | — |
| `max_tokens` on OpenAI calls | A provider rejecting the param fails fast as 4xx — now visible via failure records and recoverable via fallback; truncated JSON on very dense chunks (parser degrades per-item) | Revert PR; `metadata.max_tokens` is the operator escape hatch |
| Failure ProviderCallRecords | Row volume under a hard outage (bounded: retries × chunks, then reconciler cooldown); consumers assuming `result='recorded'` must ignore `'error'` rows | Revert PR; additive data, no cleanup needed |
| Call-time fallback (any-error trigger) | Masking a misconfigured primary (mitigated: audit event + health badge keep it loud); double spend when primary flaps (bounded per run); model-quality drift between primary and fallback candidates (provenance recorded) | Set `fallback_enabled=false` per policy (no deploy) or revert PR |
| Reconciler | Paid re-runs against a permanently broken session (capped at 10); duplicate candidates impossible (content_hash) | Remove beat entry / revert PR; cap bounds worst case |
| `--prefetch-multiplier=1` | Slightly lower batch throughput (irrelevant at current volume) | Revert compose line, `docker compose up -d worker-batch` |
| `consumer_timeout=3600000` | Slower detection of a genuinely hung consumer | Revert conf, restart broker (quorum queues preserve messages) |
| Budget ceiling decoupling | None functional at defaults (same effective ceiling) | Revert PR |
| Validate command | Paid calls (one tiny call per active policy, operator-triggered only) | Don't run it; command is inert otherwise |
| Health fields/advisory | Purely additive; advisory may annoy review-only operators | Hide in frontend; API fields stay inert |

## 6. Verification gate for closing the incident

1. Phase 1a deployed → a fresh e2e session (or, once Phase 2 ships, the
   reconciler re-running the dead sessions) produces a `session_distillation`
   WorkflowRun SUCCEEDED with non-empty `provider_call_ids`, using the
   **existing** policy rows.
2. `core_retrievaldocument` count > 0; new context bundle
   `selected_count > 0`.
3. Capability drill (staging): override on for v4-pro → json mode present in
   payload and run succeeds; override on for a non-supporting model → failure
   record with `provider returned 400` (Phase 1b), and with
   `fallback_enabled=true` (Phase 2) the run still succeeds via fallback with
   `ProviderFallbackUsed` audited.
4. Reconciler drill (staging): fail a session's distillation (invalid model,
   fallback off), fix the policy, observe the reconciler deliver the session
   without manual action.
5. `engram_validate_policies` reports pass for all active prod policies.
