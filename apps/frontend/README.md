# Engram Frontend

Next.js admin console skeleton for Engram. This is a minimal slice: a home page,
a `/health` page that render backend status from the Engram API health endpoint,
and a read-only `/memories` admin page that lists memories from the inspection
API (`GET /v1/inspection/memories/`).

## Stack

- Next.js 14.x (App Router, React Server Components)
- React 18.x
- TypeScript (strict)
- pnpm (package manager)

## Prerequisites

- Node.js 20+
- pnpm 9.x (`corepack enable`)

## Environment

| Variable | Default | Purpose |
| --- | --- | --- |
| `NEXT_PUBLIC_ENGRAM_API_URL` | `http://localhost:8000` | Base URL of the Engram backend. Used by server components to reach `/-/healthz/` and `/v1/inspection/memories/`. The `NEXT_PUBLIC_` prefix is intentional so the value is inlined into the build when needed. |
| `NEXT_PUBLIC_ENGRAM_PROJECT_ID` | _(empty)_ | Project UUID used to scope inspection API requests. Required for the `/memories` page; the page renders a config-missing state when unset. Safe to expose to the client bundle. |
| `NEXT_PUBLIC_ENGRAM_TEAM_ID` | _(empty)_ | Optional team UUID used to scope inspection API requests. When unset, requests are sent without `team_id`. Safe to expose to the client bundle. |
| `ENGRA_ADMIN_API_KEY` | _(empty)_ | Admin API key sent as `Authorization: Bearer <key>` to the inspection API. Requires the `memories:admin` capability. **Server-only** (no `NEXT_PUBLIC_` prefix); never expose in the client bundle. Required for the `/memories` page. |

No real secrets are required for this skeleton. Do not commit `.env` files with
real values (see `.gitignore`).

## Local development

From `apps/frontend/`:

```bash
pnpm install
NEXT_PUBLIC_ENGRAM_API_URL=http://localhost:8000 \
NEXT_PUBLIC_ENGRAM_PROJECT_ID=<project-uuid> \
ENGRA_ADMIN_API_KEY=<admin-key> \
pnpm dev
```

The dev server runs on http://localhost:3000.

- `/` — Home page, fetches `${NEXT_PUBLIC_ENGRAM_API_URL}/-/healthz/` and renders
  status. Renders a static fallback message when the backend is unreachable.
- `/health` — Dedicated health page showing the same endpoint response.
- `/memories` — Read-only admin list of memories. Fetches
  `${NEXT_PUBLIC_ENGRAM_API_URL}/v1/inspection/memories/?project_id=...&team_id=...`
  with `Authorization: Bearer ${ENGRA_ADMIN_API_KEY}`. Renders a memories table
  or a graceful empty / config-missing / error state.

## Production build

```bash
pnpm install
pnpm build
pnpm start
```

The smoke gate for this slice is a clean `pnpm build`. Run:

```bash
pnpm install --frozen-lockfile
pnpm build
```

`pnpm lint` is also available (`next lint`).

## Docker

Build the production image from `apps/frontend/`:

```bash
docker build -t engram/frontend:latest .
```

Run (pointing at a backend reachable from the host):

```bash
docker run --rm -p 3000:3000 \
  -e NEXT_PUBLIC_ENGRAM_API_URL=http://localhost:8000 \
  -e NEXT_PUBLIC_ENGRAM_PROJECT_ID=<project-uuid> \
  -e NEXT_PUBLIC_ENGRAM_TEAM_ID=<team-uuid> \
  -e ENGRAM_ADMIN_API_KEY=<admin-key> \
  engram/frontend:latest
```

The image is based on `node:20-slim`, installs pnpm via corepack, builds the
Next.js app, and starts it on port 3000. `NEXT_PUBLIC_ENGRAM_API_URL`,
`NEXT_PUBLIC_ENGRAM_PROJECT_ID`, and `NEXT_PUBLIC_ENGRAM_TEAM_ID` are baked
at build time for client bundle usage; for server-component fetches they are
also read at runtime, so pass them via `-e` for runtime overrides.
`ENGRA_ADMIN_API_KEY` is server-only and must always be provided at runtime via
`-e` (it is never inlined into the client bundle).

## Notes

- This skeleton intentionally avoids UI libraries. Styling is inline to keep the
  dependency surface minimal.
- The home page is a React Server Component that performs the health fetch on the
  server and degrades gracefully to a static fallback string when the fetch
  fails (e.g. backend not running during a static/preview build).
- The `/memories` page is a React Server Component that fetches the inspection
  API on the server using the admin API key. It degrades gracefully when the
  project/key env vars are missing, when the fetch fails, or when the project
  has no memories.
