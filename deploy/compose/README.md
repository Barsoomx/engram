# Compose Deployment

Owns the future local and self-hosted Compose profile for PostgreSQL,
Redis-compatible broker, backend API, worker, optional scheduler, and frontend.

This directory is inactive in the skeleton checkpoint. It must not introduce
partial services, env files, or container images before the backend runtime
slice defines real commands and health checks.

Activation gate: first Django backend and Compose health-check slice.
