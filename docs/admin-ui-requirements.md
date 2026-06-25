# Admin UI Requirements

## Product Shape

The admin UI is a dense operational console. It is not a marketing site and not
a decorative dashboard. Users should be able to scan tables, compare state,
filter aggressively, and act on repeated operational workflows.

## Information Architecture

V1 navigation:

- Overview;
- Organizations;
- Teams;
- Projects;
- Repositories;
- Users;
- API Keys;
- Secrets;
- Model Policies;
- Memory Review;
- AI Workflow Runs;
- Search Debugger;
- Audit;
- Health/Ops;
- Settings.

Later navigation:

- Service Accounts;
- Custom Roles;
- Memory Packs;
- Policy Packs;
- Billing;
- Legal Hold.

## Required Screens

Organizations, teams, and projects:

- create, edit, archive;
- owners and admins;
- membership table;
- project/team relationships;
- repository bindings;
- active hooks and last seen activity.

Users and API keys:

- invitation state;
- memberships;
- effective roles;
- API key fingerprint, owner, scope, expiry, last used, rotation, revocation;
- "why can this key access this memory?" inspector.

Secrets and model policies:

- masked secret creation;
- rotation test;
- dependency impact;
- inherited organization/team/project policy display;
- provider health;
- budget warning;
- fallback simulation.

Memory review:

- AI-curated queue, not raw observation firehose;
- filters by team, project, scope, confidence, conflict, age, source;
- diff between old/new memory;
- provenance and citations;
- approve, edit, narrow, reject, archive, supersede;
- bulk archive for low-confidence noise.

AI workflow runs:

- daily digest history;
- run status;
- inputs and source windows;
- curator actions;
- escalations;
- failed/refuted/contradictory decisions;
- rerun with same inputs.

Search debugger:

- replay retrieval for actor/project/query;
- show scope filters;
- exact matches;
- semantic candidates when enabled;
- final packed context;
- excluded memories and reason.

Audit:

- searchable event stream;
- actor/resource filters;
- request id and trace id lookup;
- export for selected date range;
- redacted payload preview.

Health/Ops:

- queue depth;
- outbox lag;
- worker failures;
- provider failures;
- secret-store health;
- index lag;
- hook error rate;
- incident banners.

## Interaction Requirements

- Tables need saved filters, sorting, pagination, column density, and bulk
  actions where safe.
- Every destructive action has preview and audit reason.
- Empty, loading, error, disabled, and permission-denied states are explicit.
- Redaction is visible: users should know content exists but is hidden by policy.
- Breadcrumbs and scope switchers make organization/team/project context clear.
- Mobile support is read/triage only; full administration is desktop-first.
