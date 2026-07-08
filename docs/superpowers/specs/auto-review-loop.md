# Auto-review loop (wave-2 punchlist slice B2)

Operator request 2026-07-08: the review queue is not manually tractable; an
automated loop must review proposed candidates with a smart large-context
model. After the realtime backlog bulk-reject, the queue holds ~533
distillation candidates and grows with every session below the auto-approve
threshold or held by escalation/conflict.

## Behavior

A periodic worker drains `MemoryCandidate.status=PROPOSED` through an LLM
judge in batches and applies decisions with full audit:

- `approve` → route through `CurateMemoryCandidate` (near-dup checks and
  promotion exactly as the inline curation path).
- `reject` → same semantics as the TTL sweep's reject (status change, conflict
  link cleanup, `MemoryAutoReviewed` audit with the judge's reason; no
  `MemoryReviewExample` rows — the corpus stays human-only).
- `skip` → candidate left proposed; stamped (metadata) so it is not re-judged
  before `ENGRAM_AUTO_REVIEW_RESKIP_DAYS` (default 7).

Exclusions (always human-only): candidates held for escalation or conflict,
and anything whose status is no longer PROPOSED at decision-apply time
(lock + re-check, mirroring `candidate_ttl._reject_batch`).

## Model plumbing

- `TaskType.AUTO_REVIEW = 'auto_review'` (migration; operators point a
  large-context policy at it; resolution via `ResolveModelPolicy` unchanged).
- New structured response kind `review_batch_decision` mirroring
  `curation_judgment` end to end: `_STRUCTURED_RESPONSE_KINDS` entry,
  Anthropic forced-tool schema, fake-provider payload, parser. Contract:
  `{"decisions": [{"candidate_id": "...", "decision": "approve|reject|skip",
  "reason": "..."}]}` — unknown ids ignored, absent ids = skip, malformed
  payload fails the run (no partial guessing).
- Prompt per call: project name/slug, review criteria (durable, specific,
  non-obvious engineering facts; reject transcripts/noise/ephemera; skip when
  uncertain), up to `auto_review_batch_size` candidates (id, kind, confidence,
  title, body truncated per existing distillation budget helpers), and up to
  N few-shot `MemoryReviewExample` rows (existing corpus, human decisions).

## Execution model

- Celery task `engram.memory.auto_review_candidates` on the batch queue,
  beat-scheduled (default every 30 min) + console trigger button on
  /memory-review (capability `memories:admin`) that enqueues the same task.
- Per run: oldest-first PROPOSED candidates, at most
  `auto_review_batch_size × auto_review_max_calls_per_run` candidates
  (defaults 20 × 5), one `WorkflowRun` (`run_type=AUTO_REVIEW`) for
  observability/idempotency; provider failure marks the run failed and leaves
  candidates untouched (they are picked up next run).

## Settings

`OrganizationSettings`: `auto_review_enabled` (default False),
`auto_review_batch_size` (default 20), `auto_review_max_calls_per_run`
(default 5). Migration + console settings UI exposure follow the existing
curator flags pattern.

## Out of scope

- Auto-reviewing escalation/conflict-held candidates.
- Realtime-path candidates (path disabled; backlog already drained).
- Changing the auto-approve threshold semantics at creation time.
