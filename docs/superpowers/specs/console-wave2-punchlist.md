# Console wave-2 punchlist

Source: operator walkthrough 2026-07-08 (9 complaints), 16-agent triage with
adversarial verification, prod inspection of engram.tools.byster.one.

Root-cause map (all confirmed at file:line during triage):

- Global ProjectSwitcher force-selects the newest project (`-created_at`) and
  every admin page silently scopes to it; no "All projects" state. This alone
  explains empty /observations, all-zero dashboard, and the "no project filter"
  complaint.
- Rerun endpoint executes the full distillation/digest pipeline synchronously
  inside the HTTP request (celery gives the same work 600s; axios cuts at 15s).
- Memory-review has no bulk action for candidates; UI checkboxes silently no-op
  for PROPOSED candidates. Backlog was 3,708 (3,175 realtime junk bulk-rejected
  on prod 2026-07-08, 533 distillation candidates remain).
- Context-bundles list is hardcoded ascending `created_at` and ignores the
  `ordering` param other inspection endpoints honor; no Sort UI.
- HeroUI `datetime-local` inputs render label over native mask until focused
  (upstream `isFilledByDefault` omits `datetime-local`); 3 call sites.
- /api-keys: status filter + search already shipped (#170/#187); only the
  default (`''` = all) is wrong. Modal capability list is a flat wall of ~31
  checkboxes with no grouping/select-all.
- /digests "Generate digest now" enqueues the DAILY digest on a page titled
  Weekly; no console trigger for the existing `generate_weekly_digest` task.
  Dashboard "Generating…" actually means "not human-reviewed" (`ready` flag).

## Slices

### B1 feat/review-bulk-action (backend + frontend, one behavior)

- POST `/v1/admin/memory-review/bulk-action/` — body
  `{ids: [uuid], action: 'approve'|'reject', reason: str}`, cap
  `memories:admin`, ids capped at 200 per call.
- Per-item semantics identical to the single-item review action (reuse
  `ReviewActionUseCase`/`reject_review_item` path incl. audit + review
  examples + conflict-link cleanup); response reports per-id outcome
  (`done`/`skipped_state`/`not_found`).
- Frontend memory-review page: when selection is non-empty show a bulk bar
  (Approve / Reject with reason) wired to the new endpoint; candidates no
  longer routed to bulk-archive (which only handles Memory rows).

### C feat/workflow-rerun-async (backend + frontend)

- Rerun view pre-creates the new `WorkflowRun` row (queued, `rerun_of`,
  fresh `request_id`) and dispatches the existing celery tasks
  (`distill_session` / `generate_daily_digest` / `generate_weekly_digest`) on
  QUEUE_BATCH; returns 202 `{run_id, status: 'queued'}`.
- `distill_session` and `generate_weekly_digest` gain an optional
  `existing_run_id` param mirroring `generate_daily_digest`.
- Frontend: rerun result type updated, toast reflects queued state, run list
  invalidated so the queued run appears.

### D feat/context-bundles-ordering (backend + frontend)

- Backend: `ListInspectionContextBundles` gets an ordering allowlist
  (`created_at`, `-created_at`), default `-created_at`, consuming the
  already-validated `inspection_scope.ordering` (mirror memories pattern).
- Frontend: `ordering` param plumbed through `listContextBundles`; Sort select
  in the filter bar (default Newest first).
- New shared `DateTimeInput` component fixing the HeroUI datetime-local
  label overlap; applied to the two Since/Until inputs on context-bundles.
  (api-keys Expiry call site swaps to it in a follow-up after B1/E merge.)

### E feat/api-keys-defaults-grouping (frontend only)

- `KEY_FILTER_DEFAULTS.status` → `'active'`; explicit "All statuses" option.
- Issue-key modal: capabilities grouped by domain prefix (split on `:`),
  per-group select-all checkbox (indeterminate state) + global select-all.
  Selecting a group checks the concrete member capabilities — wildcards are
  NOT auto-submitted (operator decision 2026-07-08).

### F feat/console-scope-ux (frontend, wave 2)

- Dashboard: metrics/activity queries go org-wide (no project_id) by default;
  visible scope indicator; WeeklyDigestCard keeps project scope (needs one).
  Do not change ProjectSwitcher force-select behavior.
- memories / observations / context-bundles: project Select in the page filter
  bar (synced with the global store) + empty states that name the scoped
  project and offer switching ("No observations in <project> — you have N
  other projects").

### G feat/digest-weekly-trigger (backend + frontend, wave 2)

- Thin console endpoint POST weekly-digest run delegating to existing
  `generate_weekly_digest.delay(...)` (WorkflowRun tracking already exists).
- /digests: "Generate daily digest" (honest label) + "Generate weekly digest".
- Dashboard widget: `ready` means reviewed — render counts/changelog when
  built, label unreviewed state "Unreviewed" with CTA, never "Generating…".

### B2 feat/auto-review-loop (backend, wave 3 — own spec before implementation)

- `TaskType.AUTO_REVIEW` model policy purpose; batched structured judge
  (`review_batch_decision` response kind mirroring `curation_judgment`);
  celery beat task + console trigger; org settings
  (`auto_review_enabled` default false, batch size, max calls/run).
- Escalation/conflict-held candidates stay human-only; approvals route through
  `CurateMemoryCandidate` (near-dup safety); idempotent via WorkflowRun;
  audit per decision. Drains the remaining distillation backlog.

## Out of scope

- Cross-project ("all projects") inspection list contract — operator chose
  page-level project selector instead (2026-07-08).
- Wildcard capability auto-collapse in the issue-key modal.
- DeepSeek 402: prod provider-config issue, handled as ops (separate report).
