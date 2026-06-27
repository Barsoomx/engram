# Admin Inspection API Security Review

Date: 2026-06-25

Branch: `feat/admin-inspection-api`

Result: pass after fix re-review.

## Scope Reviewed

- `apps/backend/engram/inspection/**`
- `apps/backend/settings/settings.py`
- `apps/backend/settings/urls.py`
- `scripts/repository_layout.py`
- `apps/backend/engram/inspection/inspection_api_tests.py`

The focused review covers read-only operational inspection of memories, context
bundles, context bundle items, and audit events.

## Commands And Tools Run

| Check | Result |
| --- | --- |
| TDD red inspection tests | Exit 1 before implementation. All four tests failed with 404 because `/v1/inspection/*` routes did not exist. |
| Security-fix RED tests | Exit 1. `cd apps/backend && poetry run pytest engram/inspection/inspection_api_tests.py -v` failed because regular `memories:read` keys could inspect memory/context responses and audit identifiers were returned raw. |
| Focused inspection tests | Exit 0 after security fixes. `cd apps/backend && poetry run pytest engram/inspection/inspection_api_tests.py -v` reported 4 passed. |
| Adjacent access/context/memory tests | Exit 0 after security fixes. `cd apps/backend && poetry run pytest engram/inspection/inspection_api_tests.py engram/access/access_scope_tests.py engram/context/context_api_tests.py engram/memory/memory_feedback_tests.py -v` reported 50 passed. |
| Host backend manage.py check | Exit 1 before Compose rerun. Failed because default DB host `postgres` is only resolvable inside Compose. |
| Compose migrate and manage.py check | Exit 0. `docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && python manage.py migrate --noinput && python manage.py check"` migrated a fresh Compose database and reported no system check issues. |
| Full backend tests | Exit 0. `docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && pytest -v"` reported 159 passed. |
| Backend lint and format | Exit 0. `docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && ruff check . && ruff format --check ."` reported all checks passed and 101 files already formatted. |
| Repository tests | Exit 0. `python3 -m unittest discover -s tests -v` reported 30 tests OK. |
| Repository layout | Exit 0. `python3 scripts/repository_layout.py` produced no output. |
| Repository text quality | Exit 0. `python3 scripts/repository_quality.py` produced no output. |
| Whitespace | Exit 0. `git diff --check HEAD` produced no output. |
| Independent read-only security review agent | Exit 0 review handoff. Reported no critical findings and three important findings: weak memory/context capability gate, incomplete identifier redaction, and audit listing self-noise. |
| Independent fix re-review agent | Exit 0. Verified the three code-level findings are resolved; docs/status parity updated after review. |

## Findings By Severity

### CRITICAL

None reported.

### IMPORTANT

- Fixed: memory and context-bundle inspection now require `memories:admin`
  instead of regular `memories:read`; regression tests prove a developer-style
  `memories:read` key receives `missing_capability`.
- Fixed: context `request_id` and audit `actor_id`, `target_id`, `request_id`,
  and `correlation_id` are redacted before response serialization.
- Fixed: audit listing excludes inspection-generated `AccessScopeResolved`
  events for `target_type='audit_event'` and `capability='audit:read'`, so
  repeated audit reads do not pollute their own output.

### MINOR

- Fixed: spec and plan now describe `memories:admin` and identifier redaction.

## Required Security Properties

- All inspection endpoints are read-only.
- Every request requires `project_id` before returning records.
- Memory and context-bundle inspection require `memories:admin`.
- Audit inspection requires `audit:read`.
- Project and team filters are resolved through the existing API-key scope
  resolver before records are returned.
- Responses redact metadata, scope evidence, authorization scope,
  content-bearing fields, and client-propagated request/audit identifiers
  through the shared redaction tooling.
- Detail endpoints return not-found for records outside the resolved scope.

## Accepted Risk

The existing access resolver still records normal API-key scope audit events
and updates key usage metadata during inspection authentication. That side
effect is accepted as the current access-control contract; audit inspection
only suppresses those inspection-generated rows from its response output.
