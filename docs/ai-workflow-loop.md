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
- escalate ambiguous (gray-band confidence) changes to an LLM judge.

Marking refuted memories, detecting contradictions, lowering confidence on
conflicting evidence, archiving, and narrowing scope are human review actions
(see Human Review), not automated curator outcomes.

Escalation is a single confidence-threshold gate: a candidate below the org's
auto-approve confidence threshold is held for human review instead of being
auto-promoted or auto-rejected.

## Human Review

Human review is exception-based:

- review queue contains only escalated items;
- queue is available through the console API (admin UI), gated by
  memories:review/memories:admin capabilities;
- every item includes the curator's recommendation and evidence;
- reviewer can approve, edit, narrow scope, reject, archive, or supersede.

## Audit

Audit records are written only for the supersede and auto-reject curator
outcomes (MemorySuperseded, MemoryAutoRejected); auto-promote does not create
an audit record. Recorded metadata is narrow: MemorySuperseded stores the
winning memory id and near-dup score; MemoryAutoRejected stores the reject
reason, body length, and optional near-dup score. Model policy, provider,
input window, before/after memory version, and a human-review-required flag
are not recorded.

Automatic deletion should be soft-delete/archive in V1. Hard deletion is a later
retention feature.
