# Parity slice 1 — model_policy admin lifecycle completeness

Closes view-completeness gaps: PR A remainder (policy detail+route, list pagination),
PR B (policy update + disable), PR D (secret enable + rename). One app (`engram/model_policy`),
one theme (admin write symmetry, audited, security-reviewed). RBAC stays on the broad
`model_policy:*` / `secrets:*` capabilities here — the `:read` split is a separate slice.

## Behavior to add

### ModelPolicy
1. `GET /v1/model-policy/policies/<uuid:policy_id>` → `ModelPolicyDetailView.get`, returns
   `model_policy_response`, 404 `model_policy_not_found` when out of scope. Mirror
   `ProviderSecretDetailView` (scope via `scoped_policies`, `get_object_or_404`).
2. `PATCH /v1/model-policy/policies/<uuid:policy_id>` → `ModelPolicyDetailView.patch`, calls a new
   `UpdateModelPolicy` service. Mutable: `name`, `provider`, `model`, `secret_id`, `active`,
   `fallback_enabled`, `task_type`. Bumps `version` by 1 on any change. Re-validates secret scope
   (same org/team) like `CreateModelPolicy`. Emits an audit event (mirror the create audit).
3. `POST /v1/model-policy/policies/<uuid:policy_id>/disable` → `ModelPolicyDisableView.post`,
   calls `DisableModelPolicy` (sets `active=False`, audited). Mirror `ProviderSecretDisableView`.
   Idempotent: disabling an already-inactive policy returns 200 with `active=False`.

### ProviderSecret
4. `POST /v1/model-policy/secrets/<uuid:secret_id>/enable` → `ProviderSecretEnableView.post`,
   calls `EnableProviderSecret` (sets `active=True`, audited). Reverse of `DisableProviderSecret`.
5. `PATCH /v1/model-policy/secrets/<uuid:secret_id>` → `ProviderSecretDetailView.patch`, calls a new
   `UpdateProviderSecret` service. Mutable: `name` only (rename); secret material untouched.
   Audited. Does NOT change fingerprint/version/active.

### Pagination (both lists)
6. Add optional `limit` (int, 1..200, default 50) + `offset` (int, ≥0, default 0) to
   `ProviderSecretQuerySerializer` and `ModelPolicyQuerySerializer`. List views slice the ordered
   queryset and keep the `{count, items}` envelope where `count` is the TOTAL match count (pre-slice)
   and `items` is the page. Existing callers omit limit/offset → first 50.

## Constraints
- New view classes go in `engram/model_policy/views.py` (match existing grouped style).
- New services + their `*Input` dataclasses go in `engram/model_policy/services.py`, mirroring
  `DisableProviderSecret`/`DisableProviderSecretInput` exactly (org+team scope guard, `actor_id`,
  `allowed_team_ids`, `ModelPolicyError` on not-found / scope-denied, AuditEvent emission).
- New serializers (`ModelPolicyUpdateSerializer`, `ProviderSecretUpdateSerializer`,
  `ModelPolicyDisableSerializer` if needed) in `engram/model_policy/serializers.py`.
- Add routes to `engram/model_policy/urls.py`. Place `policies/<uuid:policy_id>` BEFORE `resolve`?
  No — `resolve` is a literal, `<uuid>` won't shadow it; keep literals first to be safe.
- Add `model_policy_not_found` already in `ERROR_STATUS`; add any new codes used.
- Follow repo style: single quotes, no docstrings/comments unless non-obvious, blank line after
  `return`/`raise`, private attrs/methods by default, absolute imports, built-in generics.

## TDD — write these tests FIRST in `engram/model_policy/model_policy_tests.py` (extend existing),
all must fail before implementation, pass after. Use the existing fixtures/factory helpers in that
file for org/project/team/secret/policy/session-or-bearer auth.

- policy detail: owner can GET policy by id → 200 with policy_id; cross-team/cross-org → 404.
- policy update: PATCH name+model → 200, version incremented, fields changed, audit row written.
- policy update secret rescope: PATCH secret_id to a secret in another org/team → 403/400 scope error.
- policy disable: POST disable → active=False; ResolveModelPolicy no longer selects it; second
  disable is idempotent 200.
- secret enable: disable then enable → active=True again; ResolveModelPolicy can select policies on it.
- secret rename: PATCH name → 200 new name, fingerprint/version unchanged.
- pagination: create 3 policies, limit=2&offset=0 → count=3, len(items)=2; offset=2 → len(items)=1.
- capability/scope negative: developer (no model_policy:* / secrets:*) → 403 missing_capability on
  each new mutating endpoint (mirror existing negative tests).

## Verification (record exit codes)
`docker exec engram-be bash -lc 'cd /srv/app && python -m pytest -p no:cacheprovider -q
engram/model_policy && ruff check engram/model_policy && ruff format --check engram/model_policy
&& python manage.py makemigrations --check --dry-run'`

No DB migration expected (no new model fields). If `makemigrations --check` reports changes, STOP —
a model field crept in; report it.
