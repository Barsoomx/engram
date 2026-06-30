# Parity slice 3 — Console Audit Log view

Closes view-completeness gap **PR E**. The `audit:read` capability is granted to owner/admin/auditor
but is NOT called by any console (session) view — the only reader is the API-key inspection surface,
which the session frontend doesn't use. So the **auditor role has no page**. Add a session-auth,
tenant-scoped console Audit Log. App touched: ONLY `engram/console`. No model change / no migration.

## Behavior
New `AuditEventViewSet` (List + Retrieve) registered at `audit-events` on the console router
(`/v1/admin/audit-events`, `/v1/admin/audit-events/<uuid>`).
- Permissions: `[IsAuthenticated, ActiveOrganizationPermission, RequireCapability('audit:read')]`
  (mirror `WorkflowRunViewSet.get_permissions`).
- Queryset: `AuditEvent.objects.filter(organization=self.request.active_organization)
  .select_related('project', 'team').order_by('-created_at')`. NEVER widen beyond active_organization
  (tenant isolation). Retrieve of an out-of-org id → 404 (naturally, since queryset is org-scoped).
- List filters (optional query params, mirror WorkflowRunViewSet's style): `event_type`, `result`,
  `actor_id`, `target_type`, `project_id`, `team_id`, `created_at__gte`, `created_at__lt`.
- Pagination: rely on the console default DRF pagination (same as other console list views — do NOT
  hand-roll a {count,items} envelope; console uses the DRF paginator).
- Response includes the AuditEvent fields (id, event_type, actor_type, actor_id, target_type,
  target_id, capability, result, request_id, metadata, project_id, team_id, created_at) PLUS
  `actor_display` and `target_display` resolved names.

## Name resolution (actor_display / target_display)
Mirror the inspection batch-resolution pattern (read `engram/inspection/views.py`
`_batch_resolve_actor_names` / `_batch_resolve_target_names` / `audit_event_response` as REFERENCE)
but implement console-native — do NOT import inspection's private functions across apps. Resolution
must be N+1-bounded: for the list page, batch-resolve once (one `id__in` query per actor type and per
target type), not per row. For retrieve, resolve the single event. ApiKey actor names via
`select_related('owner_identity')`; identity/memory/project/team targets by id maps. Redact metadata
with the existing `redact_value` (metadata can carry arbitrary fields).

## Files
- `engram/console/views/audit_log.py` — `AuditEventViewSet` + the batch-resolution helpers.
- `engram/console/serializers/audit_log.py` — `AuditEventSerializer` reading `actor_display` /
  `target_display` from serializer context maps.
- `engram/console/urls.py` — `router.register('audit-events', AuditEventViewSet, basename='admin-audit-event')`.
- `engram/console/views/audit_log_tests.py` — tests.

## Style
Single quotes; no docstrings/comments unless non-obvious; blank line after return/raise; absolute
imports; built-in generics; private by default; trivial constructors. Match the existing console
viewset conventions (`WorkflowRunViewSet`, `MemberViewSet`).

## TDD — write FIRST in `engram/console/views/audit_log_tests.py`. For VIEW tests use mocks/real HTTP
with session Token auth (look at existing console view tests, e.g. workflow_runs_tests.py, for the
session-admin auth fixture + ActiveOrganization header pattern). Per repo rule, view/API tests use
the real DRF client (not stubs).
- auditor (role with `audit:read`) lists org audit events → 200, only this org's events present.
- retrieve an event by id → 200 with actor_display/target_display populated.
- tenant isolation: an event in ANOTHER org is absent from the list AND retrieve → 404.
- filters: `event_type` / `result` / `actor_id` / date range each narrow correctly.
- missing `audit:read` (e.g. a developer) → 403.
- pagination: more than one page of events paginates via the console paginator.
- N+1 bound: assert the list endpoint issues a bounded number of queries (use
  `django.test.utils.CaptureQueriesContext` or `assertNumQueries`-style) that does NOT grow with the
  number of events (e.g. create 5 events, capture queries, then 15 events, assert query count is the
  same/bounded).

## Verification (container `engram-rem`, forced sqlite after the pgvector harness change):
`docker exec -e ENGRAM_DATABASE_URL=sqlite:///:memory: engram-rem bash -lc 'cd /srv/app &&
python -m pytest -p no:cacheprovider -q engram/console && ruff check engram/console &&
ruff format --check engram/console && python manage.py makemigrations --check --dry-run'`
If makemigrations reports changes, STOP — no model change is expected.
