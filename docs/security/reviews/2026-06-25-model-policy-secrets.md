# Model Policy Secrets Security Review

Date: 2026-06-25

Branch: `feat/model-policy-secrets`

Result: PASS after fix verification.

## Scope Reviewed

- `apps/backend/engram/model_policy/**`
- `apps/backend/settings/settings.py`
- `apps/backend/settings/urls.py`
- `apps/backend/pyproject.toml`
- `apps/backend/poetry.lock`
- `scripts/repository_layout.py`
- `tests/repository/test_backend_runtime_contract.py`

This focused review covers provider secret storage, model policy resolution,
fake provider adapter selection, and provider call audit records. It does not
add real Anthropic/OpenAI network calls, semantic retrieval, frontend/admin UI,
MCP tools, or AI workflow jobs.

## Commands And Tools Run

| Check | Result |
| --- | --- |
| Initial RED focused test | Exit 2. `cd apps/backend && poetry run pytest engram/model_policy/model_policy_tests.py -v` failed with `ModuleNotFoundError: No module named 'engram.model_policy.models'`. |
| Cross-team secret RED regression | Exit 1. Focused test failed because detail returned `200` for another team's provider secret. |
| Team-bound project policy RED regression | Exit 1. Focused resolver test failed because another team received a project policy bound to the first team's secret. |
| Focused model-policy tests | Exit 0. `cd apps/backend && poetry run pytest engram/model_policy/model_policy_tests.py -v` reported 7 passed. |
| Focused model-policy lint/format | Exit 0. `cd apps/backend && poetry run ruff check engram/model_policy && poetry run ruff format --check engram/model_policy` reported all checks passed and 10 files already formatted. |
| Adjacent access/context/memory/inspection tests | Exit 0. `cd apps/backend && poetry run pytest engram/model_policy/model_policy_tests.py engram/access/access_scope_tests.py engram/context/context_api_tests.py engram/memory/memory_feedback_tests.py engram/inspection/inspection_api_tests.py -v` reported 57 passed. |
| Migration drift | Exit 0. `cd apps/backend && DJANGO_SETTINGS_MODULE=settings.test_settings poetry run python manage.py makemigrations --check --dry-run` reported no changes detected. |
| Repository runtime contract | Exit 0. `python3 -m unittest tests.repository.test_backend_runtime_contract -v` reported 15 tests OK. |
| Poetry lock check | Exit 0. `cd apps/backend && poetry check --lock` reported all set. |
| Repository layout | Exit 0. `python3 scripts/repository_layout.py` produced no output. |
| Independent read-only security review agent | Initial verdict `CHANGES_REQUIRED`: team-scoped `secrets:*` could mutate org-scoped secrets; resolver could return disabled-secret policies; production key handling was under-specified. Fix verification verdict `PASS`. |
| Karpathy simplicity/scope re-review agent | Initial verdict `CHANGES_REQUIRED`: org-scope secret create with `team_id` was accepted; fake provider selection was under-proven; docs were stale. Fix verification resolved code findings; stale docs were updated in this checkpoint. |
| Final Compose backend gate | Exit 0. `docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && python manage.py migrate --noinput && python manage.py check && pytest -v && ruff check . && ruff format --check ."` reported no pending migrations, system check clean, 166 passed, all Ruff checks passed, and 111 files already formatted. |

## Findings By Severity

### CRITICAL

None found.

### IMPORTANT

- RESOLVED: Team-scoped `secrets:*` keys could create, rotate, or disable
  organization-scoped provider secrets. Fixed by carrying effective
  `allowed_team_ids` into create/rotate/disable service inputs and rejecting
  org-scoped secret mutation from team-scoped requests.

### MINOR

- RESOLVED: `ResolveModelPolicy` could return active policies backed by
  disabled provider secrets. Fixed with `secret__active=True` in resolver
  candidates and a regression test expecting no resolved policy after disable.
- RESOLVED: Provider-secret envelope key handling fell back to Django
  `SECRET_KEY` outside dev/test. Fixed by explicit
  `ENGRAM_SECRET_ENCRYPTION_KEY` setting and fail-closed production behavior.

## Required Security Properties

- Provider secret plaintext is encrypted into `ProviderSecretEnvelope` rows and
  is never returned through API responses.
- Provider secret create/rotate audit metadata is redacted before persistence.
- Provider secret detail, rotate, and disable operations cannot cross the
  resolved team scope.
- Project model policies with a team binding apply only to that team; other
  teams fall back to their own team policy or the organization policy.
- Disabled provider secrets cannot be used by provider calls.
- Disabled provider secrets are not returned by model policy resolution.
- Provider call records store provider/model/policy metadata, token usage,
  latency, cost metadata, and redaction state without prompt bodies or raw
  secrets.
- `cryptography` is a runtime dependency and the lock file is current.

## Accepted Risk

- Real Anthropic/OpenAI network calls, vault-backed secret storage, semantic
  retrieval, frontend/admin UI, MCP tooling, and AI workflow jobs remain outside
  this checkpoint.
- Policy metadata fields for fallback/retention/provider allowlists are schema
  placeholders for later routing controls and are not active decision logic in
  this slice.
