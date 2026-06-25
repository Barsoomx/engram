PRAGMA foreign_keys = ON;

CREATE TABLE schema_versions (
  id INTEGER PRIMARY KEY,
  version INTEGER UNIQUE NOT NULL,
  applied_at TEXT NOT NULL
);

CREATE TABLE sdk_sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  content_session_id TEXT UNIQUE NOT NULL,
  memory_session_id TEXT UNIQUE,
  project TEXT NOT NULL,
  platform_source TEXT NOT NULL DEFAULT 'claude',
  user_prompt TEXT,
  started_at TEXT NOT NULL,
  started_at_epoch INTEGER NOT NULL,
  completed_at TEXT,
  completed_at_epoch INTEGER,
  status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'completed', 'failed')),
  worker_port INTEGER,
  prompt_counter INTEGER DEFAULT 0,
  custom_title TEXT
);

CREATE TABLE user_prompts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  content_session_id TEXT NOT NULL,
  prompt_number INTEGER NOT NULL,
  prompt_text TEXT NOT NULL,
  created_at TEXT NOT NULL,
  created_at_epoch INTEGER NOT NULL,
  FOREIGN KEY(content_session_id) REFERENCES sdk_sessions(content_session_id) ON DELETE CASCADE
);

CREATE TABLE observations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  memory_session_id TEXT NOT NULL,
  project TEXT NOT NULL,
  text TEXT,
  type TEXT NOT NULL,
  title TEXT,
  subtitle TEXT,
  facts TEXT,
  narrative TEXT,
  concepts TEXT,
  files_read TEXT,
  files_modified TEXT,
  prompt_number INTEGER,
  discovery_tokens INTEGER DEFAULT 0,
  content_hash TEXT,
  agent_type TEXT,
  agent_id TEXT,
  merged_into_project TEXT,
  generated_by_model TEXT,
  metadata TEXT,
  created_at TEXT NOT NULL,
  created_at_epoch INTEGER NOT NULL,
  FOREIGN KEY(memory_session_id) REFERENCES sdk_sessions(memory_session_id)
    ON DELETE CASCADE ON UPDATE CASCADE,
  UNIQUE(memory_session_id, content_hash)
);

CREATE TABLE session_summaries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  memory_session_id TEXT NOT NULL,
  project TEXT NOT NULL,
  request TEXT,
  investigated TEXT,
  learned TEXT,
  completed TEXT,
  next_steps TEXT,
  files_read TEXT,
  files_edited TEXT,
  notes TEXT,
  prompt_number INTEGER,
  discovery_tokens INTEGER DEFAULT 0,
  merged_into_project TEXT,
  created_at TEXT NOT NULL,
  created_at_epoch INTEGER NOT NULL,
  FOREIGN KEY(memory_session_id) REFERENCES sdk_sessions(memory_session_id)
    ON DELETE CASCADE ON UPDATE CASCADE
);

CREATE TABLE pending_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_db_id INTEGER NOT NULL,
  content_session_id TEXT NOT NULL,
  tool_use_id TEXT,
  message_type TEXT NOT NULL CHECK(message_type IN ('observation', 'summarize')),
  tool_name TEXT,
  tool_input TEXT,
  tool_response TEXT,
  cwd TEXT,
  last_user_message TEXT,
  last_assistant_message TEXT,
  prompt_number INTEGER,
  status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'processing')),
  created_at_epoch INTEGER NOT NULL,
  agent_type TEXT,
  agent_id TEXT,
  FOREIGN KEY (session_db_id) REFERENCES sdk_sessions(id) ON DELETE CASCADE
);

CREATE TABLE observation_feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  observation_id INTEGER NOT NULL,
  signal_type TEXT NOT NULL,
  session_db_id INTEGER,
  created_at_epoch INTEGER NOT NULL,
  metadata TEXT,
  FOREIGN KEY (observation_id) REFERENCES observations(id) ON DELETE CASCADE
);

INSERT INTO schema_versions (id, version, applied_at)
VALUES (1, 1, '2026-06-25T00:00:00Z');

