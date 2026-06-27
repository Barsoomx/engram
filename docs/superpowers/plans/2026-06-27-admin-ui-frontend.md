# Phase B — Admin UI Frontend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers-extended-cc:subagent-driven-development. Steps use checkbox (`- [ ]`).

**Goal:** Turn the read-only console into a capability-gated, multi-org Sentry-like admin UI driving the Phase A `/v1/admin/` endpoints.

**Architecture:** Extend `apps/frontend` (Next 14). Add `zustand` (org store) + `react-hook-form` (forms). Org switcher injects `X-Engram-Organization` via the axios client; react-query keys are org-scoped and cache is cleared on org/session change. HeroUI for components (thin wrappers only). Capability-gated sidebar + page controls. CRUD page pattern: `PageHeader` + HeroUI `Table` (filter/pagination/skeleton/empty) + create/edit `Modal` + `ConfirmDialog`.

**Tech Stack:** Next 14, React 18, HeroUI, @tanstack/react-query 5, axios, zustand, react-hook-form, Tailwind, lucide-react. Verify per task: `pnpm typecheck && pnpm build`.

**User decisions (already made):** username/password auth stays (no next-auth); header-based org switcher; HeroUI not asgard's custom components; no frontend unit tests in Phase B (typecheck+build gate; backend covers behavior).

**Reference design:** `docs/superpowers/specs/2026-06-27-admin-ui-frontend-design.md`

---

## File Structure

(All under `apps/frontend/src/`)
- `lib/org-store.ts` — zustand persist (activeOrgId).
- `lib/query-keys.ts` — org-scoped factories.
- `lib/admin-api.ts` — typed `/v1/admin/*` functions.
- `lib/auth.ts` — extend `apiClient()` org header + `hasCapability()`.
- `hooks/use-<resource>.ts` — react-query hooks per resource.
- `components/ui/{page-header,empty-state,table-row-skeleton,confirm-dialog,capability-gate}.tsx`
- `components/layout/{sidebar,org-switcher}.tsx`
- `app/(admin)/{api-keys,teams,projects,members,roles,organizations}/page.tsx`

---

## Task B0: Foundation — deps, org store, api client, query keys, UI primitives

**Goal:** Add deps, `useOrgStore`, org-header in `apiClient()`, `hasCapability`, `query-keys`, org-scoped cache reset in `Providers`, and the shared UI primitives (`PageHeader`, `EmptyState`, `TableRowSkeleton`, `ConfirmDialog`, `CapabilityGate`).

**Files:** `package.json` (deps), `lib/org-store.ts`, `lib/auth.ts` (extend), `lib/query-keys.ts`, `app/providers.tsx` (cache reset), `components/ui/*`.

**Acceptance Criteria:**
- `zustand` + `react-hook-form` installed (`pnpm install`, lockfile updated).
- `apiClient()` sends `X-Engram-Organization` when `useOrgStore` has `activeOrgId`.
- `hasCapability(['api_keys:*'], 'api_keys:read')` is true; `['teams:read']` for `'api_keys:read'` is false (wildcard-aware).
- `query-keys.ts` factories embed `activeOrgId`.
- `Providers` clears react-query cache when `activeOrgId` or token changes.
- UI primitives render; `pnpm typecheck && pnpm build` pass.

**Verify:** `cd apps/frontend && pnpm install --frozen-lockfile && pnpm typecheck && pnpm build` → exit 0.

**Steps:** deps → org-store → extend apiClient → hasCapability → query-keys → Providers cache reset → UI primitives. Build after each meaningful chunk. Commit `feat: add admin ui foundation org store and primitives`.

---

## Task B1: Org switcher + capability-gated sidebar + header wiring

**Goal:** `OrgSwitcher` (HeroUI `Select`/`Menu`) in the admin header listing orgs from `GET /v1/admin/organizations/` (`organizations:read`); selecting sets `activeOrgId`. Sidebar nav filtered by capability; add Organizations/Teams/Projects/Members/Roles items.

**Files:** `components/layout/org-switcher.tsx`, `components/layout/sidebar.tsx` (capability-gate), `app/(admin)/layout.tsx` (render switcher in header).

**Acceptance Criteria:**
- Header shows `OrgSwitcher` when user has `organizations:read`; lists the user's orgs; persists selection; sends header on subsequent requests.
- Sidebar items with a `capability` are hidden when the user lacks it (wildcard-aware).
- New nav items present; `pnpm typecheck && pnpm build` pass.

**Verify:** `cd apps/frontend && pnpm typecheck && pnpm build` → exit 0.

**Steps:** org-switcher component → list orgs via admin-api → sidebar capability filter + new items → header wiring. Commit `feat: add org switcher and capability-gated sidebar`.

