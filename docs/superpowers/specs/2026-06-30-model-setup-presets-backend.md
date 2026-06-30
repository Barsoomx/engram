# Feature 2a — model-setup backend (status + presets + one-click apply)

Operator onboarding for model configuration. App: `engram/console` (session-auth console surface;
the apply view composes the existing `engram/model_policy` services). Stacked on the DeepSeek+base_url
branch (needs `ModelPolicyInput.base_url`). No model change / no migration.

## New files
- `engram/console/model_presets.py` — the static `PRESETS` constant.
- `engram/console/views/model_setup.py` — 3 views.
- `engram/console/serializers/model_setup.py` — request serializers.
- `engram/console/views/model_setup_tests.py` — tests.
- register routes in `engram/console/urls.py`.

## PRESETS constant (editable defaults; model strings are starting points operators can edit)
Each preset: `{key, name, description, providers_needed: [...], task_models: [{task_type, provider, model, base_url}]}`.
The 6 task_types are generation, embedding, curation, digest, rerank, admin_assistant. embedding is
ALWAYS OpenAI (Anthropic/DeepSeek/GLM have no embeddings).
- `anthropic_openai` "Anthropic + OpenAI embeddings", providers [anthropic, openai]:
  generation/admin_assistant=anthropic `claude-3-5-sonnet-latest`; curation/digest/rerank=anthropic
  `claude-3-5-haiku-latest`; embedding=openai `text-embedding-3-small`.
- `openai_all` "OpenAI (all tasks)", providers [openai]:
  generation/admin_assistant=`gpt-4o`; curation/digest/rerank=`gpt-4o-mini`; embedding=`text-embedding-3-small`.
- `deepseek_openai` "DeepSeek + OpenAI embeddings", providers [deepseek, openai]:
  generation/admin_assistant/curation/digest=deepseek `deepseek-chat`; rerank=deepseek `deepseek-reasoner`;
  embedding=openai `text-embedding-3-small`.
- `glm_openai` "GLM + OpenAI embeddings", providers [openai]:
  generation/admin_assistant=openai+base_url `https://open.bigmodel.cn/api/paas/v4` model `glm-4-plus`;
  curation/digest/rerank=same base_url model `glm-4-flash`; embedding=openai (default base) `text-embedding-3-small`.
  (GLM uses provider=openai + base_url; its key is a separate secret from the OpenAI-embeddings key —
  so providers_needed for the operator is logically ["glm", "openai"]; represent GLM's key under a
  `key_slot` so the apply view knows GLM and OpenAI-embeddings are DIFFERENT secrets even though both
  are provider=openai. Add a `key_slot` field per task_model = the provider_keys map key, e.g. 'glm'
  vs 'openai'. providers_needed lists key_slots: glm_openai → ['glm', 'openai'].)

## Endpoints (all under /v1/admin/, IsAuthenticated + ActiveOrganizationPermission)
1. `GET /v1/admin/model-setup/status?project_id=&team_id=` — `ModelSetupStatusView`, cap
   `model_policy:read`. For each of the 6 task_types report `{task_type, configured, policy_id,
   provider, model, secret_active}` where configured = an active ModelPolicy exists in scope whose
   secret is active (query `ModelPolicy.objects.filter(organization, project/team scope, task_type,
   active=True, secret__active=True)`). Also return `ready` (all 6 configured) and `secrets`
   (`[{id,name,provider,active}]` in scope, fingerprint/metadata only — never raw).
2. `GET /v1/admin/model-setup/presets` — `ModelPresetsView`, cap `model_policy:read`. Returns
   `{presets: PRESETS}` (no secrets, pure templates).
3. `POST /v1/admin/model-setup/apply` — `ApplyPresetView`. Cap: require BOTH `model_policy:*` AND
   `secrets:*` (it creates secrets + policies). Use `RequireCapability('model_policy:*')` as the DRF
   permission AND verify `secrets:*` in the handler via `request.effective_scope.capabilities`
   (wildcard-aware) → 403 `missing_capability` if absent.
   Body: `{project_id, team_id?, scope ('organization'|'project'|'team'), preset_key,
   provider_keys: {<key_slot>: raw_key}, request_id}`.
   Logic in ONE `transaction.atomic()`:
   - Validate preset_key (404 `preset_not_found` else); validate every key_slot in
     `providers_needed` is present in provider_keys (400 `missing_provider_key` else).
   - For each key_slot → create a `ProviderSecret` via `CreateProviderSecret` (provider = the actual
     provider of that slot — glm slot → 'openai'; name = f'{preset_key}:{key_slot}'); map key_slot→secret.
   - For each task_model: DISABLE any existing active policy for that task_type in scope (query +
     `DisableModelPolicy`), then create a new `ModelPolicy` via `CreateModelPolicy` with provider,
     model, base_url, secret_id (the secret for that task's key_slot), task_type, scope.
   - Return `{created_secret_ids, created_policy_ids, status: <same shape as the status endpoint>}`.
   Raw keys: encrypted by CreateProviderSecret (Fernet); audit metadata redacts raw_secret already;
   the view MUST NOT log provider_keys.

## Security
- status/presets are reads (model_policy:read). apply requires model_policy:* + secrets:* (admin).
- Tenant scope: all queries + the service calls are scoped to `request.active_organization` +
  project/team; the model_policy services already enforce org/team scope + audit.
- Atomicity: a failure in any secret/policy create rolls the whole apply back.

## TDD — write FIRST in model_setup_tests.py (real DRF client + session admin, per repo rule for view
tests; reuse console test fixtures for org/project + a session admin with model_policy:* + secrets:*).
- status: no policies → all `configured=false`, `ready=false`; after creating a policy for one
  task_type → that one `configured=true`.
- presets: returns the 4 presets with the 6 task_types each; embedding always openai.
- apply (deepseek_openai): with provider_keys {deepseek, openai} → creates 2 secrets + 6 policies;
  status afterwards `ready=true`; each task_type resolves to the preset's provider/model.
- apply (glm_openai): glm task policies have provider=openai + metadata.base_url = the GLM endpoint;
  glm and openai-embeddings are SEPARATE secrets.
- apply re-run replaces (disables prior active policy per task_type — no duplicate active policy).
- apply missing a required provider key → 400 missing_provider_key, nothing created (atomic rollback).
- apply without model_policy:* or without secrets:* → 403.
- apply unknown preset_key → 404.
- tenant isolation: status/apply only touch the active org.

## Verification (container `engram-be`, forced sqlite):
`docker exec -e ENGRAM_DATABASE_URL=sqlite:///:memory: engram-be bash -lc 'cd /srv/app &&
python -m pytest -p no:cacheprovider -q engram/console engram/model_policy && ruff check engram/console
&& ruff format --check engram/console && python manage.py makemigrations --check --dry-run'`
makemigrations MUST be clean (no model change).
