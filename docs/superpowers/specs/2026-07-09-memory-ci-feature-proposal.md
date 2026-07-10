# Memory CI: Continuous Validation For Engineering Memory

Date: 2026-07-09

Status: detailed feature proposition; implementation is blocked on the core
reliability and autonomy checkpoints in
[Autonomous Memory Loop Reliability Roadmap](2026-07-09-autonomous-memory-loop-roadmap.md)

## One-Sentence Proposition

Memory CI continuously determines which engineering memories are still true
for the current codebase, automatically revises their versioned projection when
the world changes, and sends only genuine unresolved contradictions to a human.

## Why This Feature Exists

Semantic similarity answers:

> Which stored claims resemble this request or this change?

It cannot answer:

> Is this claim still true for the code, configuration, dependency set, and
> environment that exist now?

A memory can remain extremely similar to a query while being completely
obsolete. “The service uses Redis for rate limiting” will still rank highly
after the service moves to PostgreSQL advisory locks. Vector distance has no
concept of before, after, applicability, evidence precedence, or code
revision.

Memory CI makes validity causal rather than statistical:

- memories carry typed evidence anchors;
- repository and operational changes become durable world-state events;
- exact impact analysis identifies claims whose support may have changed;
- deterministic validators run first;
- models interpret bounded ambiguous evidence, not free-form similarity;
- every decision is tied to a repository state and preserved as history;
- retrieval filters for temporal eligibility before it ranks for relevance.

The result is not “RAG with a freshness score.” It is living, revision-aware
engineering knowledge.

## Product Promise

For a project with Memory CI enabled:

- a default-branch change automatically identifies impacted memories;
- unchanged evidence confirms memory without an unnecessary model call;
- clearly evolved behavior creates a new memory version;
- clearly obsolete behavior is superseded or refuted with evidence;
- prescriptive decisions are checked for code drift rather than silently
  rewritten as descriptions of the drift;
- uncertain work retries automatically;
- only mutually incompatible, contemporaneously supported claims create a
  human conflict;
- future agents receive memory valid for their repository state;
- operators can inspect what changed, why Engram changed its memory, and which
  source evidence justified the transition;
- an agent can eventually ask what Engram considered true at an earlier
  revision and see the memory delta between revisions.

## Feature Boundary

Memory CI is an Engram memory-maintenance feature. It is not a generic CI
runner, source-code analysis platform, pull-request bot framework, or agent
orchestrator.

The first version:

- follows one canonical repository and default branch per project;
- consumes trusted revision/change metadata;
- uses paths, symbols, configuration keys, dependencies, migrations, tests,
  and existing provenance;
- produces informational PR previews;
- never executes arbitrary repository code inside the Engram API or worker;
- does not block merges by default;
- does not create a permanent memory universe for every feature branch;
- does not require a generalized knowledge graph or whole-program AST engine.

## Why The Current Model Is Insufficient

Current Engram already has useful pieces:

- immutable raw events and observations;
- memory candidates, memories, and versions;
- file/symbol/exact-term extraction;
- links for files, commits, pull requests, and relationships;
- exact and semantic retrieval;
- stale/refuted flags;
- context audit;
- scheduled memory workflows.

The missing concept is applicability over a changing world state.

Today:

- a memory has coarse stale/refuted state but no validated repository revision;
- a memory version does not express where or when it applies;
- confidence is largely supplied by a model rather than derived from evidence;
- confidence decay uses elapsed time, not changes to supporting code;
- retrieval excludes coarse stale/refuted rows, then ranks without knowing
  whether the current repository head invalidated a claim;
- high similarity can be mistaken for duplicate/supersession evidence;
- context replay is not bound to the same repository state.

Memory CI should reuse the existing evidence, version, retrieval, and audit
domains rather than create a separate knowledge product.

## Core Mental Model

A memory is not just text.

A durable engineering memory is:

    claim
    + claim posture
    + applicability
    + supporting and contradicting evidence
    + immutable versions
    + validation assertions against world states
    + current authorized projection

### Claim Posture

Memory CI must distinguish at least three postures. Existing memory `kind` may
inform the classification, but posture answers a different question.

#### Descriptive

Claims what the system currently does.

Examples:

- this endpoint writes the outbox row after commit;
- this command regenerates the client bundle;
- this configuration key controls the timeout.

Current code, configuration, tests, and authoritative runtime evidence can
confirm or invalidate descriptive claims.

#### Prescriptive

States an intended rule, decision, invariant, or convention.

Examples:

- all tenant filters must be applied before ranking;
- use pnpm for JavaScript package changes;
- provider failures must not create semantic decisions.

Code drift does not automatically refute a prescriptive memory. It may reveal a
violation. The decision remains current until a newer authoritative decision
supersedes it or a genuine conflict is resolved.

#### Historical

Records a past incident, migration, result, or state.

Examples:

