# Engram Frontend

Next.js admin console skeleton for Engram. This is a minimal slice: a home page
and a `/health` page that render backend status from the Engram API health
endpoint.

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
| `NEXT_PUBLIC_ENGRAM_API_URL` | `http://localhost:8000` | Base URL of the Engram backend. Used by server components to reach `/-/healthz/`. The `NEXT_PUBLIC_` prefix is intentional so the value is inlined into the build when needed. |

No real secrets are required for this skeleton. Do not commit `.env` files with
real values (see `.gitignore`).

## Local development

From `apps/frontend/`:

```bash
pnpm install
NEXT_PUBLIC_ENGRAM_API_URL=http://localhost:8000 pnpm dev
```

The dev server runs on http://localhost:3000.

- `/` — Home page, fetches `${NEXT_PUBLIC_ENGRAM_API_URL}/-/healthz/` and renders
  status. Renders a static fallback message when the backend is unreachable.
- `/health` — Dedicated health page showing the same endpoint response.

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
  engram/frontend:latest
```

The image is based on `node:20-slim`, installs pnpm via corepack, builds the
Next.js app, and starts it on port 3000. `NEXT_PUBLIC_ENGRAM_API_URL` is baked
at build time for client bundle usage; for server-component fetches it is also
read at runtime, so pass it via `-e` for runtime overrides.

## Notes

- This skeleton intentionally avoids UI libraries. Styling is inline to keep the
  dependency surface minimal.
- The home page is a React Server Component that performs the health fetch on the
  server and degrades gracefully to a static fallback string when the fetch
  fails (e.g. backend not running during a static/preview build).
