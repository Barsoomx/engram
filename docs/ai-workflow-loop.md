# AI Workflow Loop

## Problem

Manual memory curation does not match the speed of AI-assisted development.
Team leads and memory curators should not spend their day clicking through raw
observations. The product should run scheduled AI workflows that turn noisy
session data into useful summaries, memory changes, and review exceptions.

## Schedule

V1 runs three global scheduled jobs, the same for every org/team/project:

- daily digest (crontab, 02:00 UTC);
- weekly digest (crontab, Monday 03:00 UTC);
- confidence decay (crontab, Monday 04:00 UTC).

There is no per-team/per-project schedule tuning.
The model used by each workflow is resolved through organization/team model
policy. V1 supports Anthropic, OpenAI, and DeepSeek backends so teams can trade
off quality and cost per task.

## Daily Team Digest

The daily digest consolidates a project's approved memories: it prompts the
model to de-duplicate related memories, group them by theme, and highlight
decisions, changes, and risks explicitly. The weekly digest buckets memories
by status change within the week window (refuted, archived, superseded).

Digest metadata records source memory ids, content hash, provider call id,
provider, and model. It does not track developer activity, branches, merge
requests, incidents, or review findings, and it does not cite raw
observations, commits, merge requests, or hook events.

## Autonomous Memory Curator

The curator job processes candidate memories without expecting humans to review
everything.

It does:

- merge duplicates (near-dup merge via embedding cosine similarity);
- reject low-value observations;
- promote high-confidence repeated facts to approved memory;
- escalate ambiguous (gray-band confidence) changes to an LLM judge;
- escalate candidates that trip a deterministic policy rule, regardless of
  confidence;
- hold a candidate that contradicts an existing approved memory.

Marking refuted memories, lowering confidence on conflicting evidence,
archiving, and narrowing scope are human review actions (see Human Review),
not automated curator outcomes.

Escalation to human review happens through three independent gates, any one of
which is sufficient:

1. **Confidence threshold.** A candidate below the org's auto-approve
   confidence threshold is held instead of being auto-promoted or
   auto-rejected.
2. **Deterministic policy rules.** A candidate is held, without ever reaching
   the embedding/judge pipeline, when it is organization-wide scoped
   (`visibility_scope=organization`) or its title/body contains a
   configured sensitive term (secrets, API keys, CVE identifiers, and
   similar). Both rules fail closed: they run even when the curator is
   otherwise disabled for the org
   (`OrganizationSettings.curator_enabled=False`), and even when the
   low-signal reject check has already passed. The rule set is
   configurable via `ENGRAM_CURATOR_ESCALATION_ENABLED` (default on) and
   `ENGRAM_CURATOR_SENSITIVE_TERMS`.
3. **Contradiction detection.** When the LLM judge is enabled and classifies a
   near-duplicate pair as `contradicts` (the candidate asserts the opposite of
   an existing approved memory), the candidate is held rather than merged,
   kept, or rejected. The existing memory is left untouched — still approved
   and retrievable — and a `CONFLICTS_WITH` link is recorded from the existing
   memory to the held candidate. Re-running curation on the same held
   candidate is idempotent: it does not create duplicate links or audit rows.
   Approving or rejecting the candidate during human review clears its
   conflict links.

## Confidence Decay

Approved memories that go untouched for a long time gradually lose confidence
until they surface for human review. A weekly beat job
(`engram.memory.decay_memory_confidence`, Monday 04:00 UTC) walks every
organization with `OrganizationSettings.confidence_decay_enabled=True`
(default on) and steps down the confidence of every approved, non-stale,
non-refuted memory whose `updated_at` is older than the configured minimum
age.

The staircase is deliberately simple and env-tunable:

- **step** — how much confidence drops per run
  (`ENGRAM_CONFIDENCE_DECAY_STEP`, default `0.050`);
- **floor** — the value decay will not push confidence below
  (`ENGRAM_CONFIDENCE_DECAY_FLOOR`, default `0.200`);
- **min age** — how many days a memory must sit untouched before it becomes
  eligible (`ENGRAM_CONFIDENCE_DECAY_MIN_AGE_DAYS`, default `30`).

`updated_at` is the age anchor, not `created_at`: any edit that touches a
memory's content or status resets its age, so actively-maintained memories
never decay. Digest memories (`kind=digest`) are excluded, since they are
periodic summaries rather than standalone facts; stale and refuted memories
are excluded because they are already out of the active set.

Decay feeds the same review queue as the curator's other escalation gates:
once an approved memory's confidence reaches the review threshold used by
`MemoryReviewViewSet` (`confidence <= 0.300`), it appears in the human review
queue alongside conflicted and refuted memories, even though no one manually
flagged it. Each run writes one `MemoryConfidenceDecayed` audit event per
project that had at least one memory decayed, recording the affected memory
ids (capped at 200), the count, the step, and the floor.

The org-level toggle is exposed at `GET/PUT /v1/admin/settings/retrieval`
(`confidence_decay_enabled`, default on) alongside the curator's other
retrieval settings.

