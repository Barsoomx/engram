# Checkpoint 1 Lossless Work Creation

Date: 2026-07-10

Status: proposed focused specification for C1.1 through C1.3

Roadmap gate: Checkpoint 1 — Lossless Work Creation

Depends on:

- `docs/decisions/2026-07-10-domain-progress-and-transport.md`;
- `docs/superpowers/specs/2026-07-10-checkpoint-0-reliability-contract.md`;
- `docs/reliability/memory-loop-invariants.md`;
- `docs/reliability/memory-loop-fault-matrix.md`.

## Goal

After Engram acknowledges new evidence or commits a lifecycle/schedule
transition, every required product-domain work identity and its initial
package-owned delivery signal are already durable. If that transaction rolls
back, evidence, logical work, and the package row all roll back.

Checkpoint 1 introduces stable logical identity and lossless creation. It does
not yet promise bounded leasing, retry scheduling, reconciliation, full input
coverage, or atomic semantic promotion. Those gates belong to Checkpoints 2–4.

## Success Boundary

For post-cutover traffic:

- each acknowledged raw envelope has exactly one same-scope normalized
  disposition;
- a transition whose frozen policy requires work has exactly one scoped
  `WorkflowWork` identity;
- first creation of required work calls the approved outbox-backed Celery
  boundary exactly once inside the same database transaction;
- process death after commit cannot erase the work or its committed package
  signal;
- duplicate delivery converges on the same evidence and logical work;
- task payloads contain stable work/run ids only;
- organization, project, and derived team are resolved before evidence, work,
  package creation, subject loading, or provider execution;
- digest source visibility is authorized for the eventual output before source
  selection, freezing, provider execution, or publication;
- legacy digest output without exact authorization linkage is quarantined from
  autonomous inputs and ordinary content reads without historical mutation.

Historical rows remain visible and may remain unlinked. Checkpoint 1 records
their gaps but does not bulk-create historical work or claim global historical
P1/P2 health.

## Non-Goals

Checkpoint 1 does not add:

- an Engram relay, broker status, delivery status, package mirror, transport
  retry, dead-letter, or queue polling model;
- logical-work leases, owners, heartbeats, fencing tokens, retry budgets,
  `next_retry_at`, reconciliation, or repair commands;
- candidate-decision work, chunk coverage, atomic promotion, autonomous
  curation, temporal validity, Memory CI, or retrieval optimization;
- a public `WorkflowWork` API, console screen, management command, or scheduler
  for the invariant evaluator;
- historical missing-work repair;
- mutation, deletion, or semantic reinterpretation of legacy digest rows;
- digest late-arrival/carry-forward coverage for transactions invisible when a
  closed occurrence first freezes (CP4);
- long-lived task payloads containing evidence, memory text, prompts, provider
  output, secrets, or API credentials.

## Current Runtime Contradictions

The implementation slice starts from these verified behaviors, not from the
target contract:

- hook duplicate paths return existing evidence before work repair;
- hook observation and explicit-end dispatch use post-commit callbacks, and
  lifecycle observations currently receive realtime processing tasks;
- idle sweep commits the ended state before its caller dispatches;
- manual digest/rerun rows commit separately from task creation and audit;
- scheduled digest tasks carry mutable ids and reselect current state;
- project-wide legacy digests can select team-visible sources and publish the
  derived result as project-visible without freezing an authorization policy;
- retrieval documents and stored context replay trust that legacy project
  visibility, and current console digest/rerun paths narrow only to active
  organization plus capability rather than `request.effective_scope`;
- the legacy distillation reconciler treats any historical session success as
  final rather than an exact generation;
- context session resolution may change derived team without the session lock.

Each contradiction gets a focused RED before its producer is switched.

## Serial Delivery Spine

Checkpoint 1 has three serial gates. Do not stack or implement a later slice
before the earlier slice is merged and its rollout gate is recorded. C1.3 is
itself split into non-stacked rollout PRs so data backfill and the non-null
contract never deploy together.

1. **C1.1 — identity and expand schema**
   Add logical-work persistence, the nullable run link, the session sequence
   expand fields, canonical identity services, versioned id-only tasks, model
   tests, and migration tests. Preserve old session writers with a persistent
   database default and a state-neutral repair migration for installations that
   already recorded the originally published expand migration. No producer
   switches yet.
2. **C1.2 — hook/API atomic creation and all observation writers**
   Make hook evidence, observation work, and its package signal atomic. Move
   hook and import observation creation onto the server sequence allocator.
   Deploy this slice and drain old writers before C1.3.
3. **C1.3 — sequence contract, lifecycle, rerun, and schedules**
   Deliver in this order: C1.3a deploys deterministic sequence backfill alone;
   C1.3b applies the separately verified non-null contract and migrates
   explicit/idle session end plus compatibility retries; C1.3c adds immutable
   digest work execution and migrates manual digest/new-format reruns; C1.3d
   migrates daily/weekly schedules, the daily management command, and the final
   legacy-producer census. Legacy tasks remain registered until their packages
   drain.

Only one Git owner commits, pushes, and updates the checkpoint PR. Parallel
agents may own disjoint tests/research/reviews within the active serial slice.

## Authority Boundary

`django-celery-outbox` remains the sole transport authority. Its package rows
own delivery intent, relay publication, transport retries, and dead letters.
Engram neither mirrors nor polls those states.

`WorkflowWork` owns only product-domain facts:

- the stable scoped identity of an exact required input;
- the immutable input snapshot and fingerprint;
- whether product work is still required, completed, or an explicit no-op;
- the stable relation from one or more `WorkflowRun` attempts to that logical
  requirement.

An empty package queue is never completion evidence. Logical identity and the
initial signal are created once; package delivery and Celery task execution
remain at-least-once. Checkpoint 1 does not claim exactly-once semantic output:
provider execution may repeat, and later checkpoints add durable stage coverage
and general atomic promotion. The digest-only publication/linkage exception
defined below does not broaden that claim.

## Additive Data Contract

### Enums

Add these domain choices beside the existing workflow choices:

```text
WorkflowWorkType
  observation_processing
  session_distillation
  daily_digest
  weekly_digest

WorkflowSubjectType
  observation
  agent_session
  project
  team

WorkflowWorkDisposition
  required
  complete
  no_op

WorkflowWorkResolutionReason
  succeeded
  no_signal
  no_input

RawEventNormalizationDisposition
  observation
  no_op

RawEventNormalizationReason
  evidence_only
```

Operational attempt status remains `WorkflowRunStatus`. Do not add
ready/running/failed/retry concepts to `WorkflowWork.disposition`. The accepted
CP0 term `required` means that no terminal product disposition exists yet; CP2
operational states remain orthogonal.

### Raw Event Normalization Disposition

Add nullable expand fields to `RawEventEnvelope`:

| Field | Type | Contract |
|---|---|---|
| `normalization_contract_version` | nullable positive small integer during expand | Null means not yet characterized; new C1.2 evidence writes 1 |
| `normalization_disposition` | nullable char(20) | `observation` or `no_op` for versioned evidence |
| `normalization_reason` | nullable char(40) | Required only for a declared no-op |

Database checks allow exactly these combinations:

- expand legacy: all three null;
- version 1 observation: disposition `observation`, null reason;
- version 1 no-op: disposition `no_op`, declared reason
  `evidence_only`.

Index `(organization, project, normalization_contract_version,
normalization_disposition)`. A version 1 observation disposition must have
exactly one same-scope
`ObservationSource.raw_event` link to a same-scope observation; a version 1
no-op must have zero such links. Cross-table cardinality stays in P1 and its
transaction/fault tests rather than a trigger.

