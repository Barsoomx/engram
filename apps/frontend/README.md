# Engram Frontend

Next.js admin console for Engram. Provides token-based login (username/password),
an admin shell with sidebar navigation, a dashboard with backend health and user
scope, plus read-only Memories and Health pages.

## Stack

- Next.js 14.x (App Router)
- React 18.x
- TypeScript (strict)
- HeroUI (`@heroui/react`, `@heroui/system`, `@heroui/theme`) component library
- Tailwind CSS 3.x (dark theme by default)
- @tanstack/react-query (server state)
- axios (HTTP client)
- lucide-react (icons)
- pnpm (package manager)

## Routes

| Route | Auth | Description |
| --- | --- | --- |
| `/login` | public | Username + password login. Stores the auth token in `localStorage` and redirects to `/`. Redirects to `/` if a token is already present. |
| `/` | required | Dashboard. Backend health status, signed-in user info, capabilities. Lives inside the admin shell. |
| `/memories` | required | Read-only memory list from `/v1/inspection/memories/`, authenticated with the stored token. |
| `/observations` | required | Placeholder route for the observations admin (sidebar link active). |
| `/audit` | required | Placeholder route for the audit admin (sidebar link active). |
| `/health` | required | Dedicated backend health page (`/-/healthz/`). |

`required` means the admin shell redirects to `/login` when no token is in
`localStorage`.

## Architecture

```
src/
  app/
    layout.tsx          # Root layout: dark theme, HeroUI + react-query Providers
    providers.tsx       # HeroUIProvider + ToastProvider + QueryClientProvider
    login/page.tsx      # Login page (client component)
    (admin)/
      layout.tsx        # Admin shell: sidebar + header, token gate
      page.tsx          # Dashboard (health + user scope)
      memories/page.tsx # Memories list (auth token via apiClient())
      health/page.tsx   # Backend health detail
  components/
    layout/sidebar.tsx  # Sidebar navigation
  lib/
    auth.ts             # Token storage + axios client + login/me/logout
  styles/
    globals.css         # Tailwind base + HeroUI CSS variables
```

The admin shell is a client component route group (`(admin)`). It reads the token
from `localStorage` on mount and redirects to `/login` when absent. The user
profile (`GET /v1/auth/me`) is fetched once via react-query and shared between
the sidebar/header and the dashboard through the `['auth','me']` query key.

## Authentication

The frontend uses Django REST framework Token auth (not next-auth):

- `POST /v1/auth/login` `{username, password}` -> `{token, user_id, username, identity_id, organization_id, capabilities}`
- `GET /v1/auth/me` (header `Authorization: Token <key>`) -> `{user_id, username, identity_id, organization_id, capabilities}`
- `POST /v1/auth/logout` (header `Authorization: Token <key>`) -> clears server-side token

The token is stored under `localStorage['engram_token']` and attached to every
request by the axios instance returned from `apiClient()` in `src/lib/auth.ts`.

## Environment

| Variable | Default | Purpose |
| --- | --- | --- |
| `NEXT_PUBLIC_ENGRAM_API_URL` | `http://localhost:8000` | Base URL of the Engram backend. `NEXT_PUBLIC_` prefix inlines it into the client bundle. |
| `NEXT_PUBLIC_ENGRAM_PROJECT_ID` | _(empty)_ | Project UUID used to scope inspection API requests (Memories page). Safe to expose to the client bundle. |
| `NEXT_PUBLIC_ENGRAM_TEAM_ID` | _(empty)_ | Optional team UUID used to scope inspection API requests. Safe to expose to the client bundle. |

No server-only admin API key is required for the authenticated admin pages: the
browser sends the user token from `localStorage`. Do not commit `.env` files with
real values (see `.gitignore`).

## Local development

From `apps/frontend/`:

```bash
pnpm install
NEXT_PUBLIC_ENGRAM_API_URL=http://localhost:8000 \
NEXT_PUBLIC_ENGRAM_PROJECT_ID=<project-uuid> \
pnpm dev
```

The dev server runs on http://localhost:3000.

1. Open `/login` and sign in with an Engram username and password.
2. On success you are redirected to the dashboard at `/`.

## Production build

```bash
pnpm install --frozen-lockfile
pnpm build
pnpm start
```

The smoke gate for this slice is a clean `pnpm build`. `pnpm lint` and
`pnpm typecheck` are also available.

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
  engram/frontend:latest
```

`NEXT_PUBLIC_*` vars are baked at build time for client bundle usage and are also
read at runtime for browser fetches, so pass them via `-e` for runtime overrides.
