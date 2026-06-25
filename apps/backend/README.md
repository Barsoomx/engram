# Backend

Owns the Django and Django REST Framework API, domain services, PostgreSQL
state, durable outbox, and worker entrypoints.

The first active backend gate adds the runtime shell and health endpoints. Hook
ingest, tenancy/RBAC, memory storage, durable outbox, provider calls, and
context retrieval remain deferred to later parity slices.

Current gate: first Django backend and health-check slice.
