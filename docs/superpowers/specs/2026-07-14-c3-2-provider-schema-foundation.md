# C3.2 Provider Schema Foundation

Date: 2026-07-14
Status: focused implementation specification
Roadmap gate: Checkpoint 3 prerequisite for C3.2

## Scope

This slice closes two contract gaps discovered by the completed C3.2 review:

1. the provider gateway does not expose the strict `distill_extract.v1`
   response contract that the C3.2 stage parser requires; and
2. `DistillationStage.stage_key` binds `policy_role`, while the database
   coordinate uniqueness constraint does not.

The slice is intentionally narrower than C3.2. It adds one provider response
kind and one forward migration, then leaves stage execution, replay, fallback,
and parsing behavior to the existing C3.2 branch.

## Provider Response Contract

`ProviderCallInput.response_kind='distill_extract.v1'` is a first-class
structured response kind. It has an 8192-token output budget and uses the
existing provider-specific structured-output mechanisms:

- Anthropic receives a forced tool named `emit_distillation_extraction` whose
  input schema is the extraction contract below.
- OpenAI-compatible providers that support JSON mode receive the existing
  `json_object` response-format request. The strict stage parser remains the
  semantic authority; this slice does not add a second parser or a generic
  JSON-schema engine.
- Metadata-controlled provider capability behavior remains unchanged. In
  particular, this slice does not silently enable JSON mode for a policy that
  explicitly disables it.

The structured object has exactly two required top-level keys and rejects
additional properties:

```json
{
  "memories": [
    {
      "title": "non-empty string, at most 255 characters",
      "body": "string, at most 3000 characters",
      "confidence": 0.0,
      "supporting_observation_ids": ["uuid"],
      "kind": "optional non-digest memory kind"
    }
  ],
  "no_signal_observation_ids": ["uuid"]
}
```

`memories` has at most 12 entries. Every memory requires `title`, `body`,
`confidence`, and a non-empty, duplicate-free `supporting_observation_ids`
array. `confidence` is numeric in the inclusive range `[0, 1]`. Optional
`kind` is one of `decision`, `convention`, `gotcha`, `architecture`, or
`incident`. `no_signal_observation_ids` is required and duplicate-free.

The schema guides generation only. Exact manifest membership, complete
coverage, overlap, UUID canonicalization, and all other semantic checks remain
owned by the C3.2 strict parser.

## Deterministic Fake Provider

The real `FakeProviderGateway`, not a test monkeypatch, must return a payload
that the strict extraction parser accepts:

- collect unique canonical UUIDs from exact `Observation: <uuid>` lines in
  their prompt order;
- when at least one id exists, emit one deterministic `gotcha` memory whose
  supporting ids contain every collected id and an empty no-signal list;
- when no id exists, emit empty `memories` and empty
  `no_signal_observation_ids`;
- emit no legacy `source_ids`, no extra top-level keys, and no fabricated ids.

Repeated calls with the same input retain the gateway's existing deterministic
body behavior and still create independently measurable `ProviderCallRecord`
rows. This slice does not add provider response replay or body persistence.

## Stage Coordinate Identity

`core_distill_stage_coord_uniq` is changed to the exact coordinate:

```text
(window, stage_kind, level, ordinal, policy, policy_version, policy_role)
```

This matches `stage_key`: primary and fallback attempts may use the same
policy/version without colliding, while two rows with the same role and exact
coordinate still conflict. The partial unique completed-target constraint
continues to select the single accepted logical result.

The change is a new reversible migration
`0037_distillation_stage_policy_role_coord.py`. Merged migration `0036` is not
rewritten. The migration removes and recreates the named constraint, reverses
back to the `0036` shape, and reapplies cleanly.

## File Ownership

This prerequisite slice is the sole owner of:

- `apps/backend/engram/model_policy/services.py`;
- focused model-policy gateway tests;
- `apps/backend/engram/core/models.py`;
- new migration `0037_distillation_stage_policy_role_coord.py`;
- focused model and migration tests;
- this specification, its implementation plan, and the corresponding
  ownership/constraint amendment in the authoritative CP3 specification.

It does not edit `distillation_provider_stage.py`, candidate parsing,
reduction, finalization, Celery routing, provider replay semantics, or stored
provider response content.

## Required RED Tests

1. A real fake-gateway call with exact observation lines returns only the
   strict extraction keys, covers every prompt observation exactly once, and
   remains deterministic across repeated calls.
2. The Anthropic extraction tool schema has the exact required fields, limits,
   enum, uniqueness, and `additionalProperties=false` contracts.
3. Anthropic forces that tool for `distill_extract.v1`; OpenAI-compatible JSON
   mode is requested when supported; the response kind resolves to 8192 max
   tokens.
4. The model constraint metadata includes `policy_role`.
5. At migration state `0036`, primary and fallback rows with the same
   policy/version coordinate collide. At `0037`, both roles coexist, a
   duplicate of either role still collides, reverse restores the old shape,
   and reapply restores the new shape.

Tests must be committed while failing for the missing product behavior before
production code is added.

## Review And Verification

The slice receives exactly one review round: one adversarial correctness
review and one simplicity review, both read-only. Confirmed findings are
handled in one fix round; focused gates are then rerun once without a second
review round.

All backend commands run from this worktree through the root Compose stack
with project name `engram-c32-foundation`. Required evidence is:

- focused provider, model, and migration tests;
- `ruff check` and `ruff format --check` for owned Python files;
- database migration, `manage.py check`, and `makemigrations --check --dry-run`;
- branch push before the long full-suite run;
- one full backend suite and all required CI checks green.

## Acceptance Gate

The slice is complete only when real fake, Anthropic, and OpenAI-compatible
gateway paths honor `distill_extract.v1`; the role-aware constraint survives
apply/reverse/reapply with real inserts; no existing response kind changes
behavior; the authoritative CP3 contract matches the schema; and the branch is
squash-merged before C3.2 resumes.
