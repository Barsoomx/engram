# Parity slice 5 — console minor (gap J)

Closes the remaining LOW item of gap **J**: `MemoryReviewViewSet` has list + actions but no
GET-by-id. Add `retrieve`. App: ONLY `engram/console`. No model change / no migration.

Org create/close (the other half of gap J) is intentionally NOT implemented here: org creation is a
SaaS-signup concern (roadmap Layer 4 / onboarding), not a console-admin parity action. Document that
decision in the spec/PR and leave `OrganizationViewSet` as List/Retrieve/Update.

## Behavior
`GET /v1/admin/memory-review/<uuid:pk>/` → `MemoryReviewViewSet.retrieve`.
- Permission: same as list — `[IsAuthenticated, ActiveOrganizationPermission, RequireCapability('memories:review')]`.
- Resolve `pk` within `request.active_organization` as a reviewable item: try the existing
  `get_review_candidate_or_404(organization, pk)`; if that raises not-found, try
  `get_review_memory_or_404(organization, pk)`; if neither matches → 404 (mirror the queue's
  error shape, e.g. `{'code': 'review_item_not_found', 'detail': ...}` or the helper's own
  `MemoryReviewError` status — match what the action path returns). Return `queue_item_payload(item)`
  (it already handles both `MemoryCandidate` and `Memory`).
- Org isolation: a candidate/memory in another org → 404 (the helpers are org-scoped — confirm).

Add `retrieve` as a method on the viewset (the @action detail routes already exist, so the detail URL
is registered; defining `retrieve` makes the router map GET /<pk>/ to it). Do NOT add
RetrieveModelMixin's `get_object()` path — resolution is the custom candidate-or-memory union.

## Style
Single quotes; no docstrings/comments unless non-obvious; blank line after return/raise; absolute
imports; built-in generics; private by default.

## TDD — write FIRST in `engram/console/views/memory_review_tests.py` (reuse existing fixtures: how a
candidate / a conflict-or-refuted memory + a session admin client with `memories:review` are built).
- retrieve a queue CANDIDATE by id → 200, payload `type == 'candidate'`, `id` matches.
- retrieve a queue MEMORY (conflict/refuted) by id → 200, payload `type == 'memory'`.
- unknown id → 404.
- a candidate/memory in ANOTHER org → 404 (tenant isolation).
- caller without `memories:review` → 403.

## Verification (container `engram-os`, forced sqlite):
`docker exec -e ENGRAM_DATABASE_URL=sqlite:///:memory: engram-os bash -lc 'cd /srv/app &&
python -m pytest -p no:cacheprovider -q engram/console && ruff check engram/console &&
ruff format --check engram/console && python manage.py makemigrations --check --dry-run'`
makemigrations MUST be clean (no model change).