Hook events, including lifecycle events, normalize to observations. Imported
prompt-only evidence normalizes to `no_op/evidence_only`; imported
observation/summary evidence normalizes to observations. Missing-session or
unsupported import rows that create no `RawEventEnvelope` remain import-report
outcomes, not fabricated raw-event dispositions.

After old writers drain, an operational gate records the exact deployed
revision inventory, proves every old API/worker/import/command process has
terminated, and proves no transaction from a retired revision remains open.
A database timestamp may be recorded for diagnostics, but random UUID order or
that timestamp alone is never drain authority. If transaction attribution is
unavailable, the rollout stops rather than guessing. The contract migration
then marks all remaining all-null historical/mixed-rollout rows as
`(version=0, disposition=NULL, reason=NULL)`, makes version non-null, and
replaces the expand check with a final check allowing only v0 legacy or the two
v1 combinations. The backfill and `NOT NULL` alteration are one transaction;
a concurrent retired writer that inserts null makes the migration fail and
roll back rather than silently becoming v0. This prevents a future buggy writer
from hiding new rows in the legacy null cohort.

A locked duplicate may promote legacy null/v0 evidence to v1 only after it can
prove the exact intended relation: observation requires exactly one valid
same-scope source; evidence-only requires zero sources. Multiple/cross-scope
sources or a conflicting intended disposition fail closed and are never
deleted or reclassified. Capturing fallback work policy alone does not imply a
normalization upgrade.

### WorkflowWork

Add `WorkflowWork` to `engram.core.models` as a `TimestampedModel`:

| Field | Type | Contract |
|---|---|---|
| `organization` | FK `Organization`, CASCADE | Required scope |
| `project` | FK `Project`, CASCADE | Required scope; must belong to organization |
| `team` | nullable FK `Team`, PROTECT | Derived from typed subject; never identity |
| `work_type` | char(40), `WorkflowWorkType` | Product workflow |
| `subject_type` | char(40), `WorkflowSubjectType` | Typed subject discriminator |
| `subject_id` | UUID | Subject id reloaded under explicit scope |
| `contract_version` | positive small integer, default 1 | Identity/canonicalization version |
| `occurrence_key` | char(255), blank | Stable producer occurrence; required only for digest work |
| `input_fingerprint` | char(64) | Lowercase SHA-256 hex |
| `input_snapshot` | JSON | Immutable canonical domain inputs/policy |
| `disposition` | char(20), default `required` | Product disposition only |
| `resolution_reason` | char(40), blank | Typed terminal resolution |
| `resolved_at` | nullable timestamp | Null only while `required` |

Constraints:

- unique `(organization, project, work_type, subject_type, subject_id,
  contract_version, input_fingerprint)`;
- conditional unique `(organization, project, work_type, subject_type,
  subject_id, contract_version, occurrence_key)` when occurrence key is not
  blank;
- `contract_version > 0`;
- fingerprint matches lowercase 64-character hexadecimal form;
- `observation_processing` pairs only with `observation`;
- `session_distillation` pairs only with `agent_session`;
- daily digest pairs only with `project`, requires `subject_id = project_id`,
  null team, and non-blank occurrence key;
- weekly digest pairs with project or team; project subject requires
  `subject_id = project_id` and null team, while team subject requires the
  stored/derived team id to equal `subject_id` and a same-organization
  `ProjectTeam` link in the creation service;
- observation/session work requires blank occurrence key;
- `required` requires `resolved_at IS NULL` and blank `resolution_reason`;
- `complete` requires `resolved_at IS NOT NULL` and reason `succeeded` or
  `no_signal`;
- `no_op` requires `resolved_at IS NOT NULL` and reason `no_input`.

Indexes:

- `(organization, project, disposition)`;
- `(organization, project, work_type, disposition)`;
- `(organization, project, subject_type, subject_id)`;
- `(organization, project, work_type, occurrence_key)`.

`clean()` validates project/organization and team/organization. The creation
service additionally resolves the typed subject by both organization and
project, derives its team, rejects an unsupported work/subject pair, and treats
an existing identity with a different team or semantic identity projection as
a scope/collision error rather than a second work item.

Digest occurrence is the exception to proposed-snapshot equality: the first
transaction for a stable closed window freezes the authoritative snapshot.
Later execution of the same occurrence key reuses it without mutation even if
the newly proposed source selection differs. It logs the already-frozen
decision. CP1 does not claim complete carry-forward for a transaction whose
in-window timestamp commits after the occurrence froze; that coverage gap is
recorded for CP4 writer convergence/coverage rather than hidden by mutating the
snapshot.

`input_snapshot`, identity fields, scope fields, and team are immutable after
insert through domain services. A terminal disposition cannot return to
`required`. Checkpoint 1 does not add a database trigger; workers recompute and
compare fingerprints before execution so drift fails closed rather than
silently changing input.

### WorkflowRun Link

Add nullable `WorkflowRun.work`:

- FK to `WorkflowWork` with `PROTECT` and related name `attempts`;
- no default/backfill;
- existing rows remain valid and unlinked;
- every new linked run must match work organization, project, derived team, and
  work type;
- `WorkflowRun` remains append-only attempt/history and keeps timestamps,
  failure, provider call ids, result memory, and rerun lineage.

An unlinked historical run may be read/exported. Its console rerun is rejected
before mutation with `409 legacy_work_unlinked` until the historical repair
checkpoint can establish an exact work identity. CP1 must not fabricate a
session generation from today's state and pretend it was the historical input.

### Session Sequence Fields

The expand schema adds:

```text
AgentSession.observation_sequence_cursor
  PositiveBigIntegerField(null=True) during expand

AgentSession.end_work_contract_version
  PositiveSmallIntegerField(default=0, db_default=0)

Observation.session_sequence
  PositiveBigIntegerField(null=True, blank=True) during expand
```

During expand, add:

- a conditional unique constraint on `(session, session_sequence)` when the
  sequence is non-null;
- a check that a present sequence is greater than zero;
- a check that `end_work_contract_version` is 0 or 1;
- an index on `(organization, project, status, end_work_contract_version)`;
- an index on `(organization, project, session, session_sequence)`.

Existing sessions receive null cursor during expand. Every new session creator
explicitly writes cursor zero. The final contract makes the cursor non-null
with default zero and makes `Observation.session_sequence` non-null. Client
sequence numbers, occurrence time, prompt number, creation time alone, and UUID
lexical order are never authoritative live ordering inputs.

The persistent database default on `end_work_contract_version` is a rolling-
compatibility contract, not a substitute for explicit new-writer behavior.
An old application writer that omits the unknown column must still insert the
conservative legacy value 0. Explicit null remains invalid, and the default
must remain present until every writer that can omit the column is demonstrably
retired. Value 0 never fabricates a version 1 end transition.

`end_work_contract_version=1` means the session's current `ENDED` state was
committed by the CP1 active-to-ended primitive together with its work/no-op.
Legacy and import-materialized ended state remains 0 and is excluded from the
exact post-cutover session P2 cohort. Reactivation resets the marker to 0; the
next CP1 end sets it to 1 atomically. Import raw evidence still participates in
typed P1, but synchronously promoted imported state is not fabricated into an
async session transition; historical convergence remains CP10.

## Canonical Identity

Canonical JSON is UTF-8 JSON with sorted keys, no insignificant whitespace,
Unicode preserved, UUIDs rendered lowercase with hyphens, and aware timestamps
normalized to UTC and rendered as `YYYY-MM-DDTHH:MM:SS[.ffffff]Z`. Fractional
seconds use exactly six digits when microseconds are non-zero and are omitted
when they are zero; naive timestamps and unsupported/non-canonical values fail
before a write.

