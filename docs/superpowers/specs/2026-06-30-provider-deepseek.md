# Feature — DeepSeek as a model provider

DeepSeek exposes an OpenAI-compatible chat API, so it slots into the existing
`OpenAICompatibleGateway` path. App: ONLY `engram/model_policy`. No DB migration (Provider is a
`TextChoices`; adding a value does not change the column).

## Changes
1. `engram/model_policy/models.py` — add to `Provider(TextChoices)`:
   `DEEPSEEK = 'deepseek', 'DeepSeek'`.
2. `engram/model_policy/services.py` — `default_base_url(provider)`: add a branch
   `if provider == 'deepseek': return 'https://api.deepseek.com/v1'` (OpenAI-compatible; the gateway
   appends `/chat/completions` and `/embeddings`). Keep the existing openai default.
   - `get_provider_gateway` already routes everything that is NOT `Provider.ANTHROPIC` to
     `OpenAICompatibleGateway`, so DeepSeek needs no gateway-selection change — just confirm a deepseek
     policy returns an `OpenAICompatibleGateway` whose `_base_url` is the deepseek base (or the policy's
     `metadata.base_url` override if set).
3. `engram/model_policy/serializers.py` — add `'deepseek'` to the `provider` `ChoiceField` choices in
   `ProviderSecretCreateSerializer`, `ModelPolicyCreateSerializer`, `ModelPolicyUpdateSerializer`
   (currently `('anthropic', 'openai')` → `('anthropic', 'openai', 'deepseek')`).

## Notes / non-goals
- DeepSeek has **no embeddings** endpoint (like Anthropic). A deepseek policy for `task_type=embedding`
  will fail at call time with a provider error — same behaviour as Anthropic today; do NOT add
  create-time validation here (kept consistent; the model-presets work will steer operators to pair
  DeepSeek with an embedding provider). Latest chat models: `deepseek-chat` (V3) and `deepseek-reasoner`
  (R1) — these are operator-entered `model` strings, not enumerated in the backend.
- GLM-style `base_url` override via `policy.metadata['base_url']` still works for DeepSeek too.

## TDD — write FIRST (extend `engram/model_policy/model_policy_tests.py` and/or
`real_provider_tests.py`; reuse their fixtures for org/project/secret/policy + the gateway/base_url
helpers):
- `default_base_url('deepseek') == 'https://api.deepseek.com/v1'`.
- A ProviderSecret + ModelPolicy with `provider='deepseek'` can be created via the API (serializer
  accepts `deepseek`).
- `get_provider_gateway(policy)` for a deepseek policy returns an `OpenAICompatibleGateway` whose
  `_base_url` is `https://api.deepseek.com/v1` (set `ENGRAM_PROVIDER_MODE=real` for that path, mirror
  the existing real-gateway test; with an active secret + envelope). If a real-mode test is awkward,
  at minimum assert default_base_url + serializer acceptance + that the gateway-selection branch picks
  OpenAICompatibleGateway for a non-anthropic provider.
- A deepseek policy with `metadata={'base_url': 'https://custom...'}` → gateway uses the override.

## Verification (container `engram-be`, forced sqlite):
`docker exec -e ENGRAM_DATABASE_URL=sqlite:///:memory: engram-be bash -lc 'cd /srv/app &&
python -m pytest -p no:cacheprovider -q engram/model_policy && ruff check engram/model_policy &&
ruff format --check engram/model_policy && python manage.py makemigrations --check --dry-run'`
makemigrations MUST be clean (no migration — only a TextChoices value + choices tuples).