- a production outage on a named date was caused by a stale lease;
- version 1.4 used a local SQLite worker;
- a migration repaired 131 orphan sessions.

A historical memory remains true as history even though it must not be injected
as a current operational instruction.

This distinction prevents Memory CI from making two dangerous mistakes:

- rewriting a policy merely because implementation violated it;
- presenting a historically true statement as current behavior.

## Three Independent State Machines

Memory CI must not collapse transport, semantic truth, and validity into one
status field.

### 1. Operational Work

Conceptual lifecycle:

    ready -> leased -> complete
       ^        |
       |        +-> retry_wait
       +-------------+

Worker death, provider timeout, missing credentials, malformed output, and rate
limits belong here.

An operational failure changes no semantic truth. Logical work keeps a stable
identity and automatically resumes after its dependency recovers.

### 2. Knowledge Lineage

Conceptual outcomes:

- active;
- revised into a newer version;
- merged;
- superseded;
- refuted;
- conflict.

These express what happened to the claim, not whether a current worker is
available.

### 3. Validation State

Conceptual lifecycle for an active version:

    current
       |
       +-- relevant world-state change --> impacted
                                           |
                                           v
                                      revalidating
                                      /    |    \
                              current  unknown  lineage transition
                                         |      |
                                         +------+
                                           retry

“Unknown” means Engram cannot safely certify the claim for the target state
yet. It is not a human-review result. Retrieval policy treats it
conservatively while operational work retries.

The checkpoint spec should choose the smallest persistence representation that
can enforce these separations. The conceptual state names are not a mandated
schema.

## Canonical World State

The first implementation uses an ordered stream of accepted default-branch
repository revisions for each project.

Each canonical world-state event should identify:

- organization and project;
- repository identity;
- branch identity;
- base and head revision;
- event source and stable external identity;
- changed, added, removed, and renamed paths;
- available symbol/signature changes;
- dependency, configuration, migration, and test metadata;
- event time and receipt time;
- completeness and redaction state.

The event is evidence, not merely a scheduler hint. Its processing intent must
be committed through the reliable memory-work primitives.

### Revision Coverage Invariant

Accepting repository state R does not certify existing memory for R.

Each project needs an explainable coverage boundary that distinguishes:

- latest accepted canonical revision;
- latest revision with a complete impact plan;
- latest revision for which required revalidation is complete or actively
  represented by conservative pending state.

A context request may call code-sensitive memory current for R only after
impact processing covers R. Before then, the default policy withholds that
memory as unknown and emits a revision-coverage warning. An explicit historical
request may pin to the last fully processed revision, but the bundle must name
that older revision rather than imply it represents R.

This closes the interval between revision acceptance and impact planning. Queue
health or an old `current` label cannot bridge that interval.

### Pull-Request State

Pull requests and feature branches are provisional worlds.

Memory CI may calculate a preview:

- which memory would be impacted;
- which decision or convention the change may violate;
- which memory revision Engram would propose;
- which conflict may emerge.

Preview results do not mutate canonical shared memory. A merged default-branch
revision produces the authoritative transition.

### Deployment And Runtime State

Repository head is necessary but not always sufficient. A later version may
add deployment revisions, environments, rollbacks, and runtime observations.

Until then, claims that depend on deployment state must state that limitation
instead of pretending repository head proves production state.

### Evidence Acquisition Modes

Memory CI cannot revalidate code from a commit identifier alone. A project must
declare one trusted, read-only way to obtain the bounded evidence needed for
validation:

- a configured SCM adapter reads change metadata and selected before/after
  blobs or diffs;
- the project's trusted CI submits a signed or authenticated change artifact
  containing the revision, manifest, relevant snippets, and test results;
- a self-hosted repository mirror exposes the same read-only contract;
- agent hooks contribute observations, but cannot alone declare an unmerged
  branch to be canonical.

The first slice may operate in metadata-only shadow mode. It must label its
evidence completeness honestly and must not make destructive decisions that
require source it did not receive.

Repository credentials stay server-side and project-scoped. Adapters read only
configured repository identities and bounded paths; a submitted arbitrary URL
is not a source-fetch instruction. The exact SCM/CI wire contracts belong in
their focused checkpoint specs.

## Evidence Anchors

An evidence anchor says what part of the engineering world supports or limits a
claim.

### Target Anchor Types

- repository and revision;
- file path and optional content fingerprint;
- symbol or public interface;
- configuration key;
- environment variable name;
- dependency name and version/range;
- lockfile or manifest;
- database migration or schema object;
- command and expected outcome;
- test identity and result;
- error or incident signature;
- observation, session, provider call, commit, pull request, and issue;
- relationship to another memory version.

MCI-2 implements only exact file paths and existing symbols. The remaining
types are target vocabulary introduced one at a time after the minimal causal
loop proves their measured value.

### Anchor Provenance

Every anchor needs a reason:

- explicitly cited by an agent or user;
- deterministically extracted from evidence;
- derived from repository metadata;
- imported from an upstream artifact;
- suggested by a model and then corroborated;
- manually declared as a memory contract.

Model-suggested anchors may improve recall, but they must not independently
authorize destructive state changes.

### Anchor Quality

Anchor quality should be explicit:

- exact and resolvable;
- exact but currently missing;
- inferred and corroborated;
- inferred only;
- historical;
- truncated or redacted;
- unresolved.

This quality contributes to trust and revalidation priority. It is not hidden
inside one model-confidence number.

## The Minimal Impact Graph

Memory CI needs a typed invalidation index, not a general-purpose graph
database.

Conceptual nodes include:

- memory versions;
- observations and source evidence;
- repository revisions;
- files;
- symbols/interfaces;
- configuration keys;
- dependencies;
- tests and commands;
- environments.

Useful relationship meanings include:

- applies to;
- implemented by;
- constrained by;
- tested by;
- observed in;
- depends on;
- supersedes;
- refutes;
- conflicts with.

The implementation may store these relationships in ordinary PostgreSQL models
and indexed columns. A graph database is out of scope until measured product
needs prove otherwise.

## Change-To-Impact Pipeline

The pipeline is ordered deliberately.

### Stage 1 — Resolve Scope

Resolve organization, project, repository, branch, actor, and capability before
reading change content or creating work.

No global impact search followed by tenant filtering is allowed.

### Stage 2 — Record The World-State Event

Idempotently record the canonical revision event and normalized change
manifest.

Duplicate SCM delivery must reuse the same event. A newer event may supersede
pending work, but cannot erase the audit/history of earlier revisions.

### Stage 3 — Normalize Change Semantics

Process renames before deletions.

Classify:

- content edit;
- path rename or move;
- symbol/signature change;
- deletion;
- new file or symbol;
- dependency change;
- configuration change;
- migration/schema change;
- test result change.

### Stage 4 — Traverse Exact Anchors

Select memories directly connected to changed paths, symbols, keys,
dependencies, migrations, tests, and lineage.

Record the exact reasons each memory was selected.

### Stage 5 — Apply Typed Rules

Examples:

- a lockfile bump impacts dependency-version claims;
- a removed environment key impacts configuration instructions;
- a migration impacts data-model and rollback memories;
- a public signature change impacts calling conventions;
- an identical-content rename changes an anchor but not necessarily the claim.

### Stage 6 — Bounded Recall Expansion

Lexical and semantic retrieval may discover memories whose anchors are
incomplete.

This expansion is:

- tenant/project scoped before ranking;
- capped;
- reasoned;
- lower authority than an exact anchor;
- incapable of applying a state transition by itself.

### Stage 7 — Persist The Impact Plan

Create a revision-scoped, explainable plan containing:

- impacted memory version;
- selection reasons;
- anchor changes;
- priority/risk;
- required validators;
- whether semantic expansion was used.

### Stage 8 — Create Revalidation Work

Create one stable logical work identity per memory version and target world
state, then emit its id-only package-outbox task atomically.

Replaying the same revision or running two schedulers must not duplicate the
logical work or its semantic effect.

### Coalescing Newer Revisions

If newer default-branch revisions arrive faster than revalidation completes,
Engram may jump to the newest target.

It must:

- preserve receipt/audit of intermediate revisions;
- compute the complete accumulated change from last validated state to newest
  target;
- fence an old worker from applying a decision to a newer head;
- avoid repeatedly validating an obsolete intermediate state unless historical
  reporting requested it.

## Revalidation Pipeline

### Step 1 — Freeze The Evaluation Window

Capture:

- current memory version and posture;
- prior validated world state;
- target world state;
- applicability;
- supporting and contradicting evidence;
- anchor fingerprints;
- change reasons;
- policy/model versions.

A decision applies only to this frozen window.

### Step 2 — Run Deterministic Validators First

Examples:

- relevant content and symbol fingerprints unchanged: confirm;
- path renamed with identical content/symbol: update anchor and confirm;
- descriptive claim names a deleted symbol: strong invalidation signal;
- dependency remains inside declared applicability: confirm;
- configuration key removed: strong impact signal;
- linked test still directly proves the claim: confirm;
- prescriptive rule has an executable contract that still passes: confirm;
- prescriptive rule's contract fails: record code drift, do not refute the
  rule.

Most unchanged or mechanically evolved memory should settle here without an
LLM call.

If required before/after evidence or its attestation is missing, deterministic
validation cannot confirm or invalidate the claim. The result is
unknown/retry with conservative context treatment.

### Step 3 — Gather A Bounded Evidence Package

For ambiguous cases, gather only relevant evidence:

- exact claim and posture;
- original supporting evidence;
- before/after code or configuration slices;
- changed symbols and signatures;
- applicable test or command outcomes;
- dependency and environment context;
- deterministic validator results;
- related current memories;
- source precedence and redaction/truncation warnings.

Do not send the whole repository or unbounded transcript.

