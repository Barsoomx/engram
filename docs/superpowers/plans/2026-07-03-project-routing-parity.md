# Project Routing Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make repository-url the addressing model for the whole agent surface: repo-url routing on every agent-facing endpoint via one scope-enforcing resolver (closing the latent cross-project isolation hole), one client-side precedence ladder (explicit arg > env > config > repo-derived), and `CLAUDE_PROJECT_DIR`-first derivation for the plugin MCP bridge.

**Architecture:** Per the approved spec `docs/superpowers/specs/2026-07-03-project-routing-parity-design.md` @ 82e82747 — the spec is unusually precise (file:line for every touch point, exact resolver code, per-endpoint semantics table, threat cases). Each task below names its spec sections; the spec text is the authoritative contract. Deviations from the spec require coordinator sign-off, not silent adaptation.

**Tech Stack:** Django/DRF backend (pytest, single quotes), stdlib-only CLI/MCP (unittest, double quotes), Compose e2e.

**User decisions (already made):** Auto-routing by current repository everywhere (operator directive; pinning rejected). Resolve-only + 404 for memory/observation endpoints; hooks/context/search keep resolve-or-create. Binding wins over capability in the membership guard. MCP `roots` deferred with evidence.

**Branch:** `feat/project-routing-parity` in worktree `/mnt/c/Users/filipp/Desktop/gena/_PACKAGES/engram-projrouting-wt` (base f7ec20eb + 2 spec commits).

**Working rules (all tasks):** never touch other checkouts/worktrees; commit with `--no-verify`; stage exact paths only (never `git add -A` — parallel tasks may hold uncommitted edits); backend tests run per repo practice (see `.github/workflows/backend.yml` and docs/quickstart — pgvector postgres required; use the compose/tester-container path documented in the repo, record exact commands + exit codes).

---

### Task 1: EffectiveScope.project_bound + shared resolver (backend core)

**Goal:** Add the `project_bound` flag to scope resolution and implement `resolve_project_for_scope` with the binding-wins-over-capability guard.

**Spec sections:** Architecture §1 (resolver code + guard predicate verbatim), Security threat cases 1-2, 6 (foot-gun), 11.

**Files:**
- Modify: `apps/backend/engram/core/repository.py` (add `ProjectNotFoundError`, `resolve_project_for_scope`)
- Modify: `apps/backend/engram/access/services.py` (EffectiveScope dataclass + ResolveApiKeyScope sets `project_bound=bool(key.project_id)`)
- Modify: `apps/backend/engram/access/request_scope.py` (`_session_scope` sets `project_bound=False`)
- Test: `apps/backend/engram/core/repository_tests.py` (extend), touched access tests if scope construction asserts fields

**Acceptance Criteria:**
- [ ] Resolver matrix from spec Testing §unit: project_id path; canonical repo-url match; not-found → ProjectNotFoundError(404 code `project_not_found`); empty/invalid url → `project_or_repository_required`; membership deny → AccessDeniedError('project_scope_denied') + DENIED audit with resolved project id; org-wide key + projects:agent allow (incl. just-created via allow_create); **project-bound key WITH projects:agent + foreign in-org url → denied; same key + own repo url → allowed**; session scope never takes the capability branch; cross-org url → not found (never foreign project)
- [ ] `allow_create=True` creates within scope.organization_id only, using existing `resolve_or_create_project`/canonicalization
- [ ] Full backend suite green

**Verify:** focused pytest on repository_tests + access tests, then full backend suite (same invocation as CI Backend job) → all pass, record exit code

```json:metadata
{"files": ["apps/backend/engram/core/repository.py", "apps/backend/engram/access/services.py", "apps/backend/engram/access/request_scope.py", "apps/backend/engram/core/repository_tests.py"], "verifyCommand": "full backend pytest suite (CI-equivalent invocation), exit 0", "acceptanceCriteria": ["resolver matrix incl. foot-gun negatives", "project_bound set for key and session scopes", "DENIED audit carries resolved project id", "backend suite green"], "modelTier": "standard"}
```

---

### Task 2: repo-url contract on observations + memory endpoints (backend)

**Goal:** Nine serializers accept `project_id` OR `repository_url` (one-of validation), views resolve via `resolve_project_for_scope` (allow_create=False), services untouched.

**Spec sections:** Architecture §2 (serializer list + field defs), §3 (two-step view flow + error mapping), resolve-semantics table (resolve-only rows), Security cases 5 (GET query param), 7 (replay), Testing §API.

**Files:**
- Modify: `apps/backend/engram/observations/serializers.py`, `apps/backend/engram/observations/views.py`
- Modify: `apps/backend/engram/memory/serializers.py`, `apps/backend/engram/memory/views.py`
- Test: the api tests next to each module (`observations_api_tests.py`, memory feedback/versioning/links/diff test modules — extend existing files)

