# Upstream Migration Import Design

## Goal

Close the `claude-mem` parity migration gate with a small, auditable importer
for useful upstream local memory artifacts.

This slice imports sanitized upstream SQLite memory/session data into Engram's
server-side PostgreSQL model, reports unsupported runtime artifacts explicitly,
and proves reruns are idempotent with a checked-in fixture. It does not add a
general migration framework, transcript replay engine, Chroma/vector import,
provider generation, frontend screens, MCP tools, or broad memory curation UI.

## Current Gap

The Compose golden path proves a new Engram session can create memory and later
retrieve it. The parity gate is still incomplete because existing `claude-mem`
users have no committed path to migrate useful local memory/session artifacts,
and no fixture-backed test proves unsupported artifacts are reported rather than
silently ignored.

The authoritative requirements are:

- `goal.md` requires a migration path or unsupported-record report before North
  Star expansion;
- `docs/parity/claude-mem-parity-map.md` lists upstream artifacts and importer
  requirements;
- no current `engram.imports` app, importer command, sanitized upstream fixture,
  or importer tests exist.

## Source Artifacts

The first migration slice reads a `claude-mem` source root with:

- `claude-mem.db`;
- optional `settings.json`;
- optional `transcript-watch.json`;
- optional `transcript-watch-state.json`;
- optional `corpora/`;
- optional `vector-db/` or Chroma runtime directory.

The SQLite database is the only authoritative import source in this slice.
Useful records:

- `sdk_sessions` become Engram `AgentSession` records;
- `observations` become `RawEventEnvelope`, `Observation`,
  `ObservationSource`, approved `Memory`, `MemoryVersion`, and
  `RetrievalDocument` records;
- `session_summaries` follow the same memory path as observations;
- `user_prompts` become raw provenance events only and are not promoted to
  memory.

Unsupported or deferred records are reported with source ids and reasons:

- transient local-worker queues such as `pending_messages`;
- `observation_feedback`;
- upstream server-owned tables such as `projects`, `server_sessions`,
  `agent_events`, `memory_items`, `memory_sources`, `teams`, `team_members`,
  `api_keys`, and `audit_log`;
- FTS and schema housekeeping tables;
- transcript watcher config/state;
- raw JSONL transcripts;
- corpora;
- Chroma/vector directories;
- `.env` secrets, which are never read as values.

## Target Mapping

The importer writes into an existing organization, project, and optional team.
The operator must pass exact target ids. The importer must reject cross-scope
targets before writes.

Stable source ids are deterministic strings:

- `claude-mem:{source_store_id}:sdk_session:{content_session_id}`;
- `claude-mem:{source_store_id}:observation:{memory_session_id}:{row_id}`;
- `claude-mem:{source_store_id}:session_summary:{memory_session_id}:{row_id}`;
- `claude-mem:{source_store_id}:user_prompt:{content_session_id}:{prompt_number}:{row_id}`.

Records use current Engram models:

- imported sessions use `AgentSession.external_session_id`;
- imported prompt rows use `RawEventEnvelope.client_event_id` and
  `idempotency_key`;
- imported observations store upstream ids in `Observation.source_metadata`;
- provenance uses `ObservationSource(source_type='claude_mem', source_id=...)`;
- imported memory uses `Memory.metadata.source = 'claude_mem_import'`;
- retrieval documents are built through `IndexMemoryVersion`, not direct
  duplicate indexing code.

The importer must not require schema changes in this slice. If existing
uniqueness constraints cannot provide idempotency with stable source ids, stop
and redesign before writing migrations.

## Report Contract

Both dry-run and apply modes return a JSON object:

