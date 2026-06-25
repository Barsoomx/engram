# Memory Feedback Loop Security Review

Date: 2026-06-25

Branch: `feat/memory-feedback-loop`

Implementation review SHA: `824d532bfc627d3209a5f586e63cbe738bdb5103`

Result: SECURITY APPROVED for the backend stale/refuted feedback checkpoint.

## Scope Reviewed

- `POST /v1/memories/{memory_id}/feedback`
- `memories:review` capability enforcement
- cross-project and team-scope denial
- `Memory` and `RetrievalDocument` flag consistency
- audit metadata redaction
- context retrieval exclusion after stale/refuted feedback

The focused review covered the backend implementation and final test coverage
through `824d532bfc627d3209a5f586e63cbe738bdb5103`: memory feedback
serializers, service, view, URL routing, focused feedback tests, and adjacent
access/context behavior needed to validate authorization and retrieval
exclusion.

## Commands And Tools Run

| Check | Result |
| --- | --- |
| focused code/security readback | Manual review of `apps/backend/engram/memory/serializers.py`, `services.py`, `views.py`, `urls.py`, `memory_feedback_tests.py`, `settings/urls.py`, plus adjacent `engram/context/services.py` and `engram/access/services.py`. |
| focused memory feedback tests | Exit 0. `docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && pytest engram/memory/memory_feedback_tests.py -v"` reported 9 passed after final-review coverage for cross-team denial and oversized metadata fields. |
| adjacent context/access tests | Exit 0. `docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && pytest engram/context/context_api_tests.py engram/access/access_scope_tests.py -v"` reported 37 passed. |
| full backend tests | Exit 0. `docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && pytest -v"` reported 132 passed. |
| lint/format | Exit 0. `docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && ruff check . && ruff format --check ."` reported `All checks passed!` and `68 files already formatted`. |
| migration freshness | Exit 0. `docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "python manage.py migrate --noinput && python manage.py makemigrations --check --dry-run"` applied migrations and reported `No changes detected`. |
| Compose golden path | Exit 0. `python3 scripts/e2e_golden_path.py` completed hook observation, memory candidate promotion, and future session context retrieval. |
| repository layout | Exit 0. `python3 scripts/repository_layout.py` produced no findings. |
| repository text quality | Exit 0. `python3 scripts/repository_quality.py` produced no findings. |
| whitespace | Exit 0. `git diff --check HEAD` produced no findings. |

## Findings By Severity

### CRITICAL

None.

### IMPORTANT

None.

### MINOR

None.

## Fixes Applied

Final review requested additional endpoint proof for cross-team denial on
team-visible memory and oversized `request_id`/`correlation_id` rejection before
mutation. Those tests were added in `test: cover memory feedback denials`; no
production-code change was required.

## Accepted Risk

None for the reviewed backend checkpoint. This approval does not cover
frontend/admin review UI, MCP `memory.feedback`, daily curator jobs,
provider/model-policy calls, semantic/vector retrieval, or broader memory
quality workflows.