**Acceptance Criteria:**
- [ ] Per endpoint (observations list+detail; feedback; version POST+GET; links POST+GET+DELETE; diff): repo-url happy path with org-wide agent key; unknown repo → 404 `project_not_found`; cross-org url → 404; project-scoped key + foreign in-org url → 403 `project_scope_denied`; missing both → 400 `project_or_repository_required`; project_id override unchanged; project_id+repository_url → project_id wins
- [ ] Console/session flows regress nothing (existing tests untouched and green)
- [ ] Replay: repeated feedback same request_id in repo-url mode → `already_applied`, no duplicates
- [ ] Full backend suite green

**Verify:** focused API test modules then full backend suite → pass, exit codes recorded

```json:metadata
{"files": ["apps/backend/engram/observations/serializers.py", "apps/backend/engram/observations/views.py", "apps/backend/engram/memory/serializers.py", "apps/backend/engram/memory/views.py"], "verifyCommand": "full backend pytest suite (CI-equivalent invocation), exit 0", "acceptanceCriteria": ["9 serializers one-of validation", "views two-step resolve, services untouched", "full negative matrix per endpoint", "replay idempotency in repo-url mode", "backend suite green"], "modelTier": "standard"}
```

---

### Task 3: retrofit hooks/search/context through the resolver (security fix)

**Goal:** Replace the inline project resolution in hooks/search/context services with `resolve_project_for_scope` (allow_create=True) so the membership guard closes the latent cross-project hole everywhere.

**Spec sections:** Architecture §1 "Retrofit (security fix)", Security cases 2, 6, 11, Testing §retrofit-regression.

**Files:**
- Modify: `apps/backend/engram/hooks/services.py`, `apps/backend/engram/search/services.py`, `apps/backend/engram/context/services.py`
- Test: extend the security/regression tests next to each module

**Acceptance Criteria:**
- [ ] Project-scoped key + foreign in-org repo url → denied (403 + DENIED audit) on hooks ingest, search, context session-start
- [ ] Project-bound key WITH projects:agent → same denial (foot-gun pin); own-repo url → allowed
- [ ] Org-wide agent key behavior unchanged — ALL existing hooks/search/context tests pass unmodified (except any that pinned the insecure behavior — flag those explicitly to the coordinator, do not silently rewrite semantics)
- [ ] Full backend suite green

**Verify:** focused hooks/search/context test modules then full backend suite → pass

```json:metadata
{"files": ["apps/backend/engram/hooks/services.py", "apps/backend/engram/search/services.py", "apps/backend/engram/context/services.py"], "verifyCommand": "full backend pytest suite (CI-equivalent invocation), exit 0", "acceptanceCriteria": ["cross-project deny on all three retrofitted paths", "foot-gun negative pinned", "org-wide key behavior unchanged", "backend suite green"], "modelTier": "standard"}
```

---

### Task 4: client precedence ladder + CLAUDE_PROJECT_DIR derivation (CLI + MCP)

**Goal:** One ladder everywhere (arg > env > config > repo-derived), repo-derivation source ladder (CLAUDE_PROJECT_DIR > cwd for the MCP bridge), userinfo stripping, four commands + four tools un-gated.

**Spec sections:** Architecture §4 in full (derivation ladder, mcp_tools/commands/mcp_server changes), Decision "One precedence ladder", Security case 5, Testing §CLI + §MCP.

**Files:**
- Modify: `packages/cli/engram_cli/mcp_tools.py`, `packages/cli/engram_cli/mcp_server.py`, `packages/cli/engram_cli/commands.py`, `packages/cli/engram_cli/main.py`
- Test: `mcp_tools_tests.py`, `mcp_server_tests.py`, `cli_lifecycle_tests.py`/command tests (extend)
- Regenerate: plugin bundle via `python3 scripts/sync_plugin_bundle.py` (+ `--check`)