The accepted value domain is JSON null, booleans, signed 64-bit integers,
Unicode strings, UUIDs, aware timestamps, lists, and dictionaries with string
keys. Floats, `Decimal`, bytes, sets, tuples, non-string dictionary keys, and
integers outside the signed 64-bit range fail before a write. Producers build
JSON-shaped lists explicitly rather than relying on Python-only coercions.

`input_fingerprint` is SHA-256 of canonical JSON over the type-specific
identity projection, not provenance-only snapshot fields:

```json
{
  "contract_version": 1,
  "identity_input": {},
  "occurrence_key": "",
  "subject_id": "<uuid>",
  "subject_type": "<type>",
  "work_type": "<type>"
}
```

For observation work, `identity_input` contains observation id/content digest
and the boolean policy decision that required work. It deliberately excludes
`legacy_policy_fallback`, which remains immutable provenance in the stored
snapshot and RawEvent. If normal and legacy raw sources converge on the same
observation and semantic policy, they reuse the first work snapshot rather
than creating two logical rows. Session and digest identity projections contain
their complete semantic watermark/frozen-input material.

Non-digest creation uses `get_or_create` under the full fingerprint uniqueness
constraint. Digest creation first serializes the conditional occurrence
identity; on an occurrence race it reloads the winning frozen snapshot rather
than creating another row. Other integrity races reload by complete scoped
identity and verify exact semantic projection/team equality. Provenance-only
differences preserve the first snapshot without mutation. A semantic hash
collision or scope mismatch is fatal and emits no task.

### Persisted Observation Content Digest

Do not trust client `content_hash` as the frozen-input digest. Compute a
server-side content digest from persisted redacted observation fields in this
exact order:

```text
observation UUID
observation_type
title
subtitle
body
facts
narrative
concepts
files_read
files_modified
source_metadata
```

Each field is canonicalized, encoded, prefixed by its unsigned 64-bit
big-endian byte length, and streamed into SHA-256. `session_sequence` is
deliberately absent:
C1.3 renumbers provisional C1.2 sequences and must not invalidate already
created observation work. Accepted observation input fields are append-only
through normal domain services.

## Work Input Snapshots

### Observation Processing

```json
{
  "schema": "observation_processing_input/v1",
  "observation_id": "<uuid>",
  "observation_digest": "<sha256>",
  "policy": {
    "schema": "hook_work_policy/v1",
    "realtime_candidates_enabled": true,
    "legacy_policy_fallback": false
  }
}
```

The subject is the observation. Work exists only when the captured policy
requires realtime processing and the normalized observation is not a lifecycle
type.

### Session Distillation

Every persisted observation, including lifecycle rows, receives a server
sequence. Useful input excludes `session_start` and `session_end`.

Lifecycle classification uses the trusted persisted adapter event type, never
the client-provided `observation.type`. Hook/import normalization stores that
trusted type in `Observation.source_metadata`; reuse of an observation whose
trusted lifecycle/non-lifecycle class or canonical redacted content differs is
rejected as a client content-hash collision. Session snapshot queries use the
trusted classification.

On session end, `upper_sequence_inclusive` is the maximum sequence among useful
observations, not the session cursor. This makes lifecycle-only re-endings reuse
the same semantic generation. It is zero when the session has never had useful
input.

```json
{
  "schema": "session_distillation_input/v1",
  "session_id": "<uuid>",
  "lower_sequence_exclusive": 0,
  "upper_sequence_inclusive": 37
}
```

Exact input is same-scope, non-lifecycle observations with
`0 < session_sequence <= upper_sequence_inclusive`, ordered by sequence. Gaps
from lifecycle observations are valid. The server sequence and upper bound are
the CP1 watermark: they reconstruct the exact prefix without scanning or
hashing all session content inside the end transaction. Session work is not
enabled until the C1.3 sequence contract, so the identity never depends on
provisional or later-renumbered values.

A worker reloads only the frozen scoped prefix. Observation fields remain
append-only through domain services. CP3 extends this same work identity with
content/stage hashes, deterministic chunks, and durable per-observation
coverage; CP1 does not pre-implement those guarantees.

### Daily Digest

The subject is the project and team is null. Freeze a UTC schedule bucket and
ordered exact memory versions:

```json
{
  "schema": "daily_digest_input/v1",
  "project_id": "<uuid>",
  "schedule_key": "daily:YYYY-MM-DD",
  "window_start": "<UTC Z>",
  "window_end": "<UTC Z>",
  "visibility_policy": "digest_visibility/v1",
  "allowed_team_ids": [],
  "output_visibility_scope": "project",
  "output_team_id": null,
  "eligible_source_count": 240,
  "max_sources": 200,
  "sources_truncated": true,
  "sources": [
    {"render_position": 0, "memory_id": "<uuid>",
     "memory_version_id": "<uuid>", "version": 1,
     "content_hash": "<legacy/source hash>",
     "server_body_digest": "<sha256>",
     "visibility_scope": "project", "team_id": null,
     "source_title": "<redacted frozen title>"}
  ],
  "input_digest": "<sha256>"
}
```

Authorization filtering, selection, cap, and truncation happen before work
creation. Daily work uses the project-output policy below, then preserves the
current ordering behavior over that authorized set: choose at most
`max_sources` by `(-updated_at, id)`, freeze the redacted title/exact current
version plus source visibility/team, then assign `render_position` by
`(source_title, memory_id)`. `eligible_source_count` counts only authorized
sources. The worker renders that frozen order using exact `MemoryVersion.body`,
never mutable current `Memory.title`/`Memory.body`, and never reselects, resorts,
or retruncates. Empty authorized sources create terminal `no_op/no_input` work
and no package signal.

Daily and weekly planners exclude every `kind=digest` memory before eligibility,
counting, classification, cap, or freeze. CP1 never nests derived digest output
inside another digest, so legacy content cannot be laundered into a proven v1
output. A future recursive digest needs a separately reviewed provenance rule.

### Weekly Digest

The subject is the project for project-wide work or the selected team for a
team-scoped console occurrence. Freeze the UTC week bucket plus the
already-classified change references needed to render the digest:

```json
{
  "schema": "weekly_digest_input/v1",
  "project_id": "<uuid>",
  "team_id": null,
  "schedule_key": "weekly:YYYY-Www",
  "window_start": "<UTC Z>",
  "window_end": "<UTC Z>",
  "visibility_policy": "digest_visibility/v1",
  "allowed_team_ids": [],
  "output_visibility_scope": "project",
  "output_team_id": null,
  "changes": [
    {"bucket": "added|refuted|retired|superseded|merged",
     "memory_id": "<uuid>", "memory_version_id": "<uuid>",
     "version": 1, "content_hash": "<legacy/source hash>",
     "server_body_digest": "<sha256>",
     "visibility_scope": "project", "team_id": null,
     "source_title": "<redacted frozen title>",
     "transition_ref": "<stable id>", "occurred_at": "<UTC Z>"}
  ],
  "input_digest": "<sha256>"
}
```

Authorization filtering happens before weekly classification. Authorized
changes are ordered by bucket, occurrence time, memory UUID, and transition
reference. A transition reference is the persisted `MemoryLink`/`AuditEvent`
id when one exists; otherwise it is a deterministic hash of the frozen bucket,
memory/version id, and occurrence time. Workers render the frozen title and
load exact authorized `MemoryVersion` rows rather than reclassifying mutable
current memories. Empty authorized changes create terminal `no_op/no_input`
work and no signal.

### Digest Visibility Authority

A reusable digest is a publishable derived memory, not an implicit privileged
report. Source authorization is therefore derived from the output visibility,
not from the incidental breadth of the requesting user or scheduler process.
The frozen `digest_visibility/v1` policy has exactly two forms:

