# Design: Structured weekly digest

> Roadmap Слой 3 ("renewal-driving артефакт") + closes the deferred handoff item
> A1 (`weekly_digest {merged_count, retired_count, ready}` + review action — there is
> NO weekly digest backend today, only the free-text daily digest). Base:
> `chore/pgvector-test-harness` (independent of the distillation slice — reads
> existing row state only). Tests on postgres+pgvector.

## Problem
`GenerateDigest` (`memory/services.py:880`) concatenates approved-memory text into one
free-text daily digest. There is no structured change-aggregation, no weekly window,
no `{added/merged/superseded/refuted/retired}` buckets, no GET summary endpoint, and no
"review digest" (renewal/ready) action.

## Target
A deterministic, window-scoped aggregation of memory changes into structured buckets,
persisted and exposed via a console endpoint + a review action.

## Design

### Bucket semantics (the design decision — lock these)
Window `[now - window_days, now)`, scoped by `(organization, project)`. A memory lands
in **exactly one** bucket via this precedence (highest first), so counts never
double-count:
1. `refuted` — `Memory.status == REFUTED` **or** `Memory.refuted == True`, with
   `updated_at` in the window.
2. `retired` — `Memory.status == ARCHIVED`, `updated_at` in window.
3. `superseded` — the memory has a `MemoryLink(link_type=SUPERSEDED_BY)` (it is the
   loser; also `stale=True`) created in the window.
4. `merged` — the memory has a `MemoryLink(link_type=NARROWED_BY)` created in the
   window. (There is NO native "merge" action yet; `narrow_memory`'s NARROWED_BY is the
   closest durable signal — documented approximation. When the curator slice lands it
   will produce SUPERSEDED_BY merges; revisit then.)
5. `added` — `Memory.created_at` in the window and none of the above.

`blocked` / `unresolved_questions` have NO backing model — **omit** them in this slice
(do not fabricate); leave a documented follow-up. `changelog` = a flat ordered list of
all in-window changes `{id, title(redacted), bucket, at}`.

Windowing precision note: changes (refuted/retired/superseded/merged) are windowed by
the change timestamp (`updated_at` or `MemoryLink.created_at`); `added` by `created_at`.
Any `save()` bumps `updated_at`, so this is best-effort — documented.

### Service
`memory/services.py`:
```python
@dataclass(frozen=True)
class WeeklyDigestInput:
    organization_id: uuid.UUID
    project_id: uuid.UUID
    window_days: int = 7
    request_id: str = ''
    correlation_id: str = ''

@dataclass(frozen=True)
class WeeklyDigestResult:
    digest_memory: Memory   # the persisted digest
    counts: dict[str, int]
    memory_changes: dict[str, list[dict]]
    ready: bool
```
`BuildWeeklyStructuredDigest.execute(data)`: deterministic ORM aggregation (no LLM
call needed — this is structured, not generative), build the buckets + counts +
changelog, persist a digest `Memory` with
`metadata = {'kind':'digest','digest_kind':'weekly_structured','window_start','window_end','window_days','memory_changes':{...},'counts':{...},'ready':False,'reviewed_at':None}`
(reuse the daily-digest persistence shape; redact titles). Idempotency: a
content_hash over `(project_id, window_start, window_end)` — re-run returns the existing
digest (mirror `GenerateDigest._find_existing`).
`run_weekly_digest_with_tracking(...)` mirrors `run_daily_digest_with_tracking` →
`WorkflowRun(run_type=WEEKLY_DIGEST)` (QUEUED→RUNNING→SUCCEEDED/FAILED, result_memory).

### Schema
- `WorkflowRunType.WEEKLY_DIGEST = 'weekly_digest', 'Weekly Digest'` (`core/models.py`)
  → cosmetic AlterField migration on `WorkflowRun.run_type`.
- No new model — buckets live on the digest `Memory.metadata`.

### Endpoints (each view in its own file, per repo rule)
`console/views/digests.py` + route in `console/urls.py`:
- `GET /v1/admin/digests/weekly?project_id=&window_days=` — `IsAuthenticated +
  ActiveOrganizationPermission + RequireCapability('memories:read')`. Builds (or returns
  the latest persisted) weekly digest for the active org + project; returns
  `{window_start, window_end, window_days, counts:{added,merged,superseded,refuted,retired}, memory_changes, changelog, ready}`.
- `POST /v1/admin/digests/<uuid:memory_id>/review` — `RequireCapability('memories:review')`.
  Marks the digest `Memory.metadata.ready=True` + `reviewed_at=now`, writes a
  `DigestReviewed` AuditEvent, returns `{memory_id, reviewed: true, ready: true}`. This
  is the renewal "ready" signal.

## Tests (postgres+pgvector)
`memory/weekly_structured_digest_tests.py`: build memories across states inside/outside
the window (set `created_at`/`updated_at` via `.update()` since `auto_now`), and
SUPERSEDED_BY/NARROWED_BY links; assert each bucket's membership + counts, the
one-bucket precedence (a refuted+superseded memory counts once, as refuted), idempotent
re-run, `ready=False` initially. `console/views/digests_tests.py` (mocks OK for views):
GET returns the buckets gated by `memories:read` (403 without); POST review flips
`ready=True` + writes the audit, gated by `memories:review`; tenant isolation.
Full suite green; ruff clean; `makemigrations --check` clean.

## Out of scope (follow-up)
The scheduled `generate_weekly_digest` celery task + beat entry (mechanical; the
artefact can be built on-demand via the GET endpoint first); surfacing buckets in the
WorkflowRun detail serializer; `blocked`/`unresolved` once a model exists.
