# Spec: Engram Console — Premium Redesign (frontend)

Source of truth: `design_handoff_engram_premium/README.md` + `design_handoff_engram_premium/screens/*.png`.
This spec captures the shared design system already implemented in the
foundation slice, plus per-screen restyle contracts for the page slices.

## Status

- Foundation (DONE): `tailwind.config.js` premium palette, Geist/Geist Mono via
  `next/font/google` in `src/app/layout.tsx`, `src/styles/globals.css` tokens +
  keyframes + helpers, `src/lib/design.ts`, shared primitives.
- Shell (DONE): `sidebar.tsx`, `(admin)/layout.tsx` top bar, `page-header.tsx`.
- Switchers (DONE): `org-switcher.tsx`, `project-switcher.tsx`,
  `team-switcher.tsx`, `switcher-ui.tsx`, `lib/switcher-store.ts`.
- Pages (THIS SLICE): restyle each screen against the primitives below.

## Hard rules for page slices

- Edit ONLY your assigned page file(s). Do NOT edit `tailwind.config.js`,
  `globals.css`, `lib/design.ts`, `lib/*-store.ts`, any file under
  `components/ui/`, `components/brand/`, `components/layout/`, or any other page.
- Keep ALL data wiring, hooks, query keys, mutations, capability gates, modals,
  toasts, and routing behavior intact. This is a visual restyle, not a rewrite.
- Reuse the shared primitives and HeroUI components. Do not hand-roll a second
  copy of something that already exists.
- Code style: single quotes; no comments/docstrings unless non-obvious; absolute
  `@/` imports; blank line after `return`/`raise`; private by default. Match the
  surrounding file.
- Must pass `tsc --noEmit`. Do not introduce `any`. Do not add dependencies.
- Icons: `lucide-react` only. Fonts already wired (`font-sans`, `font-mono`).

## Design tokens (Tailwind classes already available)