- project work freezes `allowed_team_ids=[]`, admits only
  `visibility_scope=project` sources in the resolved organization/project, and
  creates a project-visible output with null team;
- team work requires the selected team in the request's effective scope and a
  same-organization `ProjectTeam` link, freezes that one id in
  `allowed_team_ids`, admits project-visible sources plus team-visible sources
  for exactly that team, and creates a team-visible output bound to that team.

Session- and organization-visible sources are outside both policies. A
project-wide occurrence never means all-team privilege. If a future all-team
digest is required, it needs a separate explicit capability/principal and an
output visibility contract that cannot expose team-private input; CP1 does not
invent that surface.

Every frozen source/change records its current `visibility_scope` and `team_id`.
Immediately before provider access, one short pre-call transaction locks the
selected source `Memory` and exact `MemoryVersion` rows in deterministic UUID
order plus the selected `ProjectTeam` row for team work, revalidates the frozen
policy, and commits. Completion of that final locked revalidation is the
provider-authorization linearization point: a mutation committed before it
prevents the call; a mutation ordered after it is in-flight and may not make the
eventual output publishable.

After the provider returns, one short output transaction takes the same locks,
revalidates the same policy, and creates/reuses the exact output before releasing
them. No database lock is held across the provider call. A mutation committed
after the pre-call point but before the output lock discards the provider result
without publication; a concurrent mutation after output-lock acquisition
serializes after the valid output commit. Provider calls use null team for
project work and the selected team for team work.

### Legacy Digest Read Containment

A `kind=digest` memory is injectable or reusable only when its server-authored
metadata contains `digest_visibility/v1`, `workflow_work_id`, `input_digest`,
`output_identity`, allowed teams, and output visibility/team that match:

- a same-organization/project completed daily/weekly `WorkflowWork`;
- that work's immutable input digest and visibility snapshot;
- the recomputed output identity; and
- the `Memory` and `RetrievalDocument` visibility/team fields.

Missing, malformed, legacy, or inconsistent linkage is quarantined as
`digest_visibility_unproven`. One shared read predicate excludes such content
before digest source selection, search, ranking, packing, provider input,
context-bundle creation, or curation. Stored-bundle replay rechecks every persisted item; if
any digest is unproven, it rejects the whole replay with stable code
`context_bundle_digest_visibility_unproven` and returns no rendered context or
items. It never returns the persisted `rendered_text` and never tries to filter
and dynamically repack an immutable bundle. Search debug reports the exclusion
reason with title/content withheld and never places the document in any
candidate or packed set.

`ReembedMissingEmbeddings` applies the same proof predicate before provider
access. A proven v1 digest is eligible after publication commits; an unproven
digest is skipped with zero provider call and zero row mutation. The scheduler
does not turn a quarantined legacy document into a trusted projection.

`WeeklyDigestView`, including `weeks_back>0`, returns the existing built-false
empty shape with null digest id for an unproven legacy digest and never returns
its changes or changelog. CP1 does not delete, rewrite, backfill, re-index, or
mark those rows healthy. Ordinary `memories:read`, `memories:review`,
`context:read`, and `audit:read` surfaces must not expose their title, body,
versions, retrieval text, metadata-derived changes, or contaminated bundle
rendering. This includes inspection list/count/detail/related results, memory
version/diff, approved-only export, review queue, audit title resolution, and
workflow result-memory titles. Those surfaces filter the item, return not-found,
or use a content-withheld placeholder without changing their existing
authorization-safe shape.

No product capability, including `memories:admin`, bypasses body-bearing
quarantine in CP1: effective capabilities flatten organization/project/team
grant provenance and cannot prove access to every team that may have contributed
to a legacy project digest. Product APIs may expose only a content-withheld
quarantine marker/id where their existing shape requires it. Same-organization
team/project/admin negative tests prove no capability-union bypass. The durable
rows remain available for explicitly authorized offline operations and future
CP10 remediation. Read suppression is authorization, not historical repair.

### Digest Input And Output Identity

Do not trust the existing free-form `MemoryVersion.content_hash` as the body
binding. For every frozen version compute `server_body_digest` as SHA-256 over
canonical `(memory_version_id, version, body)` bytes. Both digest forms include
`visibility_policy`, sorted `allowed_team_ids`, output visibility/team, and each
source visibility/team in canonical digest material. Daily `input_digest` also
includes its schema/project/window/cap/truncation fields and the ordered source
tuples `(render_position, memory_id, version_id, version, server_body_digest,
visibility_scope, team_id, source_title)`. Weekly uses the corresponding
window/team and ordered change tuples `(bucket, memory_id, version_id, version,
server_body_digest, visibility_scope, team_id, source_title, transition_ref,
occurred_at)`.

Workers reload exact authorized versions, revalidate their source/output policy,
recompute every server body digest, and fail closed before rendering/provider
access on mismatch. Digest output identity is SHA-256 over `digest-output/v1`,
work type, project/team, and the
`WorkflowWork.input_snapshot['input_digest']`; it is not the legacy
memory-id-only or window-only hash. A previously generated digest is reusable
only when its metadata records the same work id, input digest, output identity,
visibility policy, allowed teams, visibility scope, and team, and the same
output transaction makes that work `complete/succeeded`. Legacy output or a
same-memory newer-version snapshot without that exact match creates new output;
it is never accepted as completion for the work or authorized for retrieval.

Inside the locked publication transaction, create the exact
`RetrievalDocument` with `IndexMemoryVersionInput(..., defer_embedding=True)` or
an equivalent deterministic row-only path. No embedding/model provider may run
under source, team, work, or output locks. The existing scheduled
`ReembedMissingEmbeddings` projection fills missing embeddings for proven
digests after commit; its delay or failure does not weaken exact/lexical
authorization or roll back the published digest.

## Server Sequence And Transaction Order

Every runtime observation writer uses this order:

1. resolve organization/project/team and the subject session;
2. enter `transaction.atomic()`;
3. lock the scoped `AgentSession` with `select_for_update(of=('self',))`;
4. recheck raw/observation duplication while holding the session lock;
5. for a genuinely new normalized observation, use the locked cursor or, for
   a legacy null cursor, the maximum existing non-null server sequence (zero
   when absent), increment once, persist the cursor, and write the same value
   to `Observation.session_sequence`;
6. for a reused observation, retain its sequence and do not advance the cursor;
7. write the exactly-one `ObservationSource` disposition;
8. create/reuse required work and call its versioned outbox task inside this
   transaction;
9. return acceptance only after commit.

Concurrent creation of the same session uses an inner savepoint around insert;
after a uniqueness race, the transaction reloads and locks the winning scoped
session. A non-null existing session team cannot be changed by later event
selection. A legacy null team may be adopted once under lock; a different
non-null derived team fails before evidence/work writes.

The import path already owns a surrounding transaction. Before creating
imported observations it locks all touched sessions in sorted UUID order, then
uses the same allocator. This avoids multi-session import/hook lock inversion.
Context-bundle session resolution also locks an existing session before any
team change: it may adopt a legacy null team once, but a different non-null team
fails closed. It creates no observation work.

## Policy Snapshot And Duplicate Repair

Every new C1.2 hook/import raw envelope uses the typed version 1 normalization
fields. New hook evidence also stores
`RawEventEnvelope.metadata.work_policy_v1`:

```json
{
  "schema": "hook_work_policy/v1",
  "realtime_candidates_enabled": true,
  "legacy_policy_fallback": false
}
```

This policy is captured once inside the evidence transaction. Later settings
changes never reinterpret the accepted event.

A duplicate request:

