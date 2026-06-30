# Feature 2b — model-setup frontend (operator onboarding UI)

Make model configuration understandable for an operator: a new **Model Setup** page (status +
presets + one-click apply) backed by `/v1/admin/model-setup/*`, plus DeepSeek + `base_url` support in
the existing model-policy / secret forms. App: `apps/frontend`. Consumes the slice-2a backend.

## Verify (host has node v22 + pnpm; node_modules is NOT present — install first)
From `apps/frontend`: `pnpm install` (honors the global age-gate), then `pnpm typecheck` (tsc --noEmit),
`pnpm lint` (eslint .), and `pnpm build` (next build). All must pass. Paste each command + exit code.

## 1) `src/lib/console-api.ts`
- `SecretProvider` → `'anthropic' | 'openai' | 'deepseek'`; `SECRET_PROVIDERS` → add `'deepseek'`.
- Add optional `base_url?: string` to the model-policy CREATE input type and pass it through
  `createModelPolicy` (the backend `ModelPolicyCreateSerializer` now accepts `base_url`).
- Add model-setup types + functions:
  - `type TaskTypeStatus = { task_type: string; configured: boolean; policy_id: string | null;
    provider: string | null; model: string | null; secret_active: boolean }`.
  - `type ModelSetupStatus = { task_types: TaskTypeStatus[]; ready: boolean;
    secrets: { id: string; name: string; provider: string; active: boolean }[] }`.
  - `type PresetTaskModel = { task_type: string; provider: string; model: string; base_url: string; key_slot: string }`.
  - `type ModelPreset = { key: string; name: string; description: string; providers_needed: string[];
    task_models: PresetTaskModel[] }`.
  - `type ApplyPresetRequest = { project_id: string; team_id?: string | null; scope: 'organization' | 'project' | 'team';
    preset_key: string; provider_keys: Record<string, string>; request_id: string }`.
  - `getModelSetupStatus(projectId, teamId?)` → GET `/v1/admin/model-setup/status` with query params.
  - `getModelPresets()` → GET `/v1/admin/model-setup/presets` → `{ presets: ModelPreset[] }`.
  - `applyPreset(req)` → POST `/v1/admin/model-setup/apply`.
  Use `apiClient()` from `@/lib/auth` (same as the other functions in this file).

## 2) `src/lib/query-keys.ts`
Add `modelSetupStatus(orgId, projectId)` and `modelPresets(orgId)` to `adminQueryKeys`.

## 3) NEW `src/app/(admin)/model-setup/page.tsx`  ('use client')
Mirror an existing page (e.g. `health/page.tsx` / `settings/page.tsx`) for structure: `PageHeader`,
react-query, HeroUI (`Card`, `Button`, `Input`, `Select`/`SelectItem`), the project/org stores
(`@/lib/project-store`, `@/lib/org-store`) for the active project_id + org. Sections:
- **Readiness banner**: from status `ready` — green "All model tasks configured" or amber "N of 6
  configured" with the missing task types.
- **Task-type grid** (6 cards): each shows task_type, configured ✓/✗, and provider/model + secret
  state when configured.
- **Presets**: from `getModelPresets()`. Each preset = a card (name, description, providers_needed
  chips). A "Use this preset" button opens an inline form with ONE password `Input` per
  `providers_needed` key-slot (label the slot, e.g. "DeepSeek API key", "OpenAI API key") + an Apply
  button. Apply → `applyPreset({ project_id, scope: 'organization', preset_key, provider_keys, request_id: crypto-ish unique })`
  → on success invalidate `modelSetupStatus` (refetch) + toast/inline success; on error show the
  backend `code`/`detail`.
- Gate the Apply UI with `hasCapability(capabilities, 'model_policy:*')` AND `'secrets:*'` (from the
  current user's capabilities — see how other admin pages read capabilities, e.g. via a me/auth hook
  or org-store). If lacking, show a read-only "ask an admin" note.
- Requires an active project (project-store). If none selected, prompt to pick one.

## 4) `src/app/(admin)/model-policies/page.tsx`  (EDIT — keep changes minimal + localized)
- Add `deepseek: 'DeepSeek'` to `PROVIDER_LABELS`; add a `deepseek` option to the provider `Select`
  in the create form (use `SECRET_PROVIDERS`).
- Add an optional **Base URL** `Input` to the policy create form (placeholder e.g.
  `https://api.deepseek.com/v1 or https://open.bigmodel.cn/api/paas/v4 (leave blank for default)`);
  include `base_url` in the `createModelPolicy` payload. Helper text: "For GLM / self-hosted /
  OpenAI-compatible endpoints. Leave blank to use the provider default."

## 5) `src/app/(admin)/secrets/page.tsx`  (EDIT — minimal)
- Extend `providerLabel` + the provider pill class + the create-form provider `Select` to include
  `deepseek` ('DeepSeek').

## 6) `src/components/layout/sidebar.tsx`  (EDIT — one nav item)
Add `{ href: '/model-setup', label: 'Model Setup', icon: <a lucide icon already imported or import one
e.g. SlidersHorizontal/Wand2> }` near the model-policies / settings entries.

## Constraints
- Match the existing code style (the redesign's HeroUI usage, Tailwind classes, react-query patterns).
  Read 2-3 neighbouring pages first and mirror them; do not introduce new dependencies.
- Keep edits to files #4/#5/#6 SURGICAL (these are shared with another agent) — only the lines needed.
- The page must typecheck + lint + build clean. Runtime depends on the slice-2a backend endpoints
  (already merged / in PR #52) — do not mock them.