INSERT INTO sdk_sessions (
  id,
  content_session_id,
  memory_session_id,
  project,
  platform_source,
  user_prompt,
  started_at,
  started_at_epoch,
  completed_at,
  completed_at_epoch,
  status,
  worker_port,
  prompt_counter,
  custom_title
)
VALUES (
  1,
  'content-session-fixture-001',
  'memory-session-fixture-001',
  '/workspace/example-repo',
  'codex',
  'Review sanitized import fixture behavior.',
  '2026-06-25T09:00:00Z',
  1782378000000,
  '2026-06-25T09:10:00Z',
  1782378600000,
  'completed',
  NULL,
  1,
  'Sanitized import fixture'
);

INSERT INTO user_prompts (
  id,
  content_session_id,
  prompt_number,
  prompt_text,
  created_at,
  created_at_epoch
)
VALUES (
  1,
  'content-session-fixture-001',
  1,
  'Please verify redaction of sk-test_fake_import_token_1234567890 in fixture import.',
  '2026-06-25T09:01:00Z',
  1782378060000
);

INSERT INTO observations (
  id,
  memory_session_id,
  project,
  text,
  type,
  title,
  subtitle,
  facts,
  narrative,
  concepts,
  files_read,
  files_modified,
  prompt_number,
  discovery_tokens,
  content_hash,
  agent_type,
  agent_id,
  merged_into_project,
  generated_by_model,
  metadata,
  created_at,
  created_at_epoch
)
VALUES (
  1,
  'memory-session-fixture-001',
  '/workspace/example-repo',
  'Importer fixture records a generated observation with file citation metadata.',
  'discovery',
  'Fixture import mapping',
  'Sanitized observation source',
  '["Fixture data is sanitized","File paths use /workspace/example-repo"]',
  'The agent reviewed a fixture file and captured import mapping notes.',
  '["migration","fixture","redaction"]',
  '[{"path":"/workspace/example-repo/src/example.py","line_start":1,"line_end":12}]',
  '[]',
  1,
  32,
  'fixture-observation-hash-001',
  'codex',
  'fixture-agent',
  NULL,
  'fake-provider/fake-model',
  '{"citations":[{"path":"/workspace/example-repo/src/example.py","line":7}],"redaction_test":true}',
  '2026-06-25T09:02:00Z',
  1782378120000
);

INSERT INTO session_summaries (
  id,
  memory_session_id,
  project,
  request,
  investigated,
  learned,
  completed,
  next_steps,
  files_read,
  files_edited,
  notes,
  prompt_number,
  discovery_tokens,
  merged_into_project,
  created_at,
  created_at_epoch
)
VALUES (
  1,
  'memory-session-fixture-001',
  '/workspace/example-repo',
  'Validate the sanitized migration fixture.',
  'Checked the minimal upstream tables and fixture layout.',
  'The importer should report deferred runtime artifacts explicitly.',
  'Created a reviewed text fixture for importer tests.',
  'Use the fixture in dry-run and apply importer tests.',
  '["/workspace/example-repo/src/example.py"]',
  '[]',
  'All data is synthetic and local paths are examples.',
  1,
  18,
  NULL,
  '2026-06-25T09:08:00Z',
  1782378480000
);

INSERT INTO pending_messages (
  id,
  session_db_id,
  content_session_id,
  tool_use_id,
  message_type,
  tool_name,
  tool_input,
  tool_response,
  cwd,
  last_user_message,
  last_assistant_message,
  prompt_number,
  status,
  created_at_epoch,
  agent_type,
  agent_id
)
VALUES (
  1,
  1,
  'content-session-fixture-001',
  'tool-use-fixture-001',
  'observation',
  'Read',
  '{"file_path":"/workspace/example-repo/src/example.py"}',
  'Sanitized tool output containing only fake fixture content.',
  '/workspace/example-repo',
  'Review the import fixture.',
  'The fixture is ready for import reporting tests.',
  1,
  'pending',
  1782378180000,
  'codex',
  'fixture-agent'
);

INSERT INTO observation_feedback (
  id,
  observation_id,
  signal_type,
  session_db_id,
  created_at_epoch,
  metadata
)
VALUES (
  1,
  1,
  'useful',
  1,
  1782378240000,
  '{"rating":"useful","source":"fixture"}'
);