- resolves and locks the existing event's scoped session;
- ensures its valid `ObservationSource` disposition exists;
- reads the persisted policy and recreates missing required work;
- if legacy evidence lacks the policy, reads the current scoped setting once,
  persists `work_policy_v1` with `legacy_policy_fallback=true`, and then uses it;
- never adds typed `normalization_contract_version=1` to legacy evidence merely
  because its missing work was repaired;
- never creates a second raw event, observation, sequence, or logical identity.

Only the transaction that first creates required work emits its initial package
signal. A second duplicate reuses the existing work and emits nothing, even if
the work remains `required`; CP1 neither reads package tables nor guesses from
their absence. Existing Celery retries and the migrated bounded distillation
retry producer remain compatible, while general required-work reconciliation
belongs to CP2. Terminal work emits no automatic signal.

This is the executable F5 boundary: historical evidence without work is
repaired by atomically creating both work and its initial signal; the mandated
two-submit test still has one package row. CP1 cannot itself produce
work-without-an-initial-signal because those writes are atomic. After relay,
absence of a current package for still-required work is normal transport
history and is reconciled in CP2. Re-signaling on every duplicate would create
request-frequency-dependent package rows and contradict the exact F5 test.

## Work Creation Primitive

One domain primitive accepts a resolved typed subject and canonical snapshot,
derives scope/team, computes the fingerprint, and returns `(work, created)`.
It never imports task functions and never reads package tables.

The producer owns the transaction and task selection:

- required work calls the matching versioned task `.delay()` or
  `.apply_async()` only when `created` is true and before leaving the
  transaction;
- no-op work creates no package row;
- any work or package creation exception rolls the entire producer transaction
  back;
- broker availability is irrelevant to request commit because package creation
  is a PostgreSQL write;
- producers never infer completion or need from package row presence/absence.

## Versioned Task Boundary

Do not reinterpret queued legacy task arguments during a rolling deploy. Add
versioned task names whose only domain arguments are `work_id` and optional
`workflow_run_id`:

```text
engram.memory.process_observation_work_v1
engram.memory.distill_session_work_v1
engram.memory.generate_daily_digest_work_v1
engram.memory.generate_weekly_digest_work_v1
```

Each task:

1. parses stable UUIDs;
2. loads `WorkflowWork` by id and verifies expected work type;
3. reloads the typed subject/input using work organization/project/team;
4. recomputes the fingerprint and any digest defined by that work schema;
5. validates an optional queued `WorkflowRun` belongs to the same work/scope;
6. executes the existing domain workflow;
7. links the run only when the existing session/digest workflow already
   records one or an explicit queued run was supplied, then makes the one-way
   terminal transition only after success: `complete/succeeded` when it
   produced or reused output, `complete/no_signal` when execution intentionally
   produced no semantic output.

Observation processing does not create a new `WorkflowRun` lifecycle in CP1;
its work disposition plus existing candidate/provider/audit provenance is the
available evidence. Uniform automatic attempt/claim lifecycle belongs to C2.1.

For current session distillation, `DistillSessionResult.truncated=true` is not
complete coverage. The attempt may record its partial result, but the logical
work remains `required`; CP1 must not turn the existing max-chunk truncation
into a false terminal success. CP3 owns continuation and coverage.

Automatic delivery of terminal work returns idempotently. An explicit rerun
with a valid new queued run executes against the same completed work and does
not create a new generation. Existing legacy task functions remain registered
until pre-cutover packages drain; new producers never call them.

Use deterministic Celery task ids for the one allowed signal per logical
action:

```text
automatic initial signal: workflow-work:<work UUID>
explicit attempt signal:  workflow-work:<work UUID>:run:<run UUID>
```

The outbox package does not enforce task-id uniqueness. Correctness comes from
serializing create-once work/run transitions, not from package constraints.

CP1 does not generally make semantic output and the terminal work update one
atomic stage. Observation/session crashes between those writes leave work
`required` and may replay the existing idempotent workflow. Digest publication
is the narrow exception: its output/version/retrieval representation, exact
authorization linkage, and `complete/succeeded` transition commit in the one
post-provider transaction because an unlinked output cannot be read safely.
This is digest authorization/publication atomicity, not observation/session
stage coverage, candidate promotion, lineage transition, or a general CP3/CP4
atomic-semantic claim.

Checkpoint 1 retains existing bounded Celery attempt retries. It does not add
logical retry scheduling, leases, or reconciliation.

## Producer Matrix

| Producer/case | Work identity and snapshot | Transaction owner | Package signal | Terminal/no-op behavior |
|---|---|---|---|---|
| New non-lifecycle hook observation, realtime enabled | observation subject + persisted digest/policy | hook ingest | versioned observation-work task inside evidence transaction | worker completes/no-signal through existing domain result |
| New non-lifecycle hook observation, realtime disabled | no observation work; evidence stores policy and session sequence | hook ingest | none | input remains for later session generation |
| `session_start` or `session_end` observation | no observation-processing work | hook ingest | none for observation itself | lifecycle row still gets sequence/source disposition |
| Explicit active-to-ended session with useful input | session subject + max useful server sequence | hook ingest | initial versioned session-work task only when work is new | required until an untruncated worker result completes |
| Explicit active-to-ended session with no useful input | session generation with upper 0 | hook ingest | none | `no_op/no_input` |
| Idle active-to-ended session with useful/no input | same session-generation identity as explicit end | per-session sweep transaction | initial signal only for newly created required work | no-input work is terminal |
| Late useful event after end | observation work per captured policy; next end uses larger useful upper | hook ingest, then later end transaction | observation signal now if required; session signal at next end | old success cannot satisfy new fingerprint |
| Lifecycle-only reactivation/re-end | same max-useful server upper as prior generation | hook/end transaction | none when the same work already exists | completed/no-op/required generation is reused |
| New-format duplicate | existing evidence/policy/sequence and same fingerprint | hook ingest | none when work already exists | no duplicate logical identity |
| Legacy duplicate without policy | same evidence plus persisted one-time current policy fallback | hook ingest | initial signal only if repair creates required work | snapshot records `legacy_policy_fallback=true` |
| Manual rerun with linked work | same work/fingerprint + new queued run | console rerun transaction | matching versioned task with work id + run id | executes explicit attempt even if work complete |
| Manual rerun with unlinked historical run | no exact work identity | console view | none | `409 legacy_work_unlinked`, no writes |
| New manual daily-digest request | same deterministic window/exact-source identity as daily work + new queued run | project-digest request transaction | one explicit task with work id + run id; it is also the initial signal when work is new | repeated API requests reuse logical work but create distinct attempts |
| Current/team weekly digest GET | project/team occurrence + frozen exact changes | scoped work-creation transaction | initial automatic signal only when work is new | exact completed output returns built true; pending/new or empty returns existing built-false shape |
| Daily schedule window | project + UTC bucket + ordered exact version refs | per-project scheduler transaction | daily-work task when sources nonempty | empty window is `no_op/no_input` |
| Weekly schedule window | project + UTC week + ordered classified change refs | per-project scheduler transaction | weekly-work task when changes nonempty | empty window is `no_op/no_input` |
| Daily management command | same UTC-bucket producer as scheduled daily; override affects frozen window/sources | per-project command transaction | initial signal only when required work is new | converges with the same scheduled bucket/input |
| Failed-distillation compatibility retry | existing linked required session work whose latest run failed + new queued run | reconciler per-work transaction | versioned session-work task with work id + run id | preserves current bounded failed-run retry only; truncated success stays visibly required for CP3 rather than blind replay |

## Manual Attempt Transactions

