# Provider Replay-Echo Security Review

Date: 2026-07-03

Branch: `fix/provider-replay-echo`

Result: PASS.

## Scope

- `apps/backend/engram/model_policy/services.py` — removal of `ProviderCallRecord`
  dedup/reuse short-circuits from `FakeProviderGateway`, `OpenAICompatibleGateway`,
  and `AnthropicMessagesGateway`, replaced with a shared `_log_repeat_attempt`
  helper that only logs on a repeated `(organization_id, project_id, task_type,
  request_id)` tuple.
- `apps/backend/engram/model_policy/model_policy_tests.py`,
  `apps/backend/engram/model_policy/real_provider_tests.py`,
  `apps/backend/engram/memory/curation_tests.py`,
  `apps/backend/engram/memory/memory_digest_tests.py`,
  `apps/backend/engram/memory/memory_worker_tests.py`,
  `apps/backend/engram/memory/services_tests.py` — regression coverage for the
  behavior change (fresh provider calls on retry, no prompt echo, no dedup
  reuse of a stale/orphaned `ProviderCallRecord`).

This is a focused re-review of a bug fix to the model-policy provider gateway
(retry/replay handling). It does not touch provider secret storage/rotation,
policy resolution/scoping, or new provider integrations — those remain covered
by `2026-06-25-model-policy-secrets.md` and are unaffected by this diff.

## Commands/tools run

| Check | Result |
| --- | --- |
| `git diff -- apps/backend` (full read) | Reviewed; change is confined to `engram/model_policy/services.py` (dedup removal) and six test files (regression coverage). |
| Focused test run | Exit 0. `docker compose -f deploy/compose/docker-compose.yml run --rm api sh -ec "poetry run pytest -q engram/model_policy/model_policy_tests.py engram/model_policy/real_provider_tests.py engram/memory/curation_tests.py engram/memory/memory_digest_tests.py engram/memory/memory_worker_tests.py engram/memory/services_tests.py"` reported 173 passed. |
| Ruff lint | Exit 0. `docker compose -f deploy/compose/docker-compose.yml run --rm api sh -ec "poetry run ruff check engram/model_policy engram/memory"` reported all checks passed. |
| Ruff format | Exit 0. `docker compose -f deploy/compose/docker-compose.yml run --rm api sh -ec "poetry run ruff format --check engram/model_policy engram/memory"` reported 44 files already formatted. |
| Manual read of `_log_repeat_attempt` and `_record_call` | Confirmed log fields and persisted `ProviderCallRecord` fields against the pre-existing redaction contract. |

## Findings by severity

### CRITICAL

None found.

### IMPORTANT

None found.

### MINOR

None found.

## Cadence checklist (goal.md items a-e)

- (a) Nothing model-generated or prompt-derived is newly persisted or logged.
  `_log_repeat_attempt` logs only `request_id` and `task_type`
  (`apps/backend/engram/model_policy/services.py:714`); it does not touch
  `data.prompt`/`data.text` or any generated title/body. `_record_call`
  (unchanged by this diff) still persists `metadata={'prompt_retained': False,
  'transport': 'http'}` with no prompt/response body field on
  `ProviderCallRecord`. Verified directly by
  `test_fake_provider_gateway_logs_repeated_request_id_without_prompt_text`,
  which asserts `secret_prompt not in str(events[0])` over the captured
  structlog event.
- (b) Provider-secret redaction posture unchanged. `redact_value(data.prompt)`
  / `redact_value(data.text)` calls and the `ProviderSecretEnvelope.objects
  .filter(secret=secret, active=True).exists()` gate ahead of every call/embed
  path are untouched by the diff; the only change under each `if not
  ProviderSecretError` check is that the old inline dedup block was replaced
  by a call to `_log_repeat_attempt(data)`.
