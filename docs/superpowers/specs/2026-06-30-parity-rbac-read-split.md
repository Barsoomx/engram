# Parity slice 4 — RBAC :read split (gaps C + G)

Least-privilege read tier for the model-policy/secret inventory and the memory/context inspection
reads. Today every reader requires the broad `:*` (or `memories:admin`), so a read-only role can't
populate dashboards. Add granular read caps, grant them to auditor + developer (owner/admin keep
everything), and re-gate ONLY the GET readers. Apps: `engram/access` (migration), `engram/model_policy`,
`engram/inspection`. Security-sensitive — mutating endpoints MUST stay on `:*`.

## 1) Migration `engram/access/migrations/0005_seed_read_capabilities.py`
Mirror `0003_admin_capabilities.py` (RunPython, idempotent get_or_create of `Capability`, then
`RoleCapability` links by role code). Reverse = no-op (or remove links) like 0003.
- New capabilities: `secrets:read` ('Read provider secret inventory.'), `model_policy:read`
  ('Read model policies.'), `context:read` ('Read context bundles.').
- Grant ALL THREE to roles: `organization_owner`, `organization_admin`, `auditor`, `developer`.
  (owner/admin already satisfy secrets/model_policy read via their `:*` wildcard, but
  **`context:read` has NO wildcard** — without an explicit grant the re-gated context inspection would
  lock owner/admin OUT. Granting all three to all four roles is the safe, explicit choice.)
- Depends on the latest access migration (0004_organizationmembership_status).

## 2) Re-gate GET readers — change ONLY these `required_capability` values, nothing else

`engram/model_policy/views.py` (the `required_capability=...` arg inside each handler):
- `ProviderSecretListView.get` → `'secrets:read'`   (was secrets:*)
- `ProviderSecretDetailView.get` → `'secrets:read'`
- `ModelPolicyListView.get` → `'model_policy:read'` (was model_policy:*)
- `ModelPolicyResolveView.get` → `'model_policy:read'`
- `ModelPolicyDetailView.get` → `'model_policy:read'`
- UNCHANGED — stay `:*`: `ProviderSecretListView.post`, `ProviderSecretDetailView.patch`,
  `ProviderSecretRotateView.post`, `ProviderSecretDisableView.post`, `ProviderSecretEnableView.post`,
  `ModelPolicyListView.post`, `ModelPolicyDetailView.patch`, `ModelPolicyDisableView.post`.

`engram/inspection/views.py` (the class-level `required_capability` attr):
- `MemoryInspectionListView` / `MemoryInspectionCountView` / `MemoryInspectionDetailView`:
  `memories:admin` → `'memories:read'`
- `ContextBundleInspectionListView` / `ContextBundleInspectionDetailView`:
  `memories:admin` → `'context:read'`
- `AuditEventInspectionListView` / `AuditEventInspectionDetailView`: UNCHANGED (`audit:read`).

Do NOT touch any service logic, querysets, serializers, or response shapes — capability strings only
(plus the migration). The wildcard check (`required in caps OR f'{prefix}:*' in caps`) means `:*`
holders still pass every re-gated reader, so admins/agents with `:*` keys are unaffected.

## TDD — write FIRST
Extend `engram/model_policy/model_policy_tests.py` and `engram/inspection/inspection_api_tests.py`
(reuse their session/bearer auth + role helpers). Add a migration test in
`engram/access/` if a natural spot exists (else cover via the API tests that rely on seeded grants).
- auditor (now holds `secrets:read`/`model_policy:read`/`context:read`/`memories:read`):
  - CAN GET secrets list+detail, policies list+detail+resolve, memory inspection list+detail+count,
    context-bundle inspection list+detail → 200.
  - CANNOT mutate: POST create / PATCH update / disable / rotate / enable / policy disable → 403
    `missing_capability`.
- developer: same read access (200) and same mutation denial (403).
- admin / `:*` key: reads AND mutations still 200 (no regression) — INCLUDING context inspection
  (regression guard for the context:read grant).
- a role/key with neither the read cap nor `:*` → 403 on the re-gated readers.

## Verification (forced sqlite; container `engram-be`)
`docker exec -e ENGRAM_DATABASE_URL=sqlite:///:memory: engram-be bash -lc 'cd /srv/app &&
python -m pytest -p no:cacheprovider -q engram/access engram/model_policy engram/inspection &&
ruff check engram/access engram/model_policy engram/inspection &&
ruff format --check engram/access engram/model_policy engram/inspection &&
python manage.py makemigrations --check --dry-run'`
`makemigrations --check` MUST be clean (the only new migration is the hand-written 0005; no model
fields change). If it reports a model change, STOP and report.