`ProjectDigestRunView` delegates to one domain transaction. It locks and
re-resolves the project through `request.effective_scope.project_ids` inside the
active organization before any source/output/work read, preserves the existing
one-active-daily-run guard, freezes the manual project-output visibility policy
and authorized snapshot, creates the work, creates a linked queued run for
required or complete non-no-op work, creates the composite-id package, writes
the audit event, and then commits before returning. The caller's broader team
membership never widens a shareable project digest. Empty authorized input records
`no_op/no_input`, emits no package, and preserves the current non-enqueued
response. An active-run conflict creates no work, run, package, or audit.
Request UUID/request id belongs to `WorkflowRun` and audit only; it is never
part of `WorkflowWork` fingerprint. Identical immutable digest input therefore
reuses one logical work while each accepted manual request remains a distinct
explicit attempt.

Workflow read/rerun also uses `request.effective_scope` in its first query:
project must be allowed, and a non-null run/work team must be in allowed team
ids. Rerun then locks that source row, requires a terminal run with non-null
`work`, locks and validates the same scoped work, creates one linked queued run
with `rerun_of` and the immutable work snapshot, creates its composite-id
package, writes `WorkflowRunReran`, and commits. An organization-local but
unauthorized source is indistinguishable from not found and produces no further
read or mutation. Any authorized unlinked historical run returns
`409 legacy_work_unlinked` with no mutation, signal, or success audit.
Workflow detail serializes an unproven digest result id/status only and replaces
its title with the stable content-withheld placeholder; effective-scope access
to the run never bypasses digest quarantine.

`WeeklyDigestView` becomes enqueue/read-through and no longer calls the mutable
legacy builder synchronously. Before current or historical lookup it requires
the project in `request.effective_scope.project_ids`; a selected team must also
be in `request.effective_scope.team_ids` and have a same-organization
`ProjectTeam` link. For the current window it freezes/creates occurrence work
plus its initial package transactionally. A null team selects the project-output
policy, never all-team data; a selected team uses the team-output policy. It
returns `built=true` only for exact output already linked by matching
work/input/output/visibility metadata; new or pending work uses the existing
`built=false` response shape and a later poll observes completion. This timing
and visibility correction must be recorded in the C1.3c PR/release notes. Empty
authorized input is no-op/built false. Historical `weeks_back>0` remains
read-only, never fabricates work, and applies the same scope and legacy digest
read-containment gates.

`DigestReviewView` resolves its project/team through `request.effective_scope`
and requires exact proven digest linkage in its first memory query. An
organization-local unauthorized or unproven digest is not found and cannot be
marked ready/reviewed; it creates no mutation or audit event.

## Session End Primitive

Explicit end and idle sweep use one shared primitive while the session row is
locked. It:

- transitions only `ACTIVE -> ENDED` and records the server end time;
- sets `end_work_contract_version=1` in the same transaction;
- computes the max useful server sequence from same-scope rows;
- creates/reuses the exact session work;
- creates terminal no-input work without a task, or creates required work and
  its package row atomically;
- returns the work id/disposition for logging/tests.

A repeated end that did not perform an active-to-ended transition never creates
a new generation or automatic signal. Required-work redelivery remains the
responsibility of the existing bounded retry path and, generally, CP2
reconciliation.

## Schedule Buckets

Scheduler helpers accept an injected aware `as_of` for tests and normalize to
UTC. Daily uses one key per UTC calendar day at the configured schedule cut;
weekly uses one ISO week key at its configured Monday cut. Duplicate scheduler
execution in the same bucket converges on the same work identity.

Occurrence keys are canonical from work type plus exact UTC window boundaries
(and team when a future team-scoped digest exists), never a request UUID. A
manual request for the same effective window uses the same occurrence; a
different explicit window/override gets a different key. The first transaction
to insert the occurrence freezes the source snapshot used by all later
automatic or explicit attempts for that occurrence.

The scheduler first applies the project-output visibility policy and freezes
authorized stable version/change references, then creates work and its package
row in a per-project transaction. It does not pass the source list through
Celery. Project-policy selection excludes team-private rows; cross-project or
injected unauthorized references fail before work/provider access.
Weekly classification always uses a closed window; mutable changes after
`window_end` belong to a later bucket and never rewrite an existing snapshot.
The scheduled-weekly eligibility check derives from the frozen change set, not
from the current "recent approved memory" proxy, so refutation-, retirement-,
or lineage-only weeks are not silently skipped.

Before legacy task removal, the migration must account for every production
producer found by the callsite census: hook ingest, explicit end, idle sweep,
scheduled daily/weekly tasks, `ProjectDigestRunView`, workflow-run rerun,
`WeeklyDigestView`, `engram_run_daily_digest`, and
`RetryFailedDistillations`. A repository search
for legacy task `.delay()`/`.apply_async()` calls outside legacy task adapters
must return zero. Import creates observations and immediately promotes imported
content rather than dispatching observation processing; it adopts server
sequence/normalization metadata but does not fabricate observation or session
work for source-materialized ended history.

## Expand, Backfill, Contract Rollout

### C1.1 Expand

- add migration `0032_workflowwork_sequence_expand.py` with `WorkflowWork`,
  nullable `WorkflowRun.work`, nullable session cursor/observation sequence,
  typed nullable raw-event normalization fields, and their
  uniqueness/check/index contracts;
- amend the already-published `0032_workflowwork_sequence_expand` field state so
  `AgentSession.end_work_contract_version` has both Python `default=0` and
  persistent `db_default=0`; fresh installations must never commit an
  incompatible no-default schema;
- add `0032b_agentsession_end_work_db_default.py`, with dependency tuple
  `('core', '0032_workflowwork_sequence_expand')`, as a state-neutral
  unconditional database repair:

  ```sql
  ALTER TABLE "core_agentsession"
  ALTER COLUMN "end_work_contract_version" SET DEFAULT 0;
  ```

  It repairs installations that already recorded the originally published
  `0032_workflowwork_sequence_expand`; it has no state operation and its
  reverse is intentionally a no-op because amended
  `0032_workflowwork_sequence_expand` owns the persistent default state;
- add canonical identity/subject validation tests and migration tests;
- prove both physical histories: an `0031` historical model can insert a new
  session after fresh amended `0032_workflowwork_sequence_expand`, and a
  simulated already-recorded old `0032_workflowwork_sequence_expand` with its
  physical default removed is repaired by
  `0032b_agentsession_end_work_db_default`; reverse to
  `0032_workflowwork_sequence_expand` and reapply
  `0032b_agentsession_end_work_db_default` must preserve the historical insert
  contract;
- assert the model exposes Python default 0 and database default 0, while an
  explicit null remains rejected;
- add versioned tasks but switch no producers;
- allow only an image containing the corrected
  `0032_workflowwork_sequence_expand` plus
  `0032b_agentsession_end_work_db_default` to execute migrations; an image
  containing the originally published broken
  `0032_workflowwork_sequence_expand` may never own migration execution;
- deploy without backfilling work or requiring non-null sequence. The rollout
  gate records the physical column default and exact old-writer insertion
  evidence before C1.2 begins.

### C1.2 Writer Cutover

- move hook ingest and import observation writers to the row-locked allocator;
- make hook, import, and context-bundle session creators explicitly initialize
  a new cursor to zero;
- make every newly created hook/import raw envelope write one version 1
  observation or explicit no-op normalization disposition in its source
  transaction;
- move new non-lifecycle hook observation work/package creation into the
  evidence transaction;
- remove hook `transaction.on_commit()` dispatch for the migrated path;
- leave session-generation emission on the legacy path until C1.3;
- deploy and drain every old writer before sequence normalization.

Sequence values written during this mixed nullable phase are provisional: no
session-generation identity consumes them yet.

### C1.3a Deterministic Backfill

