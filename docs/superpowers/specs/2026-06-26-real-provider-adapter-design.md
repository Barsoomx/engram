# Real Provider Adapter Design

## Goal

Replace the deterministic-only provider boundary with a real, LLM-agnostic
adapter that calls any OpenAI-compatible generation/embeddings endpoint
(OpenAI, GLM/ZhipuAI at `https://open.bigmodel.cn/api/paas/v4`, OpenRouter,
local servers). This is the production-hardening step the roadmap requires and
unblocks real e2e testing with a local provider key.

The fake gateway stays the default for unit/integration tests so the suite stays
deterministic, offline, and free of provider cost.

## Decision

One `OpenAICompatibleGateway` adapter plus a `get_provider_gateway()` factory.

- The adapter speaks the OpenAI Chat Completions and Embeddings JSON shapes over
  HTTP (stdlib `urllib`, no new dependency). Any OpenAI-compatible endpoint works
  by pointing `base_url` at it.
- `base_url` lives in `ModelPolicy.metadata['base_url']`. If absent, the adapter
  derives a default from `provider` (`openai` -> `https://api.openai.com/v1`).
  GLM/ZhipuAI is configured as `provider='openai'` with
  `metadata={'base_url': 'https://open.bigmodel.cn/api/paas/v4'}`, because GLM
  exposes an OpenAI-compatible API. No new `Provider` choice and no migration.
- The API key comes only from the decrypted `ProviderSecretEnvelope` (Fernet),
  never from code, env files, or commits. The adapter receives the plaintext key
  as a constructor argument and sends it as `Authorization: Bearer <key>`.
- `get_provider_gateway()` returns:
  - `FakeProviderGateway()` when `ENGRAM_PROVIDER_MODE` is unset or `fake`
    (default) — preserves every existing deterministic test;
  - `OpenAICompatibleGateway(base_url, api_key)` when `ENGRAM_PROVIDER_MODE=real`.
- Callers (`ProcessObservationRecorded._generate_candidate`,
  `IndexMemoryVersion._embed_document`, `BuildContextBundle._resolve_query_embedding`,
  `GenerateDigest`) route through `get_provider_gateway()` instead of
  instantiating `FakeProviderGateway()` directly.

## Adapter Contract

`OpenAICompatibleGateway` implements the same two methods as `FakeProviderGateway`:

- `call(data: ProviderCallInput) -> ProviderCallResult` — POST
  `{base_url}/chat/completions` with `{model, messages:[{role:'user',
  content: redacted_prompt}], temperature:0.2}`, parse `choices[0].message.content`,
  split into title/body heuristically (first line title, rest body), write a
  redacted `ProviderCallRecord`, return the result. Reuse-by-`request_id` mirrors
  the fake gateway.
- `embed(data: EmbeddingCallInput) -> EmbeddingCallResult` — POST
  `{base_url}/embeddings` with `{model, input: redacted_text}`, parse
  `data[0].embedding`, return it.

Timeout, retry classification, token/cost metadata, and redaction reuse the
existing provider-call record shape. Errors raise `ProviderSecretError` for
auth/secret failures and `ModelPolicyError` for non-2xx provider responses so the
existing graceful-skip paths apply.

## Boundaries

This slice owns:

- `apps/backend/engram/model_policy/services.py` — `OpenAICompatibleGateway`,
  `get_provider_gateway`, `default_base_url`.
- `apps/backend/engram/memory/services.py`,
  `apps/backend/engram/context/services.py` — route gateway creation through the
  factory.
- `apps/backend/engram/model_policy/real_provider_tests.py` — factory selection
  + adapter HTTP behavior with a mocked opener (no real network in CI).
- Spec, verification matrix entry, security note.

This slice defers:

- streaming, function-calling, vision, and non-OpenAI-shaped providers;
- automatic secret rotation wiring (manual rotation already exists);
- a committed e2e fixture using a real key (the e2e is run locally with the key
  injected via env into the encrypted envelope; nothing is committed).

## Verification

- Unit: factory returns fake by default and real under `ENGRAM_PROVIDER_MODE=real`;
  adapter constructs correct OpenAI-compatible requests and parses responses via
  a mocked `urllib.request.urlopen`; redacts the prompt; records the call;
  reuses by `request_id`; raises on auth/HTTP errors.
- Existing deterministic suite stays green (fake default unchanged).
- Local-only e2e (not committed): set `ENGRAM_PROVIDER_MODE=real`, create a
  `ProviderSecret` envelope from an env-supplied key, run the golden path, confirm
  a provider-generated memory lands and is retrievable. Recorded in the
  verification matrix with the key redacted.

## Self-Review

- LLM-agnostic by construction: one adapter, many endpoints via `base_url`.
- Fake default keeps the offline deterministic suite; real mode is opt-in.
- The key never leaves the encrypted envelope -> adapter -> bearer header path;
  no key in code, env files, logs, or commits.
- No model change, no migration; `base_url` rides on existing `metadata` JSONField.