- Surfaces: `bg-background` (#0A0C11), `bg-content1` (#101319, cards),
  `bg-content2` (#161A21, hover/elevated), `bg-content3` (#1C212A, tiles),
  `bg-content4`.
- Text: `text-foreground` (#ECEEF2), `text-default-700` (#C8CDD6 bright body),
  `text-default-500` (#9197A2 muted), `text-default-400` (#666C77 faint/meta).
- Borders: `border-divider` (hairline rgba 255/.065), `border-divider-strong`
  (rgba 255/.11).
- Brand: `text-primary` / `bg-primary` (#7C5CFF), `text-primary-300` (#A78BFF),
  `bg-primary-soft`. Kind colors: `text-kind-decision|convention|gotcha|architecture`.
- Status: `text-success` (#3DD9AC), `text-warning` (#F2B765), `text-danger`
  (#FB6E72), `text-info` (#6BA6FF).
- Shadows: `shadow-primary-glow`, `shadow-dropdown`, `shadow-login-card`,
  `shadow-brand-tile`. Gradients: `bg-primary-gradient`, `bg-brand-gradient`.
- Helpers (globals): `.surface-card` (content1 + hairline + radius 16),
  `.btn-premium` (gradient CTA, used via `PrimaryButton`), `.key-cap` (mono
  chip), `.top-bar-blur`, `.animate-fade-up`, `.animate-bar-grow`, `.pulse-dot`,
  `.auth-bg`, `.tnum` (tabular-nums).

## Type scale

- Page title h1: 25px/600/-0.02em → `text-[25px] font-semibold tracking-[-0.02em]`
  (use the shared `PageHeader`).
- Detail title: 26px/600/-0.02em.
- Stat value: 27px/600/-0.02em, tabular-nums.
- Card heading h3: 14.5px/600 → `text-[14.5px] font-semibold`.
- Memory card title: 16px/600/-0.01em.
- Body/summary: 13.5px/1.6 → `text-[13.5px] leading-relaxed`.
- Detail body: 15px/1.7.
- Secondary/meta: 12px → `text-[12px]`.
- Mono detail: 11.5–12px, `font-mono`.
- Eyebrow: 10–10.5px/600, uppercase, tracking-[0.12em], `text-default-400`.

Radii: cards 16 (`.surface-card`), large panels 18 (`rounded-[18px]`), buttons
10–12, pills/badges 7 (`rounded-[7px]`), tiles 8–10. Card padding 18–22px
(`p-5`/`p-[22px]`). Hover transitions 0.14–0.16s.

## Shared primitives (import and use)

- `BrandMark`, `BrandLockup` — `@/components/brand/brand-logo`.
- `KindBadge` (kind), `KindDot` (kind, size) — `@/components/ui/kind-badge`.
- `InitialTile` — `@/components/ui/initial-tile`
  (`{ name, seed?, size?, variant?: 'gradient'|'flat', radius? }`). Org avatars
  use `variant='gradient'`; project/member/agent tiles use `'flat'` with a
  stable `seed` (slug/email).
- `ConfidenceTrack` — `@/components/ui/confidence-track`
  (`{ value: 0-100, width?, height? }`) violet gradient fill.
- `Sparkline` — `@/components/ui/sparkline`
  (`{ data: number[], color?, height?, fill? }`) inline SVG polyline.
- `PrimaryButton` — `@/components/ui/primary-button` (HeroUI `Button` props minus
  `color`/`variant`; renders the gradient+glow CTA). Use for the main page CTA.
- `PulseDot` — `@/components/ui/pulse-dot` (`{ color?, size?, pulse? }`).
- `@/lib/design`: `KIND_STYLES`, `resolveKind`, `MemoryKind`, `AVATAR_PALETTE`,
  `avatarColor`, `avatarGradient`, `initials`, `formatRelativeTime`.

## Table card pattern (projects, members, api-keys, audit)

A `.surface-card` (no inner `p-2`; use internal padding) wrapping a CSS-grid
table:
- Column header row: uppercase 10.5px/600 tracking-[0.1em] `text-default-400`,
  `border-b border-divider`, `px-5 py-3`.
- Body rows: `grid` with the screen's column ratios, `items-center gap-4 px-5
  py-3.5`, separated by `border-b border-divider last:border-b-0`, hover
  `bg-content2/60`, `transition-colors`.
- Keep existing loading skeletons + `EmptyState` + action buttons; restyle action
  buttons as small `variant='flat'` (secondary) — only the page-level primary CTA
  uses `PrimaryButton`.
- Mono for slugs, ids, repos, key prefixes, emails. Truncate long mono values.

## Per-screen contracts

Read the matching README section (`design_handoff_engram_premium/README.md`) and
screenshot before editing.

### Dashboard — `src/app/(admin)/page.tsx` (screens/01-dashboard.png)
Premium overview. The analytics here have NO backing API yet, so render them as
presentational components with representative values from the handoff (clearly
the design intent). Keep the real `useQuery(['auth','me'])` and health poll
available but the page is the designed overview, not the old debug panel.
- Header: PageHeader title 'Overview', subtitle 'Memory health across your
  organization · updated just now', action `PrimaryButton` 'Connect agent' (Plus
  icon).
- 4 stat cards (grid gap 14px): label + delta row, 27px tabular value, full-width
  `Sparkline` (height 26). Memories indexed `2,481 +12.4%`; Context bundles · 7d
  `18.2k +8.1%`; Avg retrieval `142ms −11ms`; Connected agents `3 all live`.
  Positive delta `text-success`, neutral `text-default-400`.
- 'Memory ingest' panel (`rounded-[18px]` surface): heading + 'Memories captured
  per day · last 14 days' + legend dot; 14 vertical bars (gradient violet,
  `.animate-bar-grow`, radius 5px 5px 2px 2px, ~150px area) + mono month labels.
- 'Connected agents' panel: rows in `bg-content2` cards — 32px rounded
  shield-tile (agent color), name + mono model id, `PulseDot` + Active/Idle +
  last-seen. Claude Code / claude-sonnet-4 / Active · now; Codex / gpt-5-codex /
  Active · 2m; Cursor / cursor-fast / Idle · 1h.
- 'Recent activity' panel: heading + 'View all →' (→ /audit); rows = colored dot
  + text + mono meta chip + relative time; hover content2.
- 'Weekly digest' callout: violet-tinted gradient card, Sparkles icon, 'Weekly
  digest is ready', body about 34 merged / 6 retired memories, ghost-violet
  'Review digest →' button.
Put dashboard-only subcomponents inline in this file or under
`components/dashboard/` (you own that folder).

### Memories list — `src/app/(admin)/memories/page.tsx` (screens/02-memories-list.png)
- Header: PageHeader 'Memories' / 'Engineering knowledge captured by your agents,
  ready to inject.'; action ghost Filters button (SlidersHorizontal).
- Toolbar: flex-1 search field ('Search memories, tags, files…') + kind filter
  chips (All / Decisions / Conventions / Gotchas / Architecture). Active chip =
  solid `bg-foreground text-background`; inactive = transparent
  `border border-divider-strong text-default-500`. Local `useState` for chip +
  search filter over the loaded items (client-side).
- Memory cards (stack gap 12px), each a full-width button → `/memories/{id}`:
  top row `KindBadge` (kind from `metadata.kind`/`resolveKind`) + mono file path
  (from `metadata.source` if present else '—') on left, `formatRelativeTime` on
  right; title 16px/600; summary (body) 13.5/1.6 `text-default-500` max-w-[74ch];
  footer: KindDot/project square + project + '·' + agent (from metadata if
  present) left, `{pct} conf` mono `text-default-400` + `ConfidenceTrack` right
  (pct from numeric `confidence`). Hover: `border-divider-strong`, `bg-content2`,
  `-translate-y-px`.
- Keep the 'select a project' empty state and loading/error states; restyle them.

### Memory detail — `src/app/(admin)/memories/[id]/page.tsx` (screens/03-memory-detail.png)
- '‹ All memories' back link → /memories.
- Two-column grid (1.7fr / 1fr) on lg, stack on mobile.
  - Left: `KindBadge` + mono file path; 26px/600 title; a `.surface-card` body
    card with summary (15px/1.7 `text-default-700`) + the version body; then
    'Related memories' rows (`.surface-card`, hairline) with `KindDot` + title +
    kind label. Use existing `versions`/`retrieval_documents` data to populate
    body + related (map retrieval_documents or versions into the related list).
  - Right sidebar cards: Provenance (eyebrow; Project, Captured by, Source mono
    `text-primary-300`, Updated rows); Confidence (eyebrow + big %; ConfidenceTrack
    height 7 full width; success-tinted 'Authorized for injection' chip w/ Check);
    `PrimaryButton` 'Add to context bundle'.
- Keep loading/error/no-project states. Real fields available: id, title, body,
  status, visibility_scope, current_version, confidence, stale, refuted,
  metadata, created_at, updated_at, versions[], retrieval_documents[]. Derive
  kind via `resolveKind(metadata?.kind)`, source via `metadata?.source`.

### Projects — `src/app/(admin)/projects/page.tsx` (screens/04-projects.png)
Table card, columns `1.4fr 1fr 1.7fr .8fr .8fr` (+ actions col if canAdmin):
git-graph tile (`InitialTile` flat seed=slug or a GitBranch tile) + name; mono
slug; mono repository (truncate); memory count (mono — NO data yet, render '—'
and note as gap, do not fabricate); relative updated. Header `PrimaryButton`
'New project'. Keep create/edit/archive modals + ConfirmDialog; restyle the
trigger CTA as PrimaryButton, keep row action buttons as flat.

### Members — `src/app/(admin)/members/page.tsx`
Table card columns `2fr 1fr 1fr` (+actions): Member = `InitialTile` gradient +
name + mono email; Role = colored pill (Owner=violet `bg-primary-soft
text-primary-300`, Admin=blue, else neutral `bg-content3 text-default-500`);
Status = `PulseDot` + Active/Inactive (success / default). Header `PrimaryButton`
'Invite member'. Keep invite/role/deactivate modals + toasts.

### API Keys — `src/app/(admin)/api-keys/page.tsx` (screens/05-api-keys.png)
Table card columns approx `1.2fr 1.3fr 1.1fr .8fr .8fr`: Name = Key-icon tile +
name; Key = mono masked prefix `key_prefix…`; Scope = mono violet pill on
`bg-primary-soft` (render first capability or 'multiple'); Last used = relative;
Status = dot + Active(success)/Revoked(danger)/Expired(warning). Header
`PrimaryButton` 'Issue key'. Keep issue modal (one-time secret reveal) + revoke
ConfirmDialog intact.

### Audit — `src/app/(admin)/audit/page.tsx`
Table card rows: mono method/event chip colored by action (create=success,
delete/archive=danger, change/update=info, promote=primary, login=neutral,
secret=warning) + target text + 'by {actor}' + mono relative time. Keep
no-project + loading/error states.

### Settings — `src/app/(admin)/settings/page.tsx`
Restyle into premium cards: keep Current user, Backend health, Environment,
Session(sign out) cards using `.surface-card`, eyebrow labels, mono values, and a
danger `Button` for sign out. Optionally add the designed 'Memory model' /
'Retrieval' / 'Danger zone' cards as presentational (selects + violet pill
toggles) ONLY if you can do so without new endpoints; otherwise keep the real
content restyled. Do not break the real sign-out flow.

### Login — `src/app/login/page.tsx` (screens/06-login.png)
Full-screen centered (max 392px) on `.auth-bg`. 52px `BrandMark` + 'Welcome back'
(21px/600) + 'Sign in to the Engram console'. Card (`.surface-card` radius 20,
`shadow-login-card`): Username (User icon) + Password (Lock icon, masked) inputs,
full-width `PrimaryButton` 'Sign in', 'OR' divider, bordered 'Continue with
GitHub' button (Github icon — DECORATIVE, no real OAuth; keep username/password
flow working). Footer 'Engram · engineering memory for AI agents'. Keep the real
login() submit + error handling.

### Secondary pages (theme-consistency sweep)
`observations`, `organizations`, `teams`, `roles`, `health`, `memory-review`,
`workflow-runs` (+ `[id]`): not in the handoff. Light pass only — ensure they use
`.surface-card`, the shared `PageHeader` where a header exists, hairline borders,
and the new token classes so nothing looks off-theme. Do not redesign; keep all
behavior.