### Step 4 — Evidence-Aware Model Adjudication

The model interprets the package through a structured contract.

The model may recommend an outcome and explain evidence. It cannot establish
truth through self-confidence alone.

Provider outage, invalid structured output, missing embedding, or missing
secret produces operational retry. None is mapped to confirm, revise, refute,
merge, or conflict.

### Step 5 — Validate Decision Preconditions

Before applying:

- target world state is still current or deliberately historical;
- memory version has not changed;
- scope and applicability still match;
- required evidence threshold is satisfied;
- destructive decisions meet stronger evidence requirements;
- no concurrent decision already settled the same work.

### Step 6 — Apply An Atomic Outcome

Conceptual automatic outcomes:

- **confirm:** claim remains applicable; record validation evidence;
- **revise:** same durable subject changed; create a new current version;
- **narrow/broaden applicability:** claim is true under a more accurate
  environment, dependency, or subsystem boundary;
- **supersede:** a newer rule or behavior clearly replaces the old one;
- **refute:** direct evidence shows the old claim was wrong, not merely old;
- **merge:** combine equivalent knowledge and provenance;
- **reject candidate:** derived projection never represented durable knowledge;
- **conflict:** preserve incompatible supported claims for a human;
- **pending verification:** make no semantic change and retry automatically.

Every lineage-changing outcome preserves old versions and source evidence.

### Step 7 — Refresh Projections

Update exact retrieval state and temporal eligibility atomically with the
authoritative transition where feasible. Rebuild embeddings asynchronously.

A missing embedding degrades semantic recall; it does not make the authoritative
state inconsistent.

## Evidence Precedence

Precedence depends on claim posture.

### For Descriptive Claims

Typical order:

1. deterministic current code/config/schema;
2. current passing tests that directly exercise the claim;
3. authoritative deployment/runtime evidence for the applicable environment;
4. merged revision metadata and explicit source links;
5. corroborated recent observations;
6. uncorroborated observation;
7. model inference.

### For Prescriptive Claims

Typical order:

1. explicit current decision, policy, or accepted memory contract;
2. newer explicit decision that supersedes it;
3. current code/test conformance evidence;
4. observations;
5. model inference.

Implementation drift does not silently replace the rule. Memory CI reports the
drift and may create a code-change warning or conflict if a competing current
decision exists.

### For Historical Claims

Contemporaneous source evidence is authoritative. Later code change should
change retrieval intent, not rewrite history.

The focused implementation spec should make precedence configurable only where
real projects require it. Avoid a policy language in the first version.

## Validation Policy By Claim Shape

Wall-clock age may prioritize work, but it does not prove a claim false.

- **Anchored descriptive code/config facts:** revalidate when an exact anchor
  or its dependency closure changes.
- **Prescriptive decisions and conventions:** revalidate when their decision
  source changes or when code/test evidence indicates conformance drift.
- **Dependency/version claims:** revalidate on manifest, lockfile, or supported
  version-range changes.
- **Deployment/runtime claims:** revalidate only against an identified
  environment and authoritative deployment/runtime evidence.
- **Historical incidents and digests:** keep the historical assertion
  immutable; update only classification, links, or current-context eligibility.
- **Unanchored inference:** give it lower authority and a bounded validation
  lease while Memory CI tries to acquire exact anchors.

This replaces confidence decay as the correctness mechanism. Time-based sweeps
remain useful for finding unvalidated or poorly anchored memory, but their
automatic result is revalidation work, not a human review item and not
automatic falsification.

## Genuine Conflict Test

Before creating a human conflict, Memory CI must try these automatic
explanations in order:

1. claims apply to different repository revision intervals;
2. claims apply to different branches or provisional versus canonical state;
3. claims apply to different environments;
4. claims apply to different dependency versions;
5. claims apply to different projects, teams, or visibility scopes;
6. one claim is a more specific narrowing of the other;
7. one claim is historical and the other current;
8. one source clearly has stronger posture-appropriate evidence;
9. one claim is a superseding decision with clear temporal ordering;
10. one side is unsupported inference and should be rejected.

Only if the claims still apply to the same world state and remain materially
supported and mutually exclusive does the system create a human conflict.

## Context And Retrieval Behavior

The context pipeline becomes:

    resolve authorization
      -> determine requested repository state
      -> temporal/applicability eligibility
      -> exact, lexical, and semantic relevance
      -> deterministic packing
      -> immutable revision-bound bundle

### Context Eligibility

- **current and applicable:** normal authoritative context;
- **impacted/revalidating:** excluded from authoritative context by default;
- **unknown:** withheld for high-risk operational instructions; optional
  explicitly warned historical/supporting context for lower-risk use cases;
- **superseded/refuted/stale:** history and debugging only;
- **conflict:** neither side is injected as settled truth; inject a compact
  conflict warning when relevant;
- **historical:** eligible for incident/history intent, not current
  instructions;