**Acceptance Criteria:**
- [ ] `workspace_repository_url()` helper: CLAUDE_PROJECT_DIR beats cwd (test with two distinct git repos); cwd fallback intact; userinfo stripped from `git_remote_url` output (https://user:token@host → https://host)
- [ ] Four MCP handlers lose the project gate; `_scope_payload` used for POSTs; observations GET sends repository_url query param; `project_not_found` rendered as the spec's guidance text; optional `project_id` argument on all six tool schemas (ladder rung 1)
- [ ] CLI: `--project` flag on search/observations/memory commands; ENGRAM_PROJECT_ID rung honored (incl. hooks payload builder); client-side missing_project error when nothing resolves (never empty-UUID payloads)
- [ ] Bundle in sync; CLI suite + plugin suite green (record counts)

**Verify:** `PYTHONPATH=packages/cli python3 -m unittest discover -s packages/cli -p '*_tests.py' -v` && plugin suite && `python3 scripts/sync_plugin_bundle.py --check` → all pass

```json:metadata
{"files": ["packages/cli/engram_cli/mcp_tools.py", "packages/cli/engram_cli/mcp_server.py", "packages/cli/engram_cli/commands.py", "packages/cli/engram_cli/main.py"], "verifyCommand": "PYTHONPATH=packages/cli python3 -m unittest discover -s packages/cli -p '*_tests.py' -v && PYTHONPATH=packages/claude-plugin python3 -m unittest discover -s packages/claude-plugin -p '*_tests.py' -v && python3 scripts/sync_plugin_bundle.py --check", "acceptanceCriteria": ["CLAUDE_PROJECT_DIR-beats-cwd + userinfo strip", "gates removed, six tool schemas gain project_id arg", "--project/env rungs uniform incl. hooks", "bundle in sync, suites green"], "modelTier": "standard"}
```

---

### Task 5: golden-path e2e — repo-url-mode MCP drive (USER GATE)

**Goal:** Close the blind spot: second `drive_mcp_stdio` pass with a config WITHOUT project_id, cwd inside a git repo whose origin matches the bootstrap project's repository_url — all six tools must succeed via server-side repo-url resolution.

**USER-ORDERED GATE — NON-SKIPPABLE.** Close only after a real green run with captured output.

**Spec sections:** Testing §E2E, Acceptance Criteria 1-2, 7.

**Files:**
- Modify: `scripts/e2e_golden_path.py`

**Acceptance Criteria:**
- [ ] Bootstrap project gets a repository_url (check/extend `engram_bootstrap_golden_path` args if it doesn't set one — coordinate if backend change needed); a temp git repo with that origin is created; third config dir connected WITHOUT `--project`; second MCP drive runs all six tools from that cwd and asserts non-error, content-bearing results (memory_id asserted for search; distinct current_version pair for the two version calls; stale=True for feedback)
- [ ] Local full run: `python3 scripts/e2e_golden_path.py` → exit 0, `MCP stdio bridge passed` (pinned mode) AND the new `MCP repo-url mode passed` progress line (frontend excluded locally via the existing gitignored override — environmental, CI builds full stack)

**Verify:** `python3 scripts/e2e_golden_path.py` → exit 0 with both MCP progress lines; capture output

```json:metadata
{"files": ["scripts/e2e_golden_path.py"], "verifyCommand": "python3 scripts/e2e_golden_path.py", "acceptanceCriteria": ["repo-url-mode drive of all six tools green against live backend", "pinned-mode drive still green", "exit 0 with captured evidence"], "modelTier": "standard", "userGate": true, "tags": ["user-gate"]}
```

---

### Task 6: docs + matrix + changelog

**Goal:** Contracts documented: remove the asymmetry from guides, document the ladder + CLAUDE_PROJECT_DIR, api/backend contracts updated, verification-matrix checkpoint entry, CHANGELOG entry.

**Spec sections:** Architecture §5.

**Files:**
- Modify: `docs/guides/mcp.md` (asymmetry §108-116 → ladder + repo-url semantics), `docs/mcp-tools.md`, `docs/backend-contracts.md`, `docs/api-reference.md` (check it exists — else the contracts doc), `docs/verification-matrix.md` (new checkpoint entry), `CHANGELOG.md` ([Unreleased])
- Also: `docs/guides/cli.md` if it documents the affected commands

**Acceptance Criteria:**
- [ ] No doc claims the four tools require a connected project; ladder documented once and referenced; `project_not_found` + `project_scope_denied` error codes documented; CLAUDE_PROJECT_DIR noted for the plugin bridge
- [ ] verification-matrix entry with the real commands/exit codes from Tasks 1-5

**Verify:** `grep -rn 'requires a connected project\|require an explicit connected project' docs/` → only historical files; doc commands parse against the real CLI

```json:metadata
{"files": ["docs/guides/mcp.md", "docs/mcp-tools.md", "docs/backend-contracts.md", "docs/verification-matrix.md", "CHANGELOG.md"], "verifyCommand": "grep -rn 'requires a connected project' docs/ (historical only); CLI --help parse checks", "acceptanceCriteria": ["asymmetry removed from docs", "ladder + error codes documented", "matrix checkpoint appended"], "modelTier": "mechanical"}
```

---

## Execution notes

- Order: 1 → 2 → 3 (backend serialized in one worktree) with 4 in parallel (disjoint files, exact-path staging); 5 after 2+3+4; 6 after 4. Final whole-branch review → push → PR → CI → merge (operator mandate: drive to master).
- Security review is part of the loop (repo cadence): the per-task reviews must explicitly check the threat cases from the spec's Security section; the final review re-runs the checklist.
- One coordinator owns git operations during any parallel phase.