`0033_backfill_observation_sequence.py` depends on
`0032b_agentsession_end_work_db_default.py`. Deploy it without the contract
migration.
It is non-atomic and uses idempotent per-session transactions, iterating session
ids in sorted order. The migration hard-codes a reviewed maximum of 10,000
observations per session, update batches of 500, a 5-second per-session lock
timeout, and a 60-second statement timeout. A preflight aborts before mutation
if any session exceeds the row cap.

1. lock the session row with the bounded timeout;
2. load only ids/current sequences ordered by `(created_at, id)` and skip an
   already normalized/cursor-consistent session;
3. set that session's existing sequences to null;
4. assign 1..N with 500-row `bulk_update` batches;
5. set the cursor to N;
6. commit that session.

Timeout/error rolls back only the current session; prior sessions remain
committed, and rerun skips them after verifying their deterministic order.
Record completed/skipped/failed counts and run these assertions before starting
C1.3b:

- every observation has a sequence;
- every sequence is positive and unique within its session;
- every session cursor equals its maximum sequence or zero.

If old writers are not demonstrably drained, the cap is exceeded, or a session
cannot finish inside the lock/statement budgets, stop before changing the
contract and design a separately reviewed large-session path.

### C1.3b Contract And Session Producers

Deploy `0034_memory_loop_input_contract.py` only after the C1.3a assertions and
the recorded revision/process/transaction drain proof. It marks remaining
all-null history/mixed-rollout rows as v0, makes normalization version non-null, installs
the final v0/v1 combination check, makes cursor/sequence non-null, gives cursor
default zero, and makes sequence positivity unconditional. Then switch
explicit/idle end and the existing bounded failed-distillation compatibility
producer to work/run ids. Session generation is enabled only after the
contract migration has applied.

### C1.3c Digest Work And Explicit Attempts

Add immutable daily/weekly work execution, then switch new manual digest
creation and new-format workflow rerun to work/run ids. Validate the explicit
attempt transaction and review gate before scheduled producers change.

### C1.3d Scheduled Producers And Final Census

Switch daily/weekly schedules and `engram_run_daily_digest` to work ids. Prove
the legacy task producer callsite census is empty, then observe newly accepted
traffic through at least one complete daily/weekly scheduler cycle. Do not
create historical missing work.

## Invariant Evolution

CP1 updates the invariant evaluator/tests without hiding historical gaps:

- P1 continues counting all legacy source-cardinality violations and separately
  proves zero violations for typed `normalization_contract_version=1`
  post-cutover evidence, including observation-link and explicit-no-op rules;
- P2 becomes exact for post-cutover policy-bearing hook evidence and the
  current ended `end_work_contract_version=1` cohort: every transition whose
  policy/input requires work has a same-scope matching work identity;
- package atomicity is proven by fault tests, not by treating retained package
  rows or queue depth as permanent domain evidence;
- P14 remains globally `missing_observability`, while focused CP1 negative
  source-to-sink tests must pass before merge.

Checkpoint 1 does not mark global historical P1/P2 healthy and does not mutate
old evidence merely to make a dashboard green.

## First RED And Fault Tests

### C1.1 Identity/schema

1. canonical key order produces the same fingerprint;
2. changed scope, subject, contract version, or semantic identity input
   changes/rejects identity, while fallback provenance alone does not;
3. concurrent create converges on one `WorkflowWork`;
4. different derived team for the same identity is rejected;
5. disposition constraints reject invalid terminal combinations;
6. work/subject pairs and project-digest subject equality are database checked;
7. new runs link only to matching scoped work;
8. expand migration is additive, legacy cursor is null, and existing
   `WorkflowRun` rows stay valid;
9. raw normalization constraints reject partial/unknown version 1
   dispositions while leaving legacy rows valid;
10. renumbering `session_sequence` does not change observation work
    fingerprint.

### C1.2 Hook/API

1. raw envelope, observation, source, required work, and package row are visible
   together inside the surrounding transaction;
2. forced rollback leaves none of those rows;
3. suppressing all post-commit process activity still leaves recoverable work
   and package intent after commit;
4. broker unavailability does not roll back committed database intent;
5. duplicate evidence missing work creates one logical identity and one initial
   signal; a second duplicate creates neither;
6. two concurrent duplicates leave one work identity;
7. realtime-disabled/lifecycle evidence creates no observation work;
8. later settings changes do not reinterpret captured policy;
9. legacy policy fallback is persisted once;
10. foreign project/team requests create no evidence, work, or package row;
11. hook/import writers assign server sequences and duplicate reuse does not
    increment the cursor;
12. hook/import raw envelopes carry a valid typed version 1 observation/no-op
    disposition, while legacy repair promotes only after exact relation proof;
13. client sequence/time values cannot influence server order;
14. normal and legacy-fallback raw sources for the same observation/policy
    converge on one work while preserving the first provenance snapshot.

### C1.3 Lifecycle/schedules

1. concurrent append versus end places the append inside the frozen generation
   or after it for a later generation, never neither;
2. concurrent end converges on one generation;
3. empty/lifecycle-only session creates `no_op/no_input` and no package row;
4. late useful input yields a larger useful upper/fingerprint and old success
   cannot satisfy it;
5. lifecycle-only re-end reuses the prior generation;
6. idle end commits session status, work, and package together;
7. worker generation N excludes observations accepted after N, and an existing
   max-chunk truncated result leaves work required for CP3 continuation;
8. backfill is deterministic/idempotent and the next append receives N+1;
9. linked manual rerun creates a new attempt against the same work;
10. unlinked historical rerun returns 409 without writes;
11. duplicate daily/weekly bucket execution converges on one work;
12. empty schedule buckets are terminal no-op with no package;
13. every digest source/version is same-scope and authorized by the frozen
    visibility policy; mutable titles/body changes do not change frozen input,
    and task payload is work id only;
14. same-organization/project unauthorized-team sources never enter project
    daily, project weekly, or another team's weekly snapshot/provider/output;
    scheduler, management-command, and view paths each have a negative control;
15. project output is derived only from project-visible sources, team output is
    bound to exactly its selected team; barriers prove mutation committed before
    the pre-call linearization point prevents provider access, mutation ordered
    after that point prevents publication, and output locks serialize commit;
16. all `kind=digest` rows are excluded before daily/weekly eligibility and
    classification, so legacy output cannot be nested and laundered into v1;
17. legacy/malformed digest linkage is excluded from fresh search/context,
    stored-bundle replay, curation, search debug, weekly current/history, and
    ordinary body-bearing read surfaces including workflow result titles; one
    unproven persisted item rejects replay with no rendered text/items;
18. read/review/context/audit and `memories:admin` product paths all withhold
    quarantined content; a same-project Team-A admin cannot expose possible
    Team-B source data through flat capability union;
19. same-organization unauthorized project/team requests cannot read/create
    manual digest, weekly current/history/review, or workflow list/rerun state;
    unproven review cannot mutate ready status or create an audit event;
20. same memory with a newer exact version creates a different input/output
    identity and never reuses legacy digest output;
21. new manual digest, weekly read-through, management command, workflow rerun,
    and bounded linked distillation retry all emit versioned id-only tasks
    atomically;
22. repository callsite census finds no production producer of legacy task
    signatures after C1.3d.

Digest publication tests also assert `defer_embedding=True` (or the equivalent
row-only path) and zero embedding-provider calls while publication locks are
held; the existing scheduled reembed path remains outside the transaction.

F1–F6 in `docs/reliability/memory-loop-fault-matrix.md` are mandatory CP1
fault contracts. Each distinct source-to-sink boundary has one foreign-scope
negative control; worker-kill variants do not repeat it mechanically. Focused
P14 digest controls additionally use same-organization/project unauthorized
teams and prove denial before selection, provider access, and output.

