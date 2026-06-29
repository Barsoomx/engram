# Slice 2 — AI Workflow Runs (Design)

Date: 2026-06-27
Status: Design (autonomous)

## Context

`docs/admin-ui-requirements.md` wants an AI-workflow-runs view: daily digest history, run status, inputs/source windows, curator actions, escalations, failed/refuted/contradictory decisions, rerun with same inputs. Today the daily-digest pipeline exists (`memory/services.py:GenerateDigest` ~785, `memory/tasks.py:generate_daily_digest/run_scheduled_digests`, Celery beat) and emits `AuditEvent` `DigestGenerated`, but there is **no persisted run record** (status/inputs/failure) — failures are just Celery task failures, nothing queryable.

## Goal

Persist + expose workflow runs to admins: a `WorkflowRun` model, interception of the digest pipeline to record runs, and admin endpoints (list/detail/rerun) + a frontend page.

## Architecture Decisions

### AD-1: New `WorkflowRun` model (`engram/core/models.py`)
Fields: `organization`/`project`/`team` FKs, `run_type` (TextChoices: `daily_digest`, `observation_processing`), `status` (TextChoices: `QUEUED`, `RUNNING`, `SUCCEEDED`, `FAILED`), `input_snapshot` (JSONField: `{memory_ids, window_days, ...}`), `provider_call_ids` (JSONField list), `result_memory_id` (FK Memory nullable), `escalation` (bool default False), `failure_reason` (str blank), `request_id`/`correlation_id`, `started_at`/`finished_at`. Timestamped. Migration + tests.

### AD-2: Intercept the digest pipeline
In `GenerateDigest` (or a thin wrapper in `memory/tasks.py`): create `WorkflowRun(status=QUEUED)` at start → `RUNNING` → on success `SUCCEEDED` + `result_memory_id` + `provider_call_ids`; on exception `FAILED` + `failure_reason`. Wrap so the original exception still propagates (Celery marks task failed). Record `request_id`/`correlation_id` to join with `AuditEvent` curator actions. Audit `WorkflowRunRecorded`.

### AD-3: Admin endpoints (`engram/console/views/workflow_runs.py`)
`WorkflowRunViewSet` under `/v1/admin/workflow-runs/` (`ActiveOrganizationPermission` + `RequireCapability('memories:admin')`):
- `GET /` — list, filters `run_type`/`status`/`project_id`/`team_id`/`escalation`/`created_at__gte`/`__lte`, pagination.
- `GET /{id}/` — detail: run fields + inputs joined (`input_snapshot.memory_ids` → memories) + curator actions (`AuditEvent` filtered by `request_id`/`correlation_id` in the run's window) + provider calls (`ProviderCallRecord` by `provider_call_ids`).
- `POST /{id}/rerun/` — re-trigger `generate_daily_digest.delay(org_id, project_id, memory_ids)` from `input_snapshot`; create a new `WorkflowRun` (chained `rerun_of_id`); audit `WorkflowRunReran`.

### AD-4: Frontend
- `apps/frontend/src/app/(admin)/workflow-runs/page.tsx` — list table (run_type, status chip, project, escalation, started_at, duration) + filters.
- `apps/frontend/src/app/(admin)/workflow-runs/[id]/page.tsx` — detail: status, inputs (source memories), curator actions (audit feed), provider calls, rerun button (confirm).
- `lib/admin-api.ts` + `hooks/use-workflow-runs.ts` + sidebar item (`memories:admin`).

## Non-Goals
- Observation-processing run tracking is modeled (`run_type` enum) but only daily-digest is intercepted in this slice (extend later).
- Real-time run streaming (polling via react-query refetch is fine).

## Testing
Backend (Docker pytest): model + migration; intercept records QUEUED→SUCCEEDED with result + provider_call_ids; FAILED + failure_reason on exception; list filters; detail joins inputs/audit/provider; rerun creates chained run + re-triggers. Frontend `pnpm typecheck && pnpm build`.

## Next
`writing-plans` → S2.0 (backend model+intercept+ViewSet+tests), S2.1 (frontend list+detail).
