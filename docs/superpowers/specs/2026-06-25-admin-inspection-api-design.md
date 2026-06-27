# Admin Inspection API Design

## Goal

Add the minimal V1 operational inspection API so an admin or team lead can
inspect memories, context bundles, and audit events after the parity loop runs.

## Architecture

The API is backend-only and read-only under `engram.inspection`. It does not add
a custom frontend, MCP bridge, search UI, mutation workflow, provider calls, or
semantic retrieval. The endpoint namespace is `/v1/inspection/*` so this
temporary operational surface stays separate from future product APIs.

Memory and context-bundle inspection require `memories:admin`. Audit inspection
requires `audit:read`. Every request must include `project_id`; `team_id` is
optional and uses the existing API-key scope resolver. When `team_id` is omitted,
project-level records and records in the resolved key-bound teams are visible.
When `team_id` is supplied, project-level records and that authorized team are
visible. Unauthorized project or team requests fail through the existing access
error shape.

Inspection responses include IDs, scope, status, timestamps, and bounded nested
details needed to debug the rewritten memory loop. They do not expose raw API
keys or provider secrets; metadata, scope evidence, authorization scope,
content-bearing fields, and client-propagated request/audit identifiers are
redacted through the shared redaction tooling before response serialization.

## Non-Goals

- No frontend/admin shell.
- No MCP tools.
- No memory mutation beyond existing feedback endpoint.
- No provider/model-policy or embedding behavior.
- No broad organization-wide audit browsing without an explicit project scope.

## Verification

Focused backend tests cover authorized memory listing/detail, context-bundle
listing/detail, audit listing, missing capability denial for regular
`memories:read` keys, cross-team denial, response redaction, and suppression of
inspection-generated audit scope rows from audit listing output. Repository
layout and quality checks pin the new package surface.
