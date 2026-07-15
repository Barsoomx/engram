# C4 Import Provenance Foundation

Date: 2026-07-15

Status: implementation specification for the C4.3 prerequisite slice

Depends on:

- `docs/superpowers/specs/2026-07-11-checkpoint-1-lossless-work-creation.md`;
- `docs/superpowers/specs/2026-07-11-checkpoint-3-complete-distillation.md`;
- `docs/superpowers/specs/2026-07-11-checkpoint-4-atomic-memory-transitions.md`.

## Goal

Let synchronous Claude-memory imports publish version-1 memories through
`PromoteMemoryCandidate` without inventing a CP1 session-end transition, CP3
distillation window, provider stage, provider call, observation coverage, or
candidate-decision work.

The import report, confidence, metadata, row counts, replay behavior, and
prompt-only behavior remain compatible. Existing version-0 imported history
remains readable and replayable without semantic backfill.

## Chosen Design

Reuse the durable `ObservationSource` already created for every imported
observation. Extend immutable `MemoryCandidateSource` with a typed shape:

- `distillation`: the existing window + observation + stage relation;
- `import`: observation + protected `ObservationSource`, with no window or
  stage.

`MemoryVersionSource` continues to point at exactly one
`MemoryCandidateSource` or source memory version. No generic provenance table
or participant framework is added.

New imports create a version-1 candidate and one import candidate source, then
call the existing typed `PromoteMemoryCandidate`. The transition transaction
creates one memory/version/source chain, exact projection, embedding intent,
audit, transition, and candidate/current pointers. Import promotion does not
create or settle `CANDIDATE_DECISION` work because import is a synchronous
adapter decision, not a CP3/CP5 policy decision.

## Schema Contract

Add `MemoryCandidateSourceKind` with `distillation` and `import` values.
`MemoryCandidateSource.source_kind` is non-null with `distillation` as the
model/database default so existing source creation remains unchanged.

Add nullable protected `MemoryCandidateSource.import_source` pointing to
`ObservationSource`. Make only `window` and `stage` nullable. `observation`
remains required.

The database accepts exactly these shapes:

| kind | window | stage | import_source |
|---|---:|---:|---:|
| `distillation` | required | required | null |
| `import` | null | null | required |

Distillation uniqueness remains candidate + window + observation. Import
uniqueness is candidate + import source. The model validates candidate,
observation, stage/window, and import-source scope; an import source must name
the same observation as the candidate source. All new fields join the immutable
field fence.

Migration `0039` backfills existing source rows as `distillation`. Reverse is
allowed while no import source exists and refuses once import provenance has
been committed. It never deletes or fabricates provenance.

## Import Provenance Contract

The immutable import anchor snapshot contains exactly:

```json
{
  "schema": "import_candidate_source.v1",
  "observation_id": "<uuid>",
  "session_sequence": 1,
  "observation_digest": "<sha256>",
  "source_type": "claude_mem",
  "source_id": "<bounded stable import id>",
  "source_store_id": "<store id>",
  "event_type": "<observation-or-summary event>",
  "raw_event_id": "<uuid-or-null>"
}
```

`anchors_hash` is the canonical SHA-256 of that snapshot. The candidate
identity keeps the existing import hash algorithm over
`('memory-candidate', source_id, observation.content_hash)` so replay after
upgrade cannot duplicate already imported version-0 rows.

The import evidence manifest is the canonical ordered list of immutable import
anchor snapshots and hashes. Distillation evidence manifests remain byte-for-
byte unchanged. Mixed distillation/import candidate sources fail closed.

For an import candidate, the fence rechecks candidate/source scope, candidate
title/body against the imported observation, the legacy-compatible candidate
hash, and the exact import evidence-manifest hash after locking. A mismatch
raises retryable `stale_decision` before semantic writes.

## Transition And Replay Rules

- `PromoteMemoryCandidate` remains the only publishable import writer.
- Import promotion accepts no candidate-decision work claim and creates no
  candidate-decision work.
- The import idempotency key remains candidate-scoped; exact replay returns the
  existing transition and creates no source, audit, projection, embedding work,
  or semantic row.
- Memory metadata is derived inside the transition from the immutable import
  source: `source`, `source_store_id`, `source_id`, and `event_type`. The import
  adapter does not mutate memory metadata after promotion.
- Newly created imported memories are transition contract version 1.
- An existing promoted version-0 import candidate is replayed read-only. A
  proposed or identity-mismatched version-0 candidate fails closed rather than
  being upgraded in place.
- Prompts remain raw-event-only and missing-session rows remain skipped.

## Invariants

P7 accepts an import promotion without candidate-decision work only when the
candidate has exactly the valid import-source shape and the normal version-1
transition, audit, pointer, source, exact-document, and embedding-work relation
is coherent. Distillation promotions continue to require settled
candidate-decision work.

P3/P5 remain unchanged: imported ended sessions are intentionally version 0
and are not relabeled as completed CP3 distillation. P6 sees no imported
proposed candidate because candidate/source/promotion commit synchronously.

## Required RED Tests

1. Migration forward preserves existing distillation sources, adds the import
   shape, and fresh apply succeeds.
2. Migration reverse preserves legacy/distillation rows and refuses after an
   import source exists.
3. Database/model constraints reject half-shaped, mixed, observation-mismatched,
   and foreign-scope import sources.
4. Import apply creates one version-1 candidate/source/memory/version,
   `MemoryVersionSource`, exact document, embedding work, transition, and audit,
   with no candidate-decision work.
5. Imported confidence and metadata remain `.700`/`.800` and source-store/
   event identity is committed before exact projection.
6. Exact replay preserves report counts and creates no duplicate source,
   transition, audit, projection, work, or semantic row.
7. A promotion fault rolls back raw event, observation, source, candidate, and
   the entire semantic chain.
8. A stale import candidate/source fence fails before semantic writes.
9. P7 is healthy for coherent version-1 import promotion and still requires
   candidate-decision work for distillation promotion.
10. Existing version-0 imported replay remains read-only.

No selector tests, marker meta-tests, or tests of the test harness are added.

## Files And Ownership

This prerequisite slice owns only:

- `core/models.py`, migration `0039`, and focused model/migration tests;
- a focused import-provenance helper and tests;
- the import-specific branch of `memory/transitions.py`, its service adapter,
  and P7 evaluation/tests;
- `imports/services.py` and existing import contract tests;
- this specification and its implementation plan.

It does not change CP3 distillation semantics, CP5 curation policy, deployment,
secrets, provider behavior, or the two-job pytest CI split.

## Verification And Delivery

All Python/Django verification runs in root Compose project
`engram-c4import`, one pytest process at a time. The branch is pushed before
long/full or transactional runs.

Required gates are focused RED/GREEN, affected import/transition/invariant
tests, migration forward/reverse/fresh apply, `makemigrations --check`, Django
check, Ruff check/format, `git diff --check`, ordinary full lane, serialized
transactional lane, then exactly one fresh correctness/security review and one
simplicity review, one fix round, no re-review, CI, and squash merge.

After merge, fresh master is merged into `feat/c4-3-writer-convergence`
without rebase or force-push. C4.3 then resumes. CP5 remains out of scope.

## Stop Conditions

Stop if the implementation would require synthetic CP1/CP3 work, provider
history, a generic provenance participant framework, direct Memory writes,
semantic backfill of version-0 history, deletion of evidence, or a public import
report/API change.