---

## Task B2: API Keys page (CRUD, headline)

**Goal:** Replace the stub `/api-keys` page with full CRUD: list (never plaintext), issue modal (plaintext ONCE), revoke confirm.

**Files:** `lib/admin-api.ts` (api-key fns), `hooks/use-api-keys.ts`, `app/(admin)/api-keys/page.tsx`.

**Acceptance Criteria:**
- List columns: name, key_prefix, key_fingerprint, capabilities chips, created_at, last_used_at, status; never plaintext; `api_keys:read`.
- "Issue key" button gated by `api_keys:issue`; modal (name + capabilities multi-select from effective caps); on 201 shows plaintext once in a read-only field + copy + dismiss warning; closing discards.
- "Revoke" gated by `api_keys:revoke`; `ConfirmDialog` → POST `/revoke` → invalidate.
- `pnpm typecheck && pnpm build` pass.

**Verify:** `cd apps/frontend && pnpm typecheck && pnpm build` → exit 0.

**Steps:** admin-api fns → hooks → page (table + issue modal + revoke confirm). Commit `feat: add api keys admin page`.

---

## Task B3: Teams page (CRUD)

**Goal:** `/teams` list/create/edit/archive, `teams:read`/`teams:admin`.

**Files:** `lib/admin-api.ts`, `hooks/use-teams.ts`, `app/(admin)/teams/page.tsx`.

**Acceptance Criteria:** list (name, slug, created_at), create modal (name+slug), edit, archive confirm; capability gating; `pnpm typecheck && pnpm build` pass.

**Verify:** `cd apps/frontend && pnpm typecheck && pnpm build` → exit 0.

**Steps:** follow the B2 page shape. Commit `feat: add teams admin page`.

---

## Task B4: Projects page (CRUD)

**Goal:** `/projects` (replace stub) list/create/edit/archive with repository_url/default_branch; `projects:read`/`projects:admin`.

**Files:** `lib/admin-api.ts`, `hooks/use-projects.ts`, `app/(admin)/projects/page.tsx`.

**Acceptance Criteria:** list (name, slug, repository_url, default_branch), create/edit modal, archive confirm; gating; build pass.

**Verify:** `cd apps/frontend && pnpm typecheck && pnpm build` → exit 0.

**Steps:** follow B2/B3 shape. Commit `feat: add projects admin page`.

---

## Task B5: Members page (CRUD + last-owner guard UX)

**Goal:** `/members` list/invite/change-role/deactivate; show 409 (last owner) as a user-facing error.

**Files:** `lib/admin-api.ts`, `hooks/use-members.ts`, `app/(admin)/members/page.tsx`.

**Acceptance Criteria:** list (identity, role, active), invite modal (external_id/display_name/email + role), role change, deactivate confirm; 409 → toast/alert "cannot remove the last owner"; gating; build pass.

**Verify:** `cd apps/frontend && pnpm typecheck && pnpm build` → exit 0.

**Steps:** follow shape; handle 409 in mutation error. Commit `feat: add members admin page`.

---

## Task B6: Roles page (read)

**Goal:** `/roles` list roles with nested capabilities (read-only).

**Files:** `lib/admin-api.ts`, `hooks/use-roles.ts`, `app/(admin)/roles/page.tsx`.

**Acceptance Criteria:** list (code, name, built_in, capabilities chips); `roles:read`; build pass.

**Verify:** `cd apps/frontend && pnpm typecheck && pnpm build` → exit 0.

**Steps:** follow shape (read-only). Commit `feat: add roles admin page`.

---

## Task B7: Organizations page (read + settings)

**Goal:** `/organizations` list + detail/edit (name only; slug immutable).

**Files:** `lib/admin-api.ts`, `hooks/use-organizations.ts`, `app/(admin)/organizations/page.tsx`.

**Acceptance Criteria:** list the user's orgs; edit name (`organizations:admin`); slug read-only; build pass.

**Verify:** `cd apps/frontend && pnpm typecheck && pnpm build` → exit 0.

**Steps:** follow shape. Commit `feat: add organizations admin page`.

---

## Self-Review

- Spec coverage: AD-1 deps → B0; AD-2 auth → unchanged (B0 apiClient); AD-3 org switcher → B0 store + B1 component; AD-4 HeroUI primitives → B0; AD-5 capability-gate → B1 sidebar + per-page gating; AD-6 query keys/cache → B0; AD-7 CRUD pattern → B2 (canonical) then B3–B7 mirror. Every Phase A endpoint gets a page. No placeholders.
- Frontend has no test runner; gate is typecheck+build (recorded gap in spec).
