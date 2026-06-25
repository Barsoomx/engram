# Hook Event Coverage Security Review

Date: 2026-06-25

Branch: `feat/parity-14-hook-event-coverage`

Artifact head before evidence-head correction:
`76bd251c763513ce3d627967b592c3f9ef1fca8f`

Security fix commit: `3a3952fd303dfcf2d8a401f1cd10240380a97de2`

Result: SECURITY APPROVED after stable hook idempotency fix.

## Scope Reviewed

- `packages/cli/engram_cli/commands.py`
- `packages/cli/engram_cli/cli_lifecycle_tests.py`
- Security-reviewed hook event coverage diff from `b7aeb007` to
  `3a3952fd303dfcf2d8a401f1cd10240380a97de2`.
- Hook-event fallback id derivation for `event_id`, `idempotency_key`, and
  `content_hash`.
- Explicit hook payload id passthrough.
- Non-object payload and observation validation evidence.
- Session-start context behavior evidence.
- Remaining UUID use classification outside the hook event/idempotency/hash
  fallback path.

The focused review covered the hook-event risks required for this slice:
replay/idempotency stability, deterministic content hashing, explicit upstream
id preservation, validation of malformed hook payloads, and no new fallback path
that can create a fresh id for the same logical hook event.

## Commands And Tools Run

| Check | Result |
| --- | --- |
| Initial focused independent security review at `b7aeb007` | SECURITY CHANGES_REQUIRED. IMPORTANT finding: hook fallback generated random `event_id`, `idempotency_key`, and `content_hash` when incoming hook payloads lacked explicit ids. |
| Fix commit | `3a3952fd303dfcf2d8a401f1cd10240380a97de2` `fix: derive stable hook idempotency`. |
| Focused independent security re-review at `3a3952fd303dfcf2d8a401f1cd10240380a97de2` | SECURITY APPROVED. CRITICAL none, IMPORTANT none, MINOR none. |
| `PYTHONPATH=packages/cli python3 -m unittest discover -s packages/cli -p '*_tests.py' -v` | Exit 0. Full CLI suite reported 28 tests OK after the stable-id fix. |
| `python3 -m compileall packages/cli/engram_cli` | Exit 0. CLI package compiled. |
| `docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && pytest engram/hooks/hook_ingest_tests.py -v"` | Exit 0. Backend hook ingest tests reported 21 passed. |
| `python3 -m unittest discover -s tests -v` | Exit 0. Repository tests reported 22 tests OK. |
| `docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && pytest -v"` | Exit 0 on serial rerun. Full backend suite reported 114 passed. |
| `python3 scripts/e2e_golden_path.py` | Exit 0 after both CLI fixes. |

## Findings By Severity

### CRITICAL

None.

### IMPORTANT

Resolved: `packages/cli/engram_cli/commands.py` generated random fallback
`event_id`, `idempotency_key`, and `content_hash` values when incoming hook
payloads lacked explicit `event_id` or `idempotency_key`. That broke replay and
idempotency because identical logical hook inputs could be submitted as distinct
events.

No IMPORTANT findings remain open after re-review.

### MINOR

None.

## Fixes Applied

- Hook fallback ids now derive from stable hook event material, so identical
  hook inputs without explicit ids produce identical `event_id`,
  `idempotency_key`, and `content_hash` values.
- Changing stable hook event material changes the derived fallback ids and hash.
- Explicit `event_id`, `idempotency_key`, and `content_hash` values still win.
- No `uuid.uuid4()` call remains in the hook event/idempotency/hash fallback
  path; remaining UUID use is limited to context and dry-run request ids.

## Regression Tests Added

- `test_hook_error_derives_stable_fallback_idempotency_for_identical_input`
  verifies identical hook inputs without explicit ids derive the same
  `event_id`, `idempotency_key`, and `content_hash`, while changed stable
  material derives different values.
- `test_hook_error_preserves_explicit_idempotency_values` verifies explicit
  `event_id`, `idempotency_key`, and `content_hash` values are preserved.

## Accepted Risk

No accepted security risk remains for the focused hook-event slice. The branch
was merged through PR `#11`, and Backend, Compose E2E, and Repository Quality CI
passed on the merge commit.
