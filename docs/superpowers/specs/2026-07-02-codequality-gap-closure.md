# Codequality Gap Closure — Implementation Spec

Date: 2026-07-02. Branch: `refactor/codequality-gap-closure` (one PR).
Base: master `7565c6cb`.

## Context

`codequality.md` (repo root) is the agreed refactor plan. The remediation
campaign (#58–#67) already landed most of it: DomainError conversion (#66),
django-filter adoption (#64), hooks `on_commit` (#58), review locking (#60),
altyn leftovers purge. A 7-agent verification pass (2026-07-02) mapped the
remainder: 53 plan items checked, 37 not fully done — dominated by the
structured-logging pass, small error-contract tails, TOCTOU backstops, and the
two agreed usecase adoptions.

## Decisions (leader-approved, binding for implementers)

1. **Wire contracts are frozen.** No response-shape changes anywhere:
   - Serializer pre-checks for team/project slug and member invite stay
     `serializers.ValidationError` (DRF field shape). The new `IntegrityError`
     backstops in services raise DomainError (409) and only fire in the race
     window.
   - model_policy list endpoints keep their exact current JSON shape; the
     duplicated `qs[offset:offset+limit]` slicing is extracted into one shared
     helper instead of switching to DRF pagination classes (deviation from
     plan §6, deliberate).
   - `DigestReviewView` gains an optional `digest_kind` parameter defaulting
     to `weekly_structured` (back-compat).
   - No frontend changes: `build_domain_error_payload` already dual-emits
     `code` + `error_code` (verified).
2. **No domain events** (plan §5 — zero consumers in repo).
3. **Exactly two usecase adoptions**: `SetMemberRole`/`RemoveMember` as the
   `UseCaseTransactional` proof pair, and `ReviewActionUseCase` for the review
   dispatch. Nothing else gets a usecase class.
4. Logging: structlog module logger (`logger = structlog.get_logger(__name__)`),
   event names `<entity>_<verb>` snake_case, fields from the shared vocabulary
   `organization_id` / `project_id` / `team_id` / `memory_id` / `item_id` /
   entity ids as `str(...)`. Canonical example: `hook_event_ingested`
   (hooks/services.py:151). Never log secret material — reuse `redact_value()`
   where relevant.
5. model_policy error classes move to a new `model_policy/errors.py`
   (structural only, behavior unchanged).

## Slices and file ownership

No two concurrent slices may touch the same file. Implementers edit ONLY the
files their slice owns (reading anything is fine). Tests live next to modules
as `<module>_tests.py`, pytest function style, typed fixtures, `f_`/`m_`
prefixes only for injected fixtures.

### Wave 0

**S0 — console error foundation.** Owns: `console/exceptions.py` (+ its test
file). Add DomainError subclasses mirroring `LastOwnerError`:
`TeamSlugTakenError` (409, `team_slug_taken`), `ProjectSlugTakenError` (409,
`project_slug_taken`), `MemberAlreadyInvitedError` (409,
`member_already_invited`), `DigestNotFoundError` (404, `digest_not_found`),
`InvalidRerunSnapshotError` (400, `invalid_rerun_snapshot`),
`EmbeddingFieldsRequiredError` (400, `embedding_fields_required`),
`EmbeddingSecretNotFoundError` (400, `embedding_secret_not_found`).

### Wave 1 (parallel, after S0)

**S1 — hooks views cleanup.** Owns: `hooks/views.py`, hooks view tests.
Delete the local `ACCESS_STATUS` dict + `access_error_response` helper + the
two `except AccessDeniedError` blocks (HookDryRunView.post, HookIngestView.post)
so errors flow to the global handler like the other 6 view modules. Before
deleting: verify status-code parity between the local map and
`AccessDeniedError`'s DomainError mapping; update tests asserting payload
shape (global handler dual-emits `code`).

**S2 — access login log.** Owns: `access/auth_services.py`, `access/auth_tests.py`.
`logger.info('user_login_succeeded', user_id=..., identity_id=...)` at
`LoginUser.execute` success point.

**S3 — memory app tails.** Owns: `memory/services.py`, memory tests.
(a) module logger; `memory_feedback_recorded` inside
`RecordMemoryFeedback._audit`; `memory_link_recorded` inside
`RecordMemoryLink._audit`. (b) `RecordMemoryLink.execute` passes
`MemoryLinkError` (not `MemoryVersionError`) to `lock_memory_for_update`
(move the class above `RecordMemoryLink` — no forward reference). (c) daily
digest builder stamps `metadata['digest_kind'] = 'daily_structured'` on
created daily digests (run_daily_digest path, ~services.py:1157) + test.
Old unstamped daily digests are acceptably non-reviewable.

**S4 — model_policy sweep.** Owns: `model_policy/{errors.py,services.py,views.py,filters.py}` + their tests.
(a) Extract `ModelPolicyError`, `ProviderSecretError`, `ERROR_STATUS` into
`model_policy/errors.py`; re-import in services. (b) `ProviderSecretFilterSet`
(provider, scope, active) wired into `ProviderSecretListView.get` the same
manual way as `ModelPolicyFilterSet` (views.py:253). (c) Extend
`ModelPolicyFilterSet.Meta.fields` to `['task_type','provider','scope','active']`.
(d) Extract shared limit/offset slicing helper used by both list views —
response JSON unchanged. (e) 4 logs: `provider_secret_created`,
`provider_secret_rotated`, `model_policy_created`, `model_policy_updated`
at the services' success points; no secret material.

**S5 — console settings + model_setup.** Owns: `console/views/settings.py`,
`console/views/model_setup.py`, their tests.
(a) EmbeddingSettingsView.put: replace the two `Response({'error': ...})`
returns with raised `EmbeddingFieldsRequiredError` /
`EmbeddingSecretNotFoundError`; add `embedding_settings_updated` log after
`audit_admin_action`. (b) PurgeOrganizationMemoryView.post: replace manual
`AuditEvent.objects.create(...)` with `audit_admin_action(...)` (stay inside
the existing atomic block); add `organization_memory_purged` log. (c)
ApplyPresetView.post: add `model_preset_applied` log (the 500-escape concern
is already fixed via the global handler — no other change).

**S6 — warning event-name normalization.** Owns: `context/services.py`,
`memory/curation.py`, `console/metrics_service.py` (+ tests if they assert
messages). Rename the 5 free-text `logger.warning` event names to
`<entity>_<verb>` snake_case (e.g. `query_embedding_skipped`,
`embedding_skipped`, `curator_embedding_skipped`,
`overview_metrics_cache_read_failed`, `overview_metrics_cache_write_failed`).
Keep existing keyword fields. Do NOT touch the `except Exception` in
metrics_service (accepted risk, out of scope).

### Wave 2 (parallel, after wave 1 committed)

**S7 — digests + workflow runs.** Owns: `console/views/digests.py`,
`console/views/workflow_runs.py`, their tests.
(a) DigestReviewView.post: optional `digest_kind` request field
(`weekly_structured` | `daily_structured`, default `weekly_structured`),
filter + audit metadata use it; manual `Response(404)` → raise
`DigestNotFoundError`; `digest_reviewed` log after `audit_admin_action`.
(b) workflow_runs.py: both bare `except (AttributeError, TypeError, ValueError)`
parse blocks → raise `InvalidRerunSnapshotError`.

**S8 — console CRUD backstops + logs + api-key denial audit.** Owns:
`console/services.py`, `console/views/api_keys.py`, `console/services_tests.py`,
api-keys view tests.
(a) `create_team` / `create_project` / `invite_member`: wrap the create in
`except IntegrityError` → raise `TeamSlugTakenError` / `ProjectSlugTakenError`
/ `MemberAlreadyInvitedError`. Serializer pre-checks untouched. Tests call the
service directly with a pre-existing duplicate. (b) Success logs in services:
`team_created`, `team_archived`, `project_created`, `project_archived`,
`member_invited`, `member_activated`, `api_key_issued`, `api_key_revoked`
(NOT set_member_role/remove_member — S9 owns those). (c) ApiKeyViewSet.create:
catch `CapabilityWideningError` around `_issuer_can_grant`, write
`audit_admin_action(event_type='ApiKeyIssueDenied', result=<denied>, ...)` +
`logger.warning('api_key_issue_denied', ...)`, re-raise; test asserts the
AuditEvent exists on denial.

### Wave 3 (after S8)

**S9 — members UseCaseTransactional proof pair.** Owns:
`console/usecases/` (new package), `console/views/members.py`,
`console/services.py` (only `set_member_role` / `remove_member`), members tests.
Move `set_member_role` / `remove_member` logic into `SetMemberRole` /
`RemoveMember` `UseCaseTransactional` subclasses (pydantic Input/Output DTOs
per `core/domain/usecases/base.py`); delete the service functions (single
call site each); wire views; delete the redundant `handle_exception` override
(LastOwnerError flows to global handler — frontend reads `code`, already
dual-emitted). Logs inside usecases: `member_role_changed`, `member_removed`.

### Wave 4 (after S9)

**S10 — review action usecase (the one heavy slice).** Owns:
`console/usecases/review_action.py`, `console/views/memory_review.py`,
`console/services_tests.py` (append), memory_review view tests.
(a) `ReviewActionUseCase(UseCaseTransactional)`: move the `_apply_action`
dispatch (6 branches) into `_execute` as a dispatch table keyed by action
name — drop the `# noqa: C901`; view builds input DTO and calls the usecase.
Service functions keep their own `@transaction.atomic` (nested = savepoint).
(b) `memory_review_action_applied` log (action, item_id, item_type) at the
usecase success boundary. (c) retrieve(): keep the outer candidate→memory
fallback try/except, delete the inner manual Response — second lookup's
error propagates to the global handler. (d) Concurrency regression test:
two real threads/connections on the same candidate (postgres-only marker,
mirror the existing 2-thread pattern from the ingest lock-split tests),
asserting serialized execution, no lost update.

### Final gate

Full backend suite + ruff in the tester container, cross-slice review, one PR.

## Test harness

Container `engram-tester-cq` mounts this checkout's `apps/backend` at `/app`;
postgres+pgvector at `engram-testpg` (network `engram-net`). Each slice runs
ONLY its own app tests with its assigned database:

```
docker exec -e ENGRAM_DATABASE_URL=postgresql://engram:engram@engram-testpg:5432/engram_cq<N> \
  engram-tester-cq bash -c 'cd /app && pytest <paths> -q'
```

DB assignment: S1→cq1, S2→cq2, S3→cq3, S4→cq4, S5→cq5, S6→cq6, S7→cq1,
S8→cq2, S9→cq3, S10→cq4. Full-suite gate uses `engram_cq`.

## Implementer rules

- TDD: failing test first for every behavior change (RED shown, then GREEN).
- No git commands at all — the orchestrator is the sole git owner.
- No comments/docstrings; single quotes; blank line after `return`/`raise`;
  absolute imports; no base `Exception` catches.
- Only the slice's owned files may be modified. If a required change falls
  outside them — stop and report, do not touch.
- Evidence required: exact commands, exit codes, pass/fail counts.