- **prescriptive with implementation drift:** keep the rule visible and attach
  a conformance warning rather than rewriting it as current implementation.

These item-level rules apply only after the requested repository state is
covered. If the project has accepted a newer revision whose impact plan is not
complete, code-sensitive items are unknown regardless of their prior state.

### Request Fingerprint

A context request fingerprint should bind at least the behavior-relevant input,
authorization scope, retrieval policy, and requested repository world state.

Reusing a request id with another fingerprint is an idempotency conflict.
Replaying the same fingerprint returns the stored version/body/validity
snapshot byte-for-byte even if current memory later evolves.

### Memory Delta

When an agent moves from repository state A to B, Engram should be able to
return a compact delta:

- memories newly applicable;
- memories revised;
- memories superseded or refuted;
- new prescriptive violations;
- unresolved conflicts;
- high-impact memory still pending validation.

This is often more useful than injecting the entire stable context again.

## Industry-Leading Capability: Revision-Addressable Memory

The long-term differentiator is time-travel context.

An authorized agent can ask:

- what did Engram consider true at revision A;
- what changed in memory by revision B;
- which code/evidence change caused each transition;
- which current decisions existed but were violated by that code;
- which memory version was actually injected into a historical session.

This requires:

- immutable memory versions;
- revision-bound validation assertions;
- immutable context snapshots;
- append-only transition evidence;
- a rebuildable current projection.

It does not require copying the entire memory corpus per commit.

When a code revert occurs, Engram does not blindly resurrect deleted rows. It
evaluates the new world state and may revalidate a prior memory version as
applicable again, preserving both historical intervals and the new decision.

## Optional Advanced Capability: Memory Contracts

A high-value prescriptive memory may be associated with an executable or
deterministic validator.

Examples:

- every retrieval query includes organization and project scope before ranking;
- a configuration key must exist;
- a generated plugin bundle must match its canonical CLI source;
- a schema migration must preserve a named invariant;
- a command documented in memory still succeeds in a controlled test fixture.

Memory contracts turn durable decisions into continuously checked engineering
knowledge.

### Safety Rules

- model-generated validators start as proposals;
- validator proposals live in an optional setup workflow, not the semantic
  conflict inbox, and cannot create standing daily review work;
- arbitrary repository code is not executed in the Engram API/worker;
- validators run in the project's existing trusted CI or in a separately
  sandboxed integration;
- initial results are informational;
- merge blocking requires an explicitly authorized enforced contract;
- a failing contract means code drift from a prescription, not automatic
  refutation of the prescription;
- validator output becomes evidence for Memory CI.

This capability should follow the core temporal loop. It is not required for
the first Memory CI release.

## User Experience

### Pull Request Preview

An informational report shows:

- impacted memories and exact reasons;
- prescriptive decisions the change may violate;
- provisional confirm/revise/supersede proposals;
- potential conflicts;
- memories found semantically but lacking exact anchors;
- estimated model work and any incomplete checks.

No canonical memory mutation occurs before merge.

### Default-Branch Merge

The merge creates a canonical revision event. Memory CI:

1. records the change;
2. produces an impact plan;
3. schedules durable revalidation;
4. applies safe automatic outcomes;
5. refreshes context eligibility;
6. exposes conflicts only.

### Next Agent Session

Context can explain:

- validated for revision;
- applicability and posture;
- supporting evidence;
- why included;
- whether implementation drift or a related conflict exists;
- which memory delta occurred since the prior session.

### Memory Timeline

The console shows:

- original evidence;
- versions and applicability;
- validations by repository state;
- impact reasons;
- automatic decision explanations;
- model/provider provenance;
- supersede/refute/conflict history;
- contexts in which the version was injected.

### Project Health

Operators see:

- last accepted and last fully processed revision;
- impact-plan and revalidation lag;
- active/retry/expired work;
- anchor coverage;
- current/impacted/unknown/conflict counts;
- stale-memory injection metric;
- provider cost and fallback behavior;
- repair and replay status.

Operational health is separate from the semantic conflict inbox.

## Reliability And Idempotency

Memory CI inherits the core roadmap guarantees.

Required identities conceptually include:

- canonical revision event;
- normalized change manifest;
- revision-scoped impact plan;
- memory-version/target-state revalidation;
- deterministic validator invocation;
- provider adjudication stage;
- semantic transition application.

Rules:

- stable logical identities survive retries;
- attempt ids may vary;
- duplicate SCM delivery creates no duplicate event;
- duplicate task delivery creates no duplicate semantic effect;
- a stale worker cannot apply to a newer memory version or repository head;
- provider calls are recorded and de-duplicated in durable effect;
- provider failure changes no semantic state;
- incomplete work is discoverable from source invariants;
- repair is dry-run, scoped, idempotent, and resumable;
- raw evidence and append-only lineage are never deleted to make a repair pass.

## Request Scoping And Trust Boundary

The feature must prove:

- repository change identity resolves to one authorized organization/project;
- impact traversal begins inside that scope;
- semantic expansion searches only authorized project memory;
- source adapters cannot submit another project's revision;
- provider calls carry stable scoped ids, not credentials or unbounded source;
- context temporal filtering happens after authorization and before ranking;
- repair cannot cross project boundaries;
- provisional branch results cannot mutate canonical memory;
- UI/API surfaces expose only authorized evidence and source snippets.

The focused review should stay bounded to these source-to-sink paths.

## False-Positive Controls

- A touched file does not automatically make every linked memory stale.
- Rename detection happens before delete handling.
- Unchanged fingerprints confirm without model use.
- Semantic similarity can add impact candidates but cannot mutate them.
- Provisional branch results cannot change canonical memory.
- One weak negative signal moves a claim to pending validation, not refuted.
- Destructive transitions require direct evidence or corroboration.
- Prescriptive decisions are checked for violation rather than rewritten by
  implementation drift.
- Historical memories remain historical.
- Decisions are fenced to memory version and target world state.
- Repeated revision events and jobs are idempotent.
- Conflict notifications are deduplicated by competing claims and
  applicability.
- Shadow evaluation precedes enforcement.
- High-risk unknown claims are withheld rather than guessed.
- The first implementation uses exact paths/symbols/config/dependencies before
  considering generalized program analysis.

## Evaluation Corpus

Build sanitized fixture repositories and memory histories for:

- unrelated file edit;
- linked implementation edit that preserves the claim;
- exact rename;
- symbol deletion;
- API signature change;
- configuration key change;
- dependency upgrade inside and outside applicability;
- migration that changes a data-model fact;
- test that continues to prove a claim;
- test that disproves a descriptive claim;
- implementation drift from a prescriptive decision;
- newer explicit decision superseding an older decision;
- historical incident unaffected by current change;
- feature-branch preview followed by merge;
- PR closed without merge;
- rapid sequence of default-branch revisions;
- repository revert;
- duplicate revision event;
- worker crash and provider outage;
- malformed provider output;
- weak versus strong evidence;
- genuine same-state contradiction;
- cross-project isolation attempt.

Each fixture records expected impact set, expected deterministic result, allowed
model outcomes, context eligibility, and whether a human conflict is valid.

## Metrics And Acceptance Signals

### Reliability

- accepted revision events with durable processing intent;
- impacted memory/target pairs complete or actively retrying;
- expired leases beyond reconciliation SLO;
- destructive transitions caused by operational failure;
- duplicate durable effects.

### Coverage

- active memories with typed anchors;
- memories with a known validated repository state;
- current context items validated for requested state;
- default-branch revisions fully processed;
- semantic-only impact discoveries later upgraded to exact anchors.

### Latency

- merge to impact plan;
- merge to deterministic validation;
- merge to complete revalidation;
- oldest pending work by failure class.

### Quality

- impact precision and recall;
- false invalidation/restoration rate;
- stale-memory injection rate;
- automatic confirm/revise/supersede/refute/conflict distribution;
- human conflict uphold rate;
- conflicts later shown to be ordinary temporal supersession;
- unneeded model-call rate;
- context usefulness before and after temporal gating.

### Autonomy

- non-conflict revalidations resolved automatically;
- non-conflict pending age;
- semantic inbox items that are genuine conflicts;
- operational failures incorrectly routed to semantic review;
- ordinary memories requiring manual maintenance.

Exact thresholds belong in the checkpoint evaluation spec after a baseline is
measured. The rollout cannot proceed on test count alone.

## Exact Feature Implementation Order

Memory CI begins only after core lossless work creation, recovery, atomic memory
transitions, conflict-only curation, and immutable context primitives exist.

Each subcheckpoint is one coherent branch/PR. Mutable work on the next does not
begin until the prior subcheckpoint merges.

Mapping to the parent roadmap is exact:

- roadmap Checkpoint 7 contains MCI-0 through MCI-4;
- roadmap Checkpoint 8 contains MCI-5 then MCI-6;
- roadmap Checkpoints 9 and 10 then prove shared retrieval performance,
  historical repair, canary operation, MCI-7A rollout, and release readiness;
- optional MCI-7B advanced surfaces follow the core campaign.

### MCI-0 — Feature Contract And Evaluation Corpus

Objective:

- lock claim posture, applicability, genuine conflict, pending-retrieval
  policy, and accepted world-state source;
- create the fixture corpus and baseline metrics;
- measure the baseline, then freeze initial impact, false-stale,
  stale-injection, and destructive-decision acceptance thresholds in the
  focused evaluation contract;
- write the focused data-retention and rollback decision.

Parallel packages:

- product semantics;
- fixture repository design;
- eval scorer;
- read-only current anchor inventory.

Gate:

- every fixture has an expected impact and state outcome;
- false-stale and missed-impact measurements and initial rollout thresholds are
  defined in the focused evaluation contract;
