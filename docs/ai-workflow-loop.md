# AI Workflow Loop

## Problem

Manual memory curation does not match the speed of AI-assisted development.
Team leads and memory curators should not spend their day clicking through raw
observations. The product should run scheduled AI workflows that turn noisy
session data into useful summaries, memory changes, and review exceptions.

## Schedule

V1 runs scheduled jobs once or twice per day per team/project:

- midday digest for active work;
- end-of-day digest for durable summary and cleanup.

Teams can tune the schedule, but the default should work without manual setup.
The model used by each workflow is resolved through organization/team model
policy. V1 supports both Anthropic and OpenAI backends so teams can trade off
quality and cost per task.

## Daily Team Digest

The digest answers:

- what developers worked on today;
- which branches and merge requests changed;
- which merge requests were opened, reviewed, merged, or blocked;
- which problems, errors, incidents, and review findings appeared;
- which questions remain unresolved;
- which memories were used heavily;
- which injected memories looked stale, refuted, or irrelevant.

Output shape:

- changelog bullets;
- unresolved questions;
- blocked work;
- repeated failure patterns;
- suggested memory updates;
- critical items needing human review.

The digest is a product artifact, not raw telemetry. It must cite source
observations, commits, merge requests, hook events, and prior memories.

## Autonomous Memory Curator

The curator job processes candidate memories without expecting humans to review
everything.

It should:

- merge duplicates;
- reject low-value observations;
- mark refuted memories;
- detect contradictory memories;
- lower confidence when newer evidence conflicts with old guidance;
- archive obviously obsolete memories;
- narrow over-broad scopes;
- promote high-confidence repeated facts to approved memory;
- escalate only critical or ambiguous changes.

Escalation is reserved for:

- security-sensitive guidance;
- organization-wide memory;
- model/secret/policy changes;
- contradictions with high usage;
- destructive cleanup;
- low-confidence changes that would affect many projects.

## Human Review

Human review is exception-based:

- review queue contains only escalated items;
- queue is available in the admin UI and through lead-scoped MCP tools;
- every item includes the curator's recommendation and evidence;
- reviewer can approve, edit, narrow scope, reject, archive, or request another
  check;
- reviewer decisions become training/evaluation examples for future curator
  runs.

## Audit

Every automated curator action writes an audit record:

- input window;
- model policy used;
- provider and model used;
- source ids;
- decision;
- confidence;
- scope affected;
- before/after memory version;
- whether human review was required.

Automatic deletion should be soft-delete/archive in V1. Hard deletion is a later
retention feature.