```json
{
  "mode": "dry_run",
  "source": {
    "kind": "claude_mem",
    "source_store_id": "fixture-store",
    "root": "/path/to/source",
    "detected_tables": ["sdk_sessions", "observations"],
    "schema_versions": []
  },
  "target": {
    "organization_id": "...",
    "project_id": "...",
    "team_id": "..."
  },
  "counts": {
    "sdk_sessions": {"seen": 1, "importable": 1},
    "user_prompts": {"seen": 1, "importable_raw_events": 1},
    "observations": {"seen": 1, "importable_memories": 1},
    "session_summaries": {"seen": 1, "importable_memories": 1},
    "pending_messages": {"seen": 1, "unsupported": 1}
  },
  "created": {
    "agents": 0,
    "sessions": 0,
    "raw_events": 0,
    "observations": 0,
    "memory_candidates": 0,
    "memories": 0,
    "memory_versions": 0,
    "retrieval_documents": 0
  },
  "duplicates": {
    "sessions": 0,
    "raw_events": 0,
    "observations": 0,
    "memories": 0
  },
  "unsupported": [
    {
      "source_type": "pending_messages",
      "source_id": "pending_messages:1",
      "reason": "transient_local_worker_queue"
    }
  ],
  "warnings": [],
  "redactions": {"redacted": false}
}
```

Apply mode uses `"mode": "apply"` and populated `created` / `duplicates`.
Dry-run mode must not write Engram rows.

## Fixture

Add a reviewed text fixture under:

```text
apps/backend/engram/imports/fixtures/claude_mem_minimal/
  manifest.json
  claude_mem_minimal.sql
  settings.json
  transcript-watch.json
  transcript-watch-state.json
  corpora/deferred.corpus.json
  vector-db/.keep
```

Tests build a temporary SQLite database from the `.sql` file. The fixture must
contain:

- one sanitized SDK session;
- one sanitized user prompt;
- one generated observation with file/citation metadata;
- one session summary;
- one pending message;
- one observation feedback row.

The fixture must not contain real prompts, real repository paths, real API keys,
provider secrets, customer names, or private product names. Token-shaped strings
included for redaction tests must be fake and must be persisted only as
`[REDACTED]`.

## Command

Add:

```bash
python manage.py engram_import_claude_mem SOURCE_ROOT \
  --organization-id ORG \
  --project-id PROJECT \
  --team-id TEAM \
  --source-store-id STORE \
  --dry-run \
  --json
```

Default mode is dry-run unless `--apply` is passed. The command prints only the
JSON report when `--json` is used. It must not print raw secrets or unredacted
token-shaped prompt/tool content.

## Security

This is import/export/migration code, so the slice requires a focused security
review artifact under `docs/security/reviews/`.

The review must cover:

- tenant/project/team target validation before writes;
- unsupported secret-bearing artifacts;
- `.env` handling;
- prompt/tool redaction before persisted rows and command output;
- idempotency and duplicate prevention;
- no vector/Chroma import as authority;
- no server-owned upstream credential import.

## Verification

Required local commands:

- `python3 scripts/repository_layout.py`
- `python3 scripts/repository_quality.py`
- `python3 -m unittest discover -s tests -v`
- `cd apps/backend && poetry run pytest engram/imports/upstream_import_tests.py -v`
- `cd apps/backend && poetry run pytest -v`
- `cd apps/backend && poetry run ruff check .`
- `cd apps/backend && poetry run ruff format --check .`
- `docker compose -f deploy/compose/docker-compose.yml run --build --rm api sh -ec "python manage.py migrate --noinput && python manage.py makemigrations --check --dry-run"`
- `cd apps/backend && poetry check --lock`
- `git diff --check HEAD`

Required CI:

- Backend;
- Repository Quality.

CI continues to run migration apply and migration freshness through the Backend
workflow's PostgreSQL service. Local migration verification uses Compose so the
backend runs against PostgreSQL instead of the host SQLite test default.

## Boundaries

This slice owns:

- upstream migration/import design and plan;
- a minimal importer service and management command;
- a sanitized fixture;
- idempotent dry-run/apply tests;
- explicit unsupported artifact reporting;
- focused migration security review artifact;
- repository/CI gates for the importer.

This slice defers:

- raw transcript replay;
- Chroma/vector import;
- corpora import;
- upstream server-owned table import;
- frontend/admin import UI;
- MCP import tools;
- broad dedupe/merge/stale/refuted curation workflows.

## Self-Review

- The design closes the explicit migration compatibility hard gate without
  widening into a general migration platform.
- The importer uses current Engram models and services instead of adding a new
  schema.
- Unsupported data is reported explicitly, not silently ignored.
- The fixture and security review make import behavior auditable.