- (c) Tenant/project scoping of the `ProviderCallRecord` existence check is
  unchanged. Before: `.filter(organization_id=..., project_id=...,
  task_type=..., request_id=...).order_by('created_at').first()`. After:
  the same four-field filter, now via `.exists()` inside
  `_log_repeat_attempt`. No field was dropped, widened, or reordered across
  the four call sites (`FakeProviderGateway.call`, `FakeProviderGateway.embed`,
  `OpenAICompatibleGateway.call`/`.embed`, `AnthropicMessagesGateway.call`).
- (d) Re-billing exposure from removing the dedup short-circuit is bounded.
  Every worker entry point that drives a provider call is a Celery task with
  `max_retries=3` and exponential backoff (`_RETRY_BACKOFF_BASE ** (retries +
  1)`, base 5 -> 5s/25s/125s) in
  `apps/backend/engram/memory/tasks.py:27-56` (and the matching pattern for
  the other three worker tasks in that file), so a duplicate delivery can at
  most re-issue the same provider call a small, fixed number of times before
  the task gives up. Each repeat is now observable via the
  `provider_request_repeated` warning log rather than being silently absorbed
  by the old reuse path.
- (e) Regression tests prove no prompt-echo escapes. New coverage:
  `test_gateway_call_never_echoes_prompt_on_repeated_request_id` in
  `real_provider_tests.py` (parametrized across `OpenAICompatibleGateway` /
  `AnthropicMessagesGateway` and `single` / `candidates` / `curation_judgment`
  response kinds) asserts the second call re-hits the transport (`len(opener
  .requests) == 2`), returns the second response body/title rather than the
  first, and that the literal `prompt_text` marker never appears in the
  generated title or body. `test_fake_provider_gateway_logs_repeated_request_id_
  without_prompt_text` covers the fake gateway's log line specifically. The
  memory-layer tests (`curation_tests.py`, `memory_digest_tests.py`,
  `services_tests.py`) add stale/pre-existing `ProviderCallRecord` fixtures
  and assert the worker still performs a fresh provider call and does not
  surface stale/echoed content (e.g. `'Orphan record source' not in
  result.memory.body`, `provider_prompt(observation) not in
  result.candidate.body`).

## Fixes applied or accepted risk

No fixes were required; the diff under review is itself the fix for the
replay-echo bug (a prior dedup short-circuit could return a stale
`ProviderCallResult` — including a previous prompt-derived generated body —
for a repeated `request_id` instead of making a fresh provider call). No
new findings were raised in this pass.

Accepted risk: none beyond what is already accepted in
`2026-06-25-model-policy-secrets.md` (real network calls, secret vault
backend, etc.), which this diff does not change.

## Regression tests added

All added by the diff under review (not by this security pass):

- `apps/backend/engram/model_policy/model_policy_tests.py`:
  `test_fake_provider_gateway_makes_fresh_call_for_repeated_request_id`,
  `test_fake_provider_gateway_logs_repeated_request_id_without_prompt_text`,
  `test_fake_provider_gateway_embed_redacts_input_and_records_fresh_call`,
  plus updated assertions in
  `test_fake_provider_gateway_returns_deterministic_memories_object_for_candidates_kind`.
- `apps/backend/engram/model_policy/real_provider_tests.py`:
  `test_gateway_call_never_echoes_prompt_on_repeated_request_id`
  (6-way parametrized),
  `test_openai_compatible_gateway_call_makes_fresh_provider_call_on_repeat`.
- `apps/backend/engram/memory/curation_tests.py`:
  `test_curate_judge_reject_applies_even_with_pre_existing_provider_call_record`.
- `apps/backend/engram/memory/memory_digest_tests.py`:
  `test_generate_digest_makes_real_call_when_provider_record_exists_without_memory`.
- `apps/backend/engram/memory/memory_worker_tests.py`: updated
  `test_observation_recorded_worker_is_idempotent_for_duplicate_delivery`,
  `test_observation_recorded_worker_reuses_existing_candidate`,
  `test_index_memory_version_embedding_reindex_makes_fresh_call_with_stable_vector`.
- `apps/backend/engram/memory/services_tests.py`:
  `test_process_observation_generation_uses_fresh_provider_response_on_repeated_request_id`.

All 173 tests in the touched modules pass (see Commands/tools run).
