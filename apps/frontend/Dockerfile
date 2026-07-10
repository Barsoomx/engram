FROM node:24-slim AS base

ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY=localhost,127.0.0.1,::1

ENV PNPM_HOME=/pnpm
ENV PATH=$PNPM_HOME/bin:$PNPM_HOME:$PATH
RUN corepack enable \
    && corepack prepare pnpm@11.9.0 --activate \
    && pnpm config set --global minimumReleaseAge 10080 \
    && pnpm config set --global minimumReleaseAgeStrict true

WORKDIR /app

FROM base AS deps
COPY package.json pnpm-lock.yaml* pnpm-workspace.yaml* ./
RUN pnpm install --frozen-lockfile

FROM base AS builder
# Bake a placeholder, not a real URL: entrypoint.sh replaces APP_NEXT_PUBLIC_* with the
# real runtime env at container start, so one generic image serves any domain (nothing
# domain-specific is baked and no domain lives in the repo). Override the ARG only if you
# want a fixed URL baked (e.g. a local dev build).
ARG NEXT_PUBLIC_ENGRAM_API_URL=APP_NEXT_PUBLIC_ENGRAM_API_URL
ENV NEXT_PUBLIC_ENGRAM_API_URL=$NEXT_PUBLIC_ENGRAM_API_URL
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN pnpm build

FROM base AS runner
ENV NODE_ENV=production
WORKDIR /app
COPY --from=builder /app/.next ./.next
COPY --from=builder /app/public ./public
COPY --from=builder /app/node_modules ./node_modules
COPY --from=builder /app/package.json ./package.json
COPY entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh

EXPOSE 3000
# entrypoint.sh injects the real NEXT_PUBLIC_* runtime env into the built bundle, then execs the CMD.
ENTRYPOINT ["/app/entrypoint.sh"]
# Invoke next directly: `pnpm start` triggers pnpm 11 verify-deps-before-run, which
# re-runs install and fails on the gated sharp build script in this minimal runner.
CMD ["node_modules/.bin/next", "start", "-p", "3000"]