## Human Review

Human review is exception-based:

- review queue contains only escalated items;
- queue is available through the console API (admin UI), gated by
  memories:review/memories:admin capabilities;
- every item includes the curator's recommendation and evidence;
- reviewer can approve, edit, narrow scope, reject, archive, restore, or
  supersede.

Every reviewer decision (approve, edit, narrow, reject, archive, restore, or
supersede) is persisted as an immutable `MemoryReviewExample` snapshot: a
redacted title/body, the item's pre-mutation status/confidence/kind/evidence,
the curator context that produced the recommendation, the reviewer's reason,
and the actor id. These snapshots are not read back into the review flow —
they exist so a future curator evaluation pass can be scored against real
human decisions. Export them for offline evaluation with
`manage.py engram_export_review_examples --organization <uuid> [--project
<uuid>] [--output <path or ->]`, which writes one JSON object per line.

## Audit

The curator (`memory/curation.py`) writes every one of its outcomes through a
single function, `_audit_curator_action`, so all curator audit rows share one
metadata contract. It covers five event types:

- **MemoryCuratorPromoted** — written for every successful promotion except a
  replayed rerun of an already-promoted candidate (reruns are not re-audited).
  `decision` carries the specific route that led to promotion:
  `passthrough` (curator disabled for the org), `no_duplicate` (embedding
  search found no near match), `embedding_unavailable` (no embedding policy
  configured, so dedup was skipped), or `judge_keep_both` (the LLM judge ruled
  the near-duplicate pair compatible). `extra.memory_id` records the promoted
  memory.
- **MemorySuperseded** — written when a near-duplicate merge marks an older
  memory stale. `target_type=memory` and `target_id` are the *losing* (now
  stale) memory, not the candidate; `extra.winner_memory_id` and
  `extra.loser_memory_id` record both sides of the merge.
- **MemoryAutoRejected** — written when a candidate is auto-rejected.
  `reason` is `low_signal` (empty or title-echoing body) or
  `near_dup_judge_reject` (the LLM judge ruled the candidate adds no durable
  value); `extra.body_length` records the rejected body's length.
- **MemoryCandidateHeldForReview** — written by the curator only for the
  deterministic escalation gate (`_hold_for_escalation`), when a candidate is
  organization-wide scoped or matches a configured sensitive term and is held
  before ever reaching the embedding/judge pipeline. `decision` is
  `held_escalation`; `reason` is `escalation:org_wide_scope` or
  `escalation:security_sensitive`. This event type is also written, with a
  different reason and a narrower metadata shape (see below), by the
  pre-curation confidence-threshold gate — the two are distinguished by the
  `reason` field, not by decision or writer location.
- **MemoryConflictDetected** — written when the LLM judge returns
  `contradicts`. `decision` is `held_conflict`; `reason` is the judge's
  redacted, 200-char-capped explanation; `extra.memory_id` is the existing
  memory the candidate conflicts with.

Every curator-written row shares this metadata shape: `candidate_id`,
`decision`, `reason`, `near_dup_score` (2-decimal string when a near-dup score
was computed, else null), `threshold` (the near-dup threshold in play, else
null), `source_observation_id` (the candidate's source observation, else
null), and `evidence_source_ids` (a deduped, order-preserving, 50-item-capped
list of ids pulled from the candidate's evidence entries, excluding conflict
markers). When the LLM judge was consulted, a `judge` object is added with
`policy_id`, `policy_version`, `provider`, `model`,
`provider_call_record_id`, and redacted before/after snapshots
(`candidate` as `{title (redacted, <=120 chars), body_sha256, body_length}`,
`existing_memory` with the same fields plus `memory_id`) — this is the judge's
full input window without storing the raw bodies. `correlation_id` on the row
propagates the caller's correlation id when supplied; `request_id` is either
empty or a synthetic curator-generated value (for example
`curator:<candidate id>`), never a caller-supplied request id. The full metadata dict is passed
through core redaction (`redact_value`) before being persisted, so any
secret-shaped string is replaced with `[REDACTED]` and any secret-named key is
fully redacted.

The pre-curation confidence-threshold hold is a separate writer: it fires
before a candidate ever reaches the curator, from `ProcessObservationRecorded`
(`memory/services.py`, per-observation path) and `DistillSession`
(`memory/distillation.py`, session-distillation path) when a newly created
candidate's confidence is below the org's auto-approve threshold. Both call
sites write the same event type and a narrower, non-uniform metadata shape:
`reason=below_auto_approve_threshold`, `candidate_id`, `confidence`,
`threshold`, and `source_observation_id` (null for session-distillation
candidates, which are synthesized from multiple observations rather than tied
to one); the distillation path additionally records `session_id`. This
metadata is redacted the same way as the curator's.

Auto-promote through the plain replay path (an already-promoted candidate
re-curated) does not create a new audit record. Model policy input beyond the
judge's redacted snapshot, full before/after memory version bodies, and a
human-review-required flag are not recorded.

Automatic deletion should be soft-delete/archive in V1. Hard deletion is a later
retention feature.
