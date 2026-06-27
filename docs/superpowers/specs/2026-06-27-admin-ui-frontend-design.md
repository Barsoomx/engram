# Phase B — Admin UI Frontend (Design)

Date: 2026-06-27
Status: Design (autonomous, decisions delegated by owner)
Stack: `apps/frontend` (Next 14 App Router, HeroUI, @tanstack/react-query, axios)
References: `asgard-admin` (frontend patterns), Phase A backend (`/v1/admin/` endpoints)

## Context

The frontend is a read-only single-org console: pages for memories/observations/audit/health/settings + stub pages for api-keys and projects. There is no organization switcher, no capability-gating, no write operations, and the api client sends no org header. Phase A delivered a full `/v1/admin/` CRUD surface (organizations/teams/projects/members/roles/api-keys) gated by RBAC capabilities and scoped by the `X-Engram-Organization` header.

Phase B turns the console into a Sentry-like admin UI that drives Phase A endpoints.

## Goal

A capability-gated, multi-org admin UI: org switcher in the header, capability-filtered sidebar, and full CRUD pages for API Keys (headline), Teams, Projects, Members, Roles, and Organization settings — wired to `/v1/admin/`.

## Non-Goals (Phase B)

- Onboarding wizard / plug-and-play bootstrap (Phase C).
- Memory review, AI workflow runs, search debugger screens (separate slices).
- Custom-role editor (roles are read + assign; the UI shows them).
- OAuth / phone login (username/password stays).

## Architecture Decisions

### AD-1: Extend `apps/frontend`, stay on Next 14
Keep Next 14.2.5 (no Next 15 churn). Add `zustand` (org store + persist) and `react-hook-form` (forms). Reuse existing HeroUI + react-query + axios.

### AD-2: Auth unchanged
Reuse the verified username/password → DRF `Token` → `localStorage` flow (`lib/auth.ts`). No next-auth, no JWT. The same token authenticates admin calls.

### AD-3: Org switcher via zustand + header
A `useOrgStore` (zustand, `persist` to `localStorage`) holds `activeOrgId`. The api client injects `X-Engram-Organization: <id>` on every request when set. The switcher lists orgs from `GET /v1/admin/organizations/` (filtered by the user's `organizations:read`). On switch, react-query cache is cleared (org-scoped keys) — see AD-6.

### AD-4: HeroUI for components, thin wrappers only
Use HeroUI `Button`, `Modal`, `Table`, `Chip`, `Input`, `Select`, `Pagination` directly (already installed). Add thin wrappers only where reused: `PageHeader`, `EmptyState`, `TableRowSkeleton`, `ConfirmDialog` (built on HeroUI Modal). Do NOT port asgard-admin's `BtnBase`/`ModalBase` — HeroUI covers it with less code.

### AD-5: Capability-gating
`/me` returns `capabilities`. Sidebar nav items declare a required capability (or null for always-visible); items are filtered. Pages gate their mutating controls (e.g. "Issue key" button) by capability via a `hasCapability(caps, code)` helper (wildcard-aware, mirroring backend `RequireCapability`).

### AD-6: Query keys include org + cache reset
Query-key factories include `activeOrgId` at the root (e.g. `['admin', orgId, 'api-keys', params]`). A `useEffect` in `Providers` watches `(token, orgId)` and calls `queryClient.clear()` on change, so no cross-org/cross-session data leaks.

### AD-7: CRUD page pattern (per resource)
Each resource page = `PageHeader` (title + actions) + `Table` (columns, search/filter `Input`/`Select`, `Pagination`, `TableRowSkeleton`/`EmptyState` states) + create/edit `Modal` (react-hook-form) + `ConfirmDialog` for destructive actions. Data via react-query `useQuery`/`useMutation`; mutations invalidate the resource's query keys. Follows the asgard-admin `employees` page shape.

## File Structure (new/modified under `apps/frontend/src`)

- `lib/auth.ts` — extend `apiClient()` to inject `X-Engram-Organization` from `useOrgStore.getState().activeOrgId`; add `hasCapability(caps, code)` helper.
- `lib/org-store.ts` — zustand store (activeOrgId, setActiveOrg), persist.
- `lib/query-keys.ts` — org-scoped query-key factories per resource.
- `lib/admin-api.ts` — typed API functions per resource (listX, createX, updateX, archiveX, revokeKey, issueKey, inviteMember...) calling `/v1/admin/...`.
- `hooks/use-<resource>.ts` — react-query hooks (query + mutation + invalidation) per resource.
- `components/ui/` — `page-header.tsx`, `empty-state.tsx`, `table-row-skeleton.tsx`, `confirm-dialog.tsx`, `capability-gate.tsx`.
- `components/layout/sidebar.tsx` — capability-gated nav with Organizations/Teams/Projects/Members/Roles/API Keys/Dashboard/Memories/Observations/Audit/Health/Settings.
- `components/layout/org-switcher.tsx` — header org selector.
- `app/(admin)/layout.tsx` — render `OrgSwitcher` in header.
- `app/(admin)/{api-keys,teams,projects,members,roles,organizations}/page.tsx` — CRUD pages (api-keys replaces stub; organizations replaces projects stub's profile-only view; new teams/members/roles pages).

## Capability → nav mapping

- Organizations — `organizations:read`
- Teams — `teams:read`
- Projects — `projects:read`
- Members — `members:read`
- Roles — `roles:read`
- API Keys — `api_keys:read`
- Dashboard/Memories/Observations/Audit/Health/Settings — always visible (read console)

## API Keys page (headline) specifics

- List: name, key_prefix, key_fingerprint, capabilities (chips), created_at, last_used_at, status (active/revoked/expired) — NEVER plaintext.
- Issue modal: name + capabilities (multi-select from the user's effective caps) → POST; on 201 show the plaintext ONCE in a read-only field with copy button + "close to dismiss" warning; closing the modal discards it.
- Revoke: `ConfirmDialog` (danger) → POST `/revoke` → invalidate.

## Testing

Frontend has no test runner configured today. Phase B adds lightweight verification: `pnpm typecheck` (tsc --noEmit) and `pnpm build` (next build) must pass on each task. (Unit/integration test setup is deferred — recorded as a gap; backend behavior is covered by Phase A tests.)

## Open decisions (accepted)

- No frontend unit tests in Phase B (typecheck + build gate only). Backend covers behavior.
- Org switcher transport = header (matches backend AD-3). Confirmed feasible with the axios client.
- asgard-admin `*-classes.ts`/`*.test.mjs` per-component split NOT ported (HeroUI + Tailwind suffice).

## Next step

`writing-plans` → ordered tasks B0–B8, TDD-via-typecheck-and-build, subagent-driven execution.
