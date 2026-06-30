# Parity slice 2 — inspection / observations triage hardening

Closes view-completeness gap **PR F**: pagination + filters on the inspection lists and the
observations list, plus two missing detail (retrieve-by-id) endpoints. Apps touched: ONLY
`engram/inspection` and `engram/observations`. RBAC is UNCHANGED here (memory/context inspection
stay `memories:admin`, audit stays `audit:read`, observations stay `observations:read`) — the
`:read` re-tiering is a separate slice. No model fields, no migration expected.

## Behavior to add

### Inspection lists (session+bearer via the existing `_inspection_scope`)
1. Pagination on all three list views (`MemoryInspectionListView`, `ContextBundleInspectionListView`,
   `AuditEventInspectionListView`): add `limit` (1..200, default 50) + `offset` (≥0, default 0) to
   `InspectionQuerySerializer`. Thread them through `_inspection_scope`/the `List*` service `.execute`
   and slice the ORDERED queryset. Keep the `{count, items}` envelope where `count` is the TOTAL
   match count (pre-slice) and `items` is the page. Read `inspection/services.py` +
   `InspectionBaseView._inspection_scope` first to wire it without breaking existing callers
   (omitting limit/offset → first 50, count semantics unchanged for existing tests → if an existing
   test asserts count==len(items) on small data it still holds).
2. Filters (all optional, ignored when absent):
   - memories: `status` (Memory status value), `kind` (matches `metadata__kind`).
   - audit-events: `event_type`, `correlation_id` (→ filter `request_id`), `since`/`until`
     (ISO-8601 → `created_at__gte` / `created_at__lt`).
   - context-bundles: `since`/`until` only.
   Add these optional fields to `InspectionQuerySerializer`; each view applies only the ones relevant
   to it. Filtering happens in the `List*` services on the already org/project/team-scoped queryset.

### New detail endpoints
3. `GET /v1/inspection/audit-events/<uuid:audit_event_id>` → `AuditEventInspectionDetailView`
   (mirror `ContextBundleInspectionDetailView`): `required_capability='audit:read'`,
   `ListInspectionAuditEvents().detail(inspection_scope, audit_event_id)` (add a `.detail()` to the
   service raising `InspectionNotFoundError('audit_event_not_found', ...)` when out of org/project
   scope). Response = `audit_event_response(ae, actor_name_map=..., target_name_map=...)` built from
   single-event name maps (reuse `_batch_resolve_actor_names`/`_batch_resolve_target_names` on a
   one-element list). Add route to `inspection/urls.py`.
4. `GET /v1/observations/<uuid:observation_id>` → `ObservationDetailView` (bearer-only APIView, mirror
   `ObservationListView`): add a `GetObservation` service + `ObservationDetailInput` mirroring
   `ListObservations`/`ObservationListInput` (`required_capability='observations:read'`, scope by
   org+project+team, `get` by id, redact via the existing `_observation_response`, raise
   `AccessDeniedError`-style not-found → 404 when out of scope). Add route to `observations/urls.py`
   BEFORE the `''` list route is fine (uuid path is distinct). Keep the observations response shape
   (single observation dict + `request_id`).

### Observations list filters
5. Extend `ObservationListQuerySerializer` + `ListObservations`: add `offset` (≥0, default 0),
   `observation_type` (optional), `session_id` (optional UUID), `since`/`until` (optional ISO). Apply
   on the org/project/team-scoped queryset; keep `limit` max 100. Existing `{items, warnings}` shape
   stays; slicing is `[offset:offset+limit]`.

## Constraints / style
- Single quotes; no docstrings/comments unless non-obvious; blank line after return/raise; absolute
  imports; built-in generics; private by default; trivial constructors.
- Detail endpoints MUST enforce the SAME capability + tenant scope as their list (no new read surface
  that bypasses scope). This is the security check for this slice.
- Redaction: reuse the existing `redact_value` / `_observation_response` / `audit_event_response`
  paths; do not emit unredacted bodies.

## TDD — write FIRST in `inspection/inspection_api_tests.py` (or the existing inspection test module)
and `observations/observations_api_tests.py`; use existing fixtures/auth helpers there.
- pagination: N memories, `limit=2&offset=0` → count=N, len(items)=2; `offset=2` → next page.
- memory filter: `status`/`kind` narrows to matching rows only.
- audit filter: `event_type` + `correlation_id` + `since/until` each narrow correctly.
- audit detail: GET existing id → 200 with actor_display/target_display; cross-org/project id → 404
  `audit_event_not_found`; missing capability → 403.
- observation detail: GET existing id → 200 redacted; cross-project/org id → 404; missing capability/
  bad key → 403.
- observation list filters: `offset` + `observation_type`/`session_id` narrow correctly.

## Verification (record exit codes) — container `engram-os` live-mounts this worktree at /srv/app:
`docker exec engram-os bash -lc 'cd /srv/app && python -m pytest -p no:cacheprovider -q
engram/inspection engram/observations && ruff check engram/inspection engram/observations
&& ruff format --check engram/inspection engram/observations
&& python manage.py makemigrations --check --dry-run'`
If `makemigrations --check` reports changes, STOP — no model change is expected.
