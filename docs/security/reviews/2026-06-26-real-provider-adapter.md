# Security Review: Real Provider Adapter

**Branch:** `feat/real-provider-adapter`
**Date:** 2026-06-26
**Reviewer:** independent read-only security review agent (opus); I-1 reconciled
and fixed by the implementation lead.

## Scope

- `apps/backend/engram/model_policy/services.py` — `OpenAICompatibleGateway`,
  `get_provider_gateway`, `decrypt_secret`, `default_base_url`,
  `_resolve_base_url`, `_split_completion`.
- `apps/backend/engram/memory/services.py` — `ProcessObservationRecorded` and
  `GenerateDigest` now route through `get_provider_gateway(resolved.policy)`.
- `apps/backend/engram/context/services.py` — `IndexMemoryVersion` and
  `resolve_query_embedding` route through the factory.

This slice adds real outbound HTTP provider calls (OpenAI-compatible: OpenAI,
GLM/ZhipuAI, OpenRouter, local). The fake gateway stays the default.

## Findings By Severity

### Important — fixed

**I-1. `GenerateDigest` did not gracefully handle provider errors.** The fourth
call site lacked the `try/except (ModelPolicyError, ProviderSecretError)` the
other three sites have, so the first real provider network failure would crash
the digest task instead of being handled. **Fixed in commit `8ef6065e`:**
`GenerateDigest` now wraps the call and raises a clear
`MemoryWorkerError('digest provider unavailable: ...')`. Covered by
`test_generate_digest_wraps_provider_failure_as_worker_error`.

### Minor — accepted

- **M-1.** `redaction_state` checks `'[REDACTED]' in data.prompt` (raw) in
  addition to `redacted_prompt.redacted`. Harmless (only more conservative);
  kept for symmetry with `FakeProviderGateway`.
- **M-2.** Real adapter records `latency_ms=0` and `cost_usd='0.0000'`.
  Operational gap, not security.
- **M-3.** `base_url` is operator-configured with no scheme/host validation
  (operator-side SSRF). Accepted under the trusted-operator model; revisit if
  `ModelPolicy` editing becomes tenant-facing.

## Verified Safe

- **Secret source:** `api_key` comes only from `decrypt_secret(envelope)` of the
  active `ProviderSecretEnvelope` (Fernet). No env fallback, no hardcoded key.
- **No persistence/return/logging of the key:** `ProviderCallRecord.metadata`
  is `{'prompt_retained': False, 'transport': 'http'}`; results carry only ids,
  provider, model, redaction_state, title/body, embedding.
- **Redaction before HTTP:** `redact_value(data.prompt/data.text)` runs before
  the request body is built; raw input never sent.
- **No response-body echo:** `_open` raises `ModelPolicyError` with only
  `error.code`/`error.reason`, never `error.read()`.
- **Replay/idempotency:** `_existing_record` lookup by `request_id` precedes any
  HTTP call, mirroring the fake gateway.
- **Fake default:** `ENGRAM_PROVIDER_MODE` unset/non-`real` returns
  `FakeProviderGateway()`; the deterministic offline suite is unchanged.
- **No key in commits:** grep for `sk-`, `egk_`, `bearer`, `AIza`, `xox`,
  `api_key='...'` literals found none in the diff.

## Accepted Risks

- Operator-side SSRF via `metadata['base_url']` (M-3).
- `Authorization` re-send on same-host 3xx redirect (urllib default).
- `latency_ms`/`cost_usd` not populated for real calls yet (M-2).

## Local e2e (not committed)

Set `ENGRAM_PROVIDER_MODE=real`, create a `ProviderSecret` envelope from an
env-supplied key (`ENGRAM_*_KEY`), run the golden path; confirm a
provider-generated memory lands and is retrievable. The key is injected at
runtime into the encrypted envelope only; nothing is committed. (GLM/ZhipuAI is
configured as `provider='openai'` with
`metadata={'base_url': 'https://open.bigmodel.cn/api/paas/v4'}`.)

## Verdict

**SECURITY APPROVED** after the I-1 fix. Secret handling, redaction, error
containment, idempotency, and the fake default all hold. The real path does not
weaken any existing guarantee.
