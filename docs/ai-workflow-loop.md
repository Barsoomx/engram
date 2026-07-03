# AI Workflow Loop

## Problem

Manual memory curation does not match the speed of AI-assisted development.
Team leads and memory curators should not spend their day clicking through raw
observations. The product should run scheduled AI workflows that turn noisy
session data into useful summaries, memory changes, and review exceptions.

## Schedule

V1 runs two global scheduled jobs, the same for every org/team/project:

- daily digest (crontab, 02:00 UTC);
- weekly digest (crontab, Monday 03:00 UTC).

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

## Human Review

Human review is exception-based:

- review queue contains only escalated items;
- queue is available through the console API (admin UI), gated by
  memories:review/memories:admin capabilities;
- every item includes the curator's recommendation and evidence;
- reviewer can approve, edit, narrow scope, reject, archive, or supersede.

## Audit

Audit records are written for these curator outcomes: MemorySuperseded
(near-dup merge), MemoryAutoRejected (low-signal or judge reject),
MemoryCandidateHeldForReview (deterministic policy escalation), and
MemoryConflictDetected (judge contradiction hold). Auto-promote does not
create an audit record. Recorded metadata is narrow: MemorySuperseded stores
the winning memory id and near-dup score; MemoryAutoRejected stores the reject
reason, body length, and optional near-dup score;
MemoryCandidateHeldForReview stores an `escalation:<rule>` reason and the
candidate id; MemoryConflictDetected stores the candidate id, the conflicting
memory id, the near-dup score, and a redacted, length-capped judge reason.
Model policy, provider, input window, before/after memory version, and a
human-review-required flag are not recorded.

Automatic deletion should be soft-delete/archive in V1. Hard deletion is a later
retention feature.