- no code or schema is changed.

### MCI-1 — Canonical Revision Ledger

Objective:

- accept idempotent default-branch world-state events;
- normalize base/head and changed/renamed path manifests;
- create durable processing work atomically;
- expose latest accepted, impact-planned, and fully processed project state;
- establish one trusted SCM/CI evidence source for bounded before/after blobs
  or fingerprints. Attested test metadata is optional in the first slice.

Parallel packages after the event contract freezes:

- source adapter;
- backend ingest;
- workflow/reconciler integration;
- contract/fault tests;
- operations read model.

Gate:

- duplicate, out-of-order, missing-base, rapid-update, worker-crash, and
  cross-project cases pass;
- missing or incomplete bounded source evidence is explicit and yields
  unknown/retry, never confirm, refute, or supersede;
- no arbitrary repository execution is introduced.

### MCI-2 — Minimal Path/Symbol Anchors

Objective:

- normalize existing exact file-path and symbol anchors only;
- preserve anchor provenance and quality;
- backfill only evidence that can be derived exactly from existing Engram
  provenance.

Parallel packages after one schema owner:

- path/symbol normalization;
- existing-provenance mapping;
- backfill/dry-run report;
- anchor coverage evaluation.

Gate:

- every new code-sensitive memory has exact path/symbol anchors or an explicit
  unanchored reason;
- backfill is scoped, idempotent, resumable, and non-destructive;
- model-only anchors cannot drive destructive transitions.

Configuration, dependency, migration, test, environment, broad lineage,
semantic anchor inference, and custom console work are deferred until the
path/symbol causal loop passes MCI-4.

### MCI-3 — Minimal Shadow Impact Planner

Objective:

- build deterministic changed/renamed/deleted path and existing-symbol impact
  traversal;
- persist explainable revision-scoped impact plans;
- create revalidation work without changing memory.

Parallel packages:

- path/rename rules;
- existing-symbol rules;
- impact read model;
- fixture scorer.

Gate:

- duplicate revision produces one plan;
- unrelated changes avoid global revalidation;
- rename/delete behavior is correct;
- impact recall/precision meets the shadow threshold;
- latest impact-planned revision advances only after the complete plan commits;
- production memory state remains unchanged.

There is no semantic expansion in this first causal slice. It may be added as a
bounded recall-only input in MCI-5 after deterministic impact behavior is
measured.

### MCI-4 — Deterministic Revalidation

Objective:

- confirm unchanged/equivalent evidence;
- update renamed anchors;
- detect deletion/change signals for exact path/symbol claims and prescriptive
  drift;
- persist shadow validation assertions.

Parallel packages:

- fingerprint validators;
- path/symbol validators;
- posture-aware decision policy;
- work/retry integration;
- deterministic eval.

Gate:

- most unchanged cases avoid a model call;
- missing required before/after evidence yields unknown/retry rather than a
  semantic result;
- a failed validator cannot directly refute a prescriptive rule;
- decisions are fenced to target state and memory version;
- active retrieval is still unchanged until MCI-6.

### MCI-5 — Model-Assisted Revalidation And Atomic Lineage

Objective:

- adjudicate ambiguous evidence through a structured provider contract;
- add bounded semantic impact expansion as recall-only input after exact scope
  and causal reasons are established;
- automatically confirm, revise, narrow, merge, supersede, or refute;
- produce conflict only after the genuine-conflict test;
- apply outcomes through atomic semantic transitions.

Parallel packages after the decision contract freezes:

- evidence package builder;
- provider adapter and fallback;
- decision policy;
- lineage transition integration;
- conflict API/UI;
- eval scorer.

Gate:

- provider outage and malformed output mutate no semantic state;
- destructive transitions meet evidence thresholds;
- old and new versions remain reconstructable;
- shadow decisions meet eval thresholds;
- only conflicts reach humans.

### MCI-6 — Temporal Retrieval And Context Enforcement

Objective:

- enable validity/applicability filtering before relevance ranking;
- bind bundles to repository state and exact memory versions;
- expose memory delta and validity explanations;
- withhold unsafe unknown claims.

Parallel packages:

- retrieval eligibility;
- bundle fingerprint/snapshot;
- memory-delta service;
- CLI/plugin rendering;
- console timeline;
- E2E.

Gate:

- no known obsolete claim is injected as current;
- a request for accepted revision R before its impact plan completes withholds
  code-sensitive memory or explicitly pins to an older processed revision;
- context replay is byte-stable or rejects fingerprint mismatch;
- conflict is represented as warning, not settled truth;
- high-risk pending memory is withheld;
- token budgets remain strict.

### MCI-7A — Continuous Operation And Default-On Rollout

Objective:

- run on accepted changes plus a periodic coverage sweep;
- expand canaries to default-on.

Parallel packages:

- rollout/health;
- coverage and retry sweep;
- revert regression coverage;
- load/cost evaluation.

Gate:

