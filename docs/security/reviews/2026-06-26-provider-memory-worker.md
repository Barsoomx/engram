# Provider Memory Worker Security Review

Date: 2026-06-26

Branch: `feat/provider-memory-worker`

Result: PASS after independent security and Karpathy re-review.

## Scope Reviewed

- `apps/backend/engram/memory/services.py`
- `apps/backend/engram/memory/memory_worker_tests.py`
- `apps/backend/engram/model_policy/services.py`
- `apps/backend/engram/model_policy/model_policy_tests.py`
- `apps/backend/engram/core/management/commands/engram_bootstrap_golden_path.py`
- `apps/backend/engram/core/golden_path_tests.py`
- `apps/backend/engram/celeryconfig.py`
- `apps/backend/engram/core/celery_foundation_tests.py`
- `scripts/e2e_golden_path.py`
- `docs/superpowers/specs/2026-06-26-provider-memory-worker-design.md`
- `docs/superpowers/plans/2026-06-26-provider-memory-worker.md`

This focused review covers provider-backed memory proposal generation through
the existing memory worker. It does not cover real provider network calls,
embeddings, semantic retrieval, daily digest scheduling, frontend/admin UI, MCP
tools, or new Celery topology. It also records the reference-backend Celery
parity fix for Redis Sentinel result backend configuration while preserving
Engram queue names and the package outbox transport.

## Commands And Tools Run

| Check | Result |
| --- | --- |
| TDD RED focused tests | Exit 1 before implementation. Initial RED showed missing provider calls/provenance and missing policy fallback. Fix RED showed 3 failures for xoxb redaction and existing-candidate policy bypass. Karpathy-fix RED showed 4 failures for local candidate text, missing generated fields, and missing existing-candidate provenance update. |
| Focused memory/model-policy/Celery/golden-path tests | Exit 0. `cd apps/backend && poetry run pytest engram/memory/memory_worker_tests.py engram/model_policy/model_policy_tests.py engram/core/golden_path_tests.py engram/core/celery_foundation_tests.py -v` reported 41 passed. |
| Full backend tests | Exit 0. `cd apps/backend && poetry run pytest -v` reported 172 passed. |
| Lint and format | Exit 0. Focused Ruff check passed and focused Ruff format check reported 8 files already formatted. |
| Migration drift | Exit 0. `cd apps/backend && DJANGO_SETTINGS_MODULE=settings.test_settings poetry run python manage.py makemigrations --check --dry-run` reported no changes detected. |
| Repository checks | Exit 0. `python3 -m unittest discover -s tests -v` reported 31 tests OK; `python3 scripts/repository_layout.py` and `python3 scripts/repository_quality.py` exited 0. |
| Compose golden path E2E | Exit 1 before script update because the E2E still searched memory by old observation title after provider-generated titles landed. Exit 0 after update; real Compose services accepted a hook observation, worker created retrieval document, and future session context included the generated memory. |
| Independent read-only security review agent | PASS. Initial review found no blocking findings. Re-review after generated provider output also found no Critical, Important, or Minor findings. |
| Karpathy simplicity/scope review agent | Initial `CHANGES_REQUIRED`; re-review `PASS_CODE`. Fixed provider-generated title/body consumption and existing-candidate provenance update. |
| Final Compose backend gate | Exit 0. `docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && python manage.py migrate --noinput && python manage.py check && pytest -v && ruff check . && ruff format --check ."` applied migrations, passed system checks, reported 172 passed, Ruff clean, and 111 files already formatted. |

## Findings By Severity

### CRITICAL

None.

### IMPORTANT

None after fixes.

Resolved findings:

- Provider-backed generation now consumes deterministic fake provider output
  through `ProviderCallResult.generated_title` and `generated_body`; the worker
  no longer builds candidate title/body from local observation text after the
  provider call.
- Existing candidate reuse now resolves policy and calls the fake provider
  before promotion; candidates missing provider provenance are updated with the
  generated title/body and provider evidence before memory promotion.
- Token-shaped provider/tool output is redacted through the shared core
  redactor before provider prompt construction and before candidate, memory,
  retrieval document, and provider call persistence.

### MINOR

Residual accepted hardening note: `ProviderCallRecord` uses lookup-then-create
idempotency for `(organization, project, task_type, request_id)` without a
database uniqueness constraint. This is accepted for the fake/local provider
path because the memory worker locks the observation row before provider
generation and the fake gateway has no external side effect. Add a database
constraint before real provider network side effects use this gateway contract.

## Required Security Properties

- Celery task payloads contain stable ids only.
- Provider policy resolution uses the observation organization, project, and
  team scope.
- Missing or disabled generation policy fails before candidate, memory,
  retrieval document, or provider call writes.
- Duplicate worker delivery does not duplicate provider call records.
- Provider secrets, API keys, prompt bodies, and token-shaped tool output are
  not stored in candidate evidence, memory metadata/body, provider call records,
  audit metadata, logs, or task payloads.
- Existing exact retrieval and context bundle authorization behavior remains
  unchanged.

## Accepted Risk

No blocking accepted risk for this checkpoint. The gateway idempotency
hardening note above is deferred until the real provider adapter slice.
