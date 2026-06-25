# Backend

Owns the future Django and Django REST Framework API, domain services,
PostgreSQL state, durable outbox, and worker entrypoints.

This directory is inactive in the skeleton checkpoint. It must not introduce
runtime code, migrations, service containers, provider calls, or local memory
storage until the backend gate starts with failing tests.

Activation gate: first Django backend and health-check slice.