- canary projects meet reliability, quality, autonomy, and latency thresholds;
- rollback is drilled;
- no blind historical corpus rewrite;
- default-on expansion is organization-scoped and reversible.

MCI-7A is delivered through roadmap Checkpoint 10 after shared retrieval
performance and historical repair are proven.

### MCI-7B — Optional Advanced Developer Surfaces

Objective:

- add provisional PR preview;
- support revision-addressable historical/time-travel queries;
- trial non-blocking memory contracts;
- expand typed anchors to configuration, dependencies, migrations, tests,
  environments, and cross-repository contracts only as measured need appears.

Parallel packages:

- PR preview adapter;
- historical/time-travel query;
- contract-run evidence adapter;
- one bounded typed-anchor expansion at a time.

Gate:

- no provisional result mutates canonical memory;
- historical queries preserve authorization and immutable bundle evidence;
- validator proposals do not enter the conflict inbox or block CI without
  explicit authorization;
- each new anchor type improves evaluated impact quality enough to justify its
  complexity.

MCI-7B is not required for the core autonomous-loop campaign. It is the
advanced product expansion after the causal loop is reliable in production.

## Rollout Policy

1. Inventory anchors without behavior change.
2. Ingest canonical revisions in report-only mode.
3. Produce shadow impact plans.
4. Enable deterministic confirmations only.
5. Run model adjudication in shadow.
6. Canary non-destructive automatic revisions.
7. Canary temporal context filtering.
8. Enable destructive supersede/refute only after evaluation and rollback proof.
9. Keep conflict-only human review throughout.
10. Expand by project and organization while domain invariants remain green.

Historical memory is not bulk-marked current merely because the backfill ran.
It receives a declared baseline with explicit evidence quality, then evolves
from new world-state events.

## Acceptance Scenarios

Memory CI is not complete until it proves at least:

1. An unrelated file change produces no model call and no memory mutation.
2. A linked file edit with unchanged relevant symbol confirms the memory.
3. A rename updates the anchor without declaring the claim false.
4. A deleted symbol makes a descriptive instruction ineligible pending
   resolution, then supersedes/refutes it with evidence.
5. Code violating a prescriptive decision keeps the decision and reports
   conformance drift.
6. A newer explicit decision supersedes the old prescription automatically.
7. A historical incident remains historical and is not injected as current.
8. A dependency bump updates version applicability.
9. Provider outage leaves validation pending and automatically resumes.
10. Malformed provider output creates no semantic result.
11. Duplicate and out-of-order revision events converge idempotently.
12. A newer head fences an older worker decision.
13. A revert coherently revalidates an appropriate prior version.
14. Two claims for different environments do not create a conflict.
15. Two supported incompatible claims for the same state do create one
    deduplicated conflict.
16. Context for revision A remains replayable after revision B changes memory.
17. Context for current head excludes known obsolete instructions.
18. A cross-project change cannot inspect or revalidate another project's
    memory.
19. Context requested after revision R is accepted but before its impact plan
    completes cannot present code-sensitive memory as certified for R.

## Recommended First Slice

The first implementation should be intentionally narrow:

- one default-branch revision event;
- changed/renamed path manifest;
- existing exact path and symbol anchors;
- durable revision and revalidation work identity;
- shadow-only impacted-memory report;
- deterministic unchanged/rename confirmation;
- no automatic refute or supersede;
- invariant and lag metrics;
- fixtures for edit, rename, delete, unrelated change, duplicate event, worker
  crash, provider outage, and newer-head fencing.

That slice proves the causal loop. It avoids prematurely building a generalized
graph, AST platform, SCM suite, or model-driven PR gate.

## Core Feature Definition Of Done

The core Memory CI loop is complete when:

- every accepted canonical code change has durable, recoverable processing;
- impact selection is scoped, explainable, and evaluated;
- active memories expose posture, applicability, anchors, and validated world
  state;
- deterministic validation handles unchanged and mechanical changes;
- ambiguous cases use bounded evidence-aware model adjudication;
- infrastructure failure creates no semantic mutation;
- automatic transitions preserve immutable versions and evidence;
- only genuine conflicts reach humans;
- retrieval filters for temporal validity before similarity;
- context bundles are revision-bound and reproducible;
- stale-memory injection is measured and within the accepted threshold;
- memory timeline and project health make the behavior understandable;
- a revert is processed coherently;
- fresh-clone and fault-injection E2E prove continuous operation;
- rollout and rollback are project-scoped, canaried, and documented.

## Advanced Maturity Gate

The optional MCI-7B expansion is complete when:

- provisional PR preview cannot mutate canonical memory;
- an authorized historical context request reconstructs revision-addressable
  memory and immutable bundle evidence;
- non-blocking memory contracts return posture-correct evidence;
- every additional typed anchor proves measured impact-quality value;
- the advanced surfaces reuse the core state machine rather than creating a
  second memory-maintenance loop.