## Files And Ownership

### C1.1 schema/identity owner

- `apps/backend/engram/core/models.py`;
- `apps/backend/engram/core/migrations/0032_workflowwork_sequence_expand.py`;
- `apps/backend/engram/core/migrations/0032b_agentsession_end_work_db_default.py`;
- `apps/backend/engram/core/migrations_tests.py` and the focused model metadata
  test;
- `apps/backend/engram/memory/workflow_work.py` and adjacent tests;
- versioned task definitions and focused task tests;
- no hook, import, context, producer, task-routing, or public API change belongs
  to the rolling-default repair.

### C1.2 hook/API owner

- `apps/backend/engram/hooks/services.py`;
- `apps/backend/engram/hooks/hook_ingest_tests.py`;
- `apps/backend/engram/imports/services.py` and import tests;
- `apps/backend/engram/context/services.py` and focused session-creation tests;
- observation-work task adapter/tests;
- P1/P2 post-cutover evaluator/tests.

### C1.3a sequence backfill owner

- sequence backfill migration and migration tests only.

### C1.3b contract/lifecycle owner

- sequence contract migration and migration tests;
- `apps/backend/engram/memory/session_sweep.py` and tests;
- session-work task/distillation adapter/tests;
- `apps/backend/engram/memory/distillation_reconciler.py` and tests;
- P2 lifecycle evaluator/tests.

### C1.3c digest/explicit-attempt owner

- immutable digest services/task tests;
- central digest-visibility quarantine helper and tests;
- context/search/replay/curation digest authorization and tests;
- search-debug, inspection, memory-version/diff, approved export, memory-review,
  and audit-title quarantine consumers/tests;
- `apps/backend/engram/console/views/project_digest.py` and tests;
- `apps/backend/engram/console/views/digests.py` and tests;
- `apps/backend/engram/console/views/workflow_runs.py` and rerun tests.

### C1.3d scheduler/command owner

- digest scheduler/task tests;
- `apps/backend/engram/core/management/commands/engram_run_daily_digest.py`
  and tests;
- legacy-producer callsite contract test.

Shared files have one active writer. Tests/research/review may run in parallel
only when file ownership is disjoint.

## Verification Per Serial Slice

Each slice must record:

- a focused RED before implementation and GREEN after;
- focused Ruff check/format;
- migration apply/reverse/freshness and Django system checks in Compose;
- existing hook, import, task, session, digest, rerun, and invariant regressions
  affected by the slice;
- `git diff --check` and repository-quality hook;
- independent spec/code-quality review;
- Karpathy simplicity review;
- bounded organization/project/team source-to-sink review;
- Claude adversarial review against the committed slice range;
- draft PR CI names, URLs, conclusions, rollback, and residual risks.

The C1.1 rolling-default repair additionally records PostgreSQL physical-schema
proof that `core_agentsession.end_work_contract_version` is `NOT NULL` with
database default 0, plus the fresh-`0032_workflowwork_sequence_expand`,
simulated-published-`0032_workflowwork_sequence_expand`, exact reverse, and
`0032b_agentsession_end_work_db_default` reapply historical-writer tests.
Migration freshness alone is not proof of the physical default.

The merged code is not the C1.1 deployment gate. Before C1.2 begins, direct
PID-1 signaling, rollout-specific async quiescence, gated API cutover,
exact-image rehearsal, rollback, and continuous hook-ledger requirements in
`docs/superpowers/specs/2026-07-10-cp1-1-loss-aware-rollout.md` must also close.

## Rollback

- Before producer cutover, revert additive code/migrations normally.
- Reversing `0032b_agentsession_end_work_db_default` to amended
  `0032_workflowwork_sequence_expand` intentionally retains the database
  default; this matches amended `0032_workflowwork_sequence_expand` state and
  keeps old writers compatible. Reversing through
  `0032_workflowwork_sequence_expand` drops the entire expand schema. Do not
  drop only the database default while any old writer can omit the column.
- During nullable sequence rollout, old rows remain valid; revert writers while
  leaving additive columns/model present if required for safe rollback.
- After non-null contract, do not roll back to a writer that can insert null.
  Roll back behavior with a forward compatibility release or first relax the
  constraint in a reviewed migration.
- `WorkflowWork` rows created for post-cutover traffic are durable product
  history and are not deleted merely to roll back code.
- No rollback may delete raw evidence or reinterpret terminal semantic state.

## Acceptance Gate

Checkpoint 1 closes only when:

- C1.1, C1.2, C1.3a, C1.3b, C1.3c, and C1.3d have merged serially with their
  deploy gates recorded;
- fresh amended `0032_workflowwork_sequence_expand`, simulated already-applied
  published `0032_workflowwork_sequence_expand`, reverse to
  `0032_workflowwork_sequence_expand`, and
  `0032b_agentsession_end_work_db_default` reapply all preserve insertion by
  the historical `0031_workflowrun_active_daily_digest_unique` session model
  with stored `end_work_contract_version=0`; the deployed physical column
  default is exactly 0;
- all post-cutover accepted events have exact normalization disposition;
- every post-cutover required transition has one stable scoped work identity;
- evidence/lifecycle work and its one initial package signal commit or roll
  back together;
- duplicate and concurrent delivery create no duplicate logical identity;
- process death after commit leaves recoverable work;
- observation sequence is non-null, positive, unique, and cursor-consistent;
- late useful input creates a newer generation and lifecycle-only input does not;
- every producer carries stable work/run ids only, the legacy-producer census
  is empty, and legacy packages have drained;
- every digest freezes `digest_visibility/v1`; project output has only
  project-visible input, team output has only project plus its selected team,
  and no unauthorized source reaches provider or publication;
- all digest output is excluded from new digest input, and unproven legacy
  output is quarantined from weekly history, fresh retrieval/search/curation,
  debug, bundle replay, and ordinary body-bearing read surfaces without
  mutating the historical row; no flat capability/admin bypass exposes content;
- digest output/version/retrieval linkage and work completion commit atomically
  under the narrow publication exception; broader stage/promotion atomicity
  remains deferred;
- publication defers embedding under locks and relies on the existing scheduled
  reembed projection after commit;
- manual digest, weekly current/history/review, and workflow list/rerun queries
  narrow through `request.effective_scope` before reading/mutating scoped rows;
- P1/P2 post-cutover cohort and focused P14 tests pass;
- broker/package ownership remains entirely in `django-celery-outbox`;
- no CP2 leases, general required-work reconciliation, new retry policy, or
  historical repair leaked into CP1; migrating the existing bounded linked
  distillation retry is compatibility work only;
- focused local/container checks, all review gates, and checkpoint CI are green.

## Stop Conditions

Stop before implementation or the next serial slice if:

- logical identity, derived team, canonical snapshot, or cutover cohort is
  ambiguous;
- a producer cannot create work and package intent in its source transaction;
- a design needs package-row presence/absence to decide product completion;
- a rolling deployment can insert null/duplicate sequence after contract;
- an old session writer can observe the new non-null end-work marker without a
  physical database default, or an image containing the uncorrected published
  `0032_workflowwork_sequence_expand` can execute migrations;
- sequence backfill cannot be deterministic, idempotent, bounded, and resumed;
- manual rerun would fabricate a historical input generation;
- a digest snapshot would load mutable current state instead of exact versioned
  references;
- digest input authorization cannot be expressed as a stable output-bounded
  visibility policy or revalidated before provider/publication;
- the first schema change is not additive;
- required fixes expand into CP2 lease/retry/repair, public API, secrets,
  deployment mutation, or historical data repair without a new reviewed spec.
