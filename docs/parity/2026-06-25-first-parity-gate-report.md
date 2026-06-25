# First `claude-mem` Parity Gate Report

Date: 2026-06-25

Checkpoint start SHA:
`cc2f1f2e5baa9b49af74b195774d18482eb94e4f`

Upstream audit SHA:
`3fe0725a97e18b5edf3e61cde60e181ab2b6c997`

PR evidence:
`https://github.com/Barsoomx/engram/pull/11`

## Verdict

The first Codex-led CLI/hooks/API parity gate is proven by this checkpoint once
the evidence report, request-size-limit fix, security roll-up, and CI are
merged.

This report does not claim Claude Code runtime parity, MCP parity, semantic
retrieval breadth, frontend/admin readiness, production deployment readiness,
or full Engram North Star completion.

`docs/parity/claude-mem-parity-map.md` explicitly defers Claude Code native
package work, MCP bridge implementation, `PreToolUse`, `Stop`, live transcript
watching, semantic retrieval breadth, frontend/admin depth, signed plugin
release channels, and production hardening. Those are later checkpoints.

## Gate Requirements

| Requirement | Status | Evidence |
| --- | --- | --- |
| 1. Committed parity map classifies upstream behavior. | Proven | `docs/parity/claude-mem-parity-map.md` records the upstream commit, inspected files and commands, classification key, hook contracts, CLI/install behavior, session lifecycle, observation generation, memory behavior, retrieval/context behavior, MCP classification, worker replacement, and migration compatibility. |
| 2. One hook/client path submits session-start, observation/tool-use, error, and decision events to the Django API. | Proven | `packages/cli/engram_cli/main.py` exposes hook subcommands. `packages/cli/engram_cli/commands.py` posts event payloads and renders server or Codex responses. `apps/backend/engram/hooks/urls.py` exposes `dry-run`, `post-tool-use`, `session-start`, `error`, and `decision` endpoints. CLI and backend tests cover the event path. |
| 3. PostgreSQL stores raw events, observations, generated memory, retrieval documents, and context-bundle audit records. | Proven | The durable records live in `apps/backend/engram/core/models.py`. Compose uses PostgreSQL through `deploy/compose/docker-compose.yml`. The fresh `python3 scripts/e2e_golden_path.py` run on the checkpoint branch exercised the PostgreSQL-backed loop end to end. |
| 4. A worker creates or updates at least one useful memory from captured activity. | Proven | `apps/backend/engram/memory/services.py` processes `ObservationRecorded` outbox work into a memory candidate, and promotion creates approved memory, memory version, and retrieval document records. The Compose golden path promoted the captured hook observation. |
| 5. A future session receives an authorized cited context bundle containing that memory. | Proven | `apps/backend/engram/context/services.py` filters authorized retrieval documents before ranking and persists bundle/audit records. The Compose golden path submitted a later session-start request and received context containing the promoted memory. |
| 6. One Docker Compose E2E golden path proves CLI/hook to next-session context injection. | Proven | `python3 scripts/e2e_golden_path.py` exits 0 on the checkpoint branch. It starts Compose, bootstraps scope and key, runs `engram connect`, submits `hook post-tool-use`, waits for worker promotion, requests future `hook session-start`, validates context, and stops Compose. |
| 7. Existing useful upstream artifacts can be imported or reported unsupported through an idempotent fixture-backed migration path. | Proven | `apps/backend/engram/imports/upstream_import_tests.py` covers dry-run reports, approved memory/retrieval-document import, unsupported records, redaction, project/team scope validation, and rerun idempotency over checked-in sanitized fixtures. |

## Runtime Classification

Codex is the implemented first runtime path for the gate:

- `packages/codex-plugin/.codex-plugin/plugin.json`
- `packages/codex-plugin/plugin/hooks/codex-hooks.json`
- `packages/codex-plugin/codex_plugin_contract_tests.py`
- `packages/cli/engram_cli/commands.py`

Claude Code is explicitly deferred for this gate. The repo currently contains
only `packages/claude-plugin/README.md`; there is no native Claude Code plugin
manifest or Claude-specific response formatter. Do not claim Claude Code runtime
parity until a later checkpoint implements and verifies it.

MCP is audited and classified, but the MCP bridge is deferred until after the
first CLI/hooks parity loop. Do not claim MCP parity from this gate.

## Verification

Local verification on the checkpoint branch:

- `python3 scripts/e2e_golden_path.py` exited 0.
- `docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && pytest engram/hooks/hook_ingest_tests.py engram/context/context_api_tests.py -v"` exited 0 with 44 tests passed after the request-size-limit fix.
- `docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "poetry install --no-interaction --no-root --with dev && ruff check engram/hooks engram/context && ruff format --check engram/hooks engram/context"` exited 0.
- Prior focused backend/CLI/plugin/import/context/hook tests are recorded in
  `docs/verification-matrix.md`.
- Fresh independent read-only audit commands also reported:
  `python3 scripts/repository_quality.py` exit 0,
  `python3 scripts/repository_layout.py` exit 0,
  targeted CLI tests exit 0 with 28 tests OK, and targeted backend parity tests
  exit 0 with 71 tests passed.

Prior GitHub CI on the checkpoint start merge commit:

- Backend:
  `https://github.com/Barsoomx/engram/actions/runs/28172695242`
- Compose E2E:
  `https://github.com/Barsoomx/engram/actions/runs/28172695204`
- Repository Quality:
  `https://github.com/Barsoomx/engram/actions/runs/28172695225`

This checkpoint still requires its own pull-request CI to pass before merge.

## Security Evidence

Committed focused security reviews:

- `docs/security/reviews/2026-06-25-upstream-migration-import.md`
- `docs/security/reviews/2026-06-25-hook-event-coverage.md`
- `docs/security/reviews/2026-06-25-first-parity-gate-rollup.md`

The roll-up review initially found missing serializer-level request size limits
on hook/context inputs. This checkpoint adds the limits and regression tests.
The focused re-review returned SECURITY APPROVED with no remaining critical,
important, or minor findings for the scoped first parity gate.

Known pre-expansion security boundary:

- The gate is approved only for the first Codex-led CLI/hooks/API parity loop.
- Claude Code plugin distribution, MCP bridge behavior, provider secret
  adapters, semantic/vector retrieval, frontend/admin, signed plugin releases,
  and production deployment exposure are outside this gate and require their
  own focused security reviews before merge.

## Stop Point

This report satisfies the required stop-and-report checkpoint after the first
parity gate. The next work must be chosen as a new checkpoint; expansion work
must not imply that deferred Claude Code, MCP, semantic retrieval, frontend, or
production-hardening gates are already complete.
