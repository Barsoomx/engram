from __future__ import annotations

import json
import os
import unittest
from datetime import datetime

from engram_cli.commands import (
    build_generic_hook_payload,
    build_session_start_hook_payload,
    build_session_start_payload,
    build_user_prompt_submit_payload,
    format_hook_response,
)

CONFIG: dict[str, object] = {"project_id": "", "team_id": "", "agent_version": ""}
REPO = "git@github.com:acme/engram.git"


class HookPayloadTests(unittest.TestCase):
    def test_empty_payload_gets_nonempty_fallback_with_prompt(self) -> None:
        built = build_generic_hook_payload(
            CONFIG,
            "claude_code",
            {"session_id": "s1", "repository_url": REPO, "prompt": "learn the repo"},
            "user_prompt_submit",
        )

        self.assertTrue(built["payload"])
        self.assertEqual("user_prompt_submit", built["payload"]["trigger"])
        self.assertEqual("learn the repo", built["payload"]["prompt"])

    def test_empty_payload_captures_tool_fields(self) -> None:
        built = build_generic_hook_payload(
            CONFIG,
            "claude_code",
            {
                "session_id": "s1",
                "repository_url": REPO,
                "tool_name": "Read",
                "tool_input": {"file_path": "a.py"},
            },
            "post_tool_use",
        )

        self.assertEqual("post_tool_use", built["payload"]["trigger"])
        self.assertEqual("Read", built["payload"]["tool_name"])
        self.assertEqual({"file_path": "a.py"}, built["payload"]["tool_input"])

    def test_explicit_payload_is_preserved(self) -> None:
        built = build_generic_hook_payload(
            CONFIG,
            "claude_code",
            {"session_id": "s1", "repository_url": REPO, "payload": {"custom": 1}},
            "post_tool_use",
        )

        self.assertEqual({"custom": 1}, built["payload"])


class ObservationSynthesisTests(unittest.TestCase):
    def test_user_prompt_submit_body_carries_prompt(self) -> None:
        built = build_generic_hook_payload(
            CONFIG,
            "claude_code",
            {"session_id": "s1", "repository_url": REPO, "prompt": "learn the repo"},
            "user_prompt_submit",
        )

        observation = built["observation"]
        self.assertEqual("user_prompt_submit", observation["type"])
        self.assertIn("learn the repo", observation["body"])

    def test_post_tool_use_read_body_and_files_read(self) -> None:
        built = build_generic_hook_payload(
            CONFIG,
            "claude_code",
            {
                "session_id": "s1",
                "repository_url": REPO,
                "tool_name": "Read",
                "tool_input": {"file_path": "/repo/a.py"},
                "tool_response": {"output": "file contents"},
            },
            "post_tool_use",
        )

        observation = built["observation"]
        self.assertEqual("post_tool_use", observation["type"])
        self.assertIn("Read", observation["body"])
        self.assertIn("/repo/a.py", observation["body"])
        self.assertIn("file contents", observation["body"])
        self.assertEqual(["/repo/a.py"], observation["files_read"])
        self.assertNotIn("files_modified", observation)

    def test_write_tools_report_files_modified(self) -> None:
        for tool_name in ("Edit", "Write", "MultiEdit"):
            built = build_generic_hook_payload(
                CONFIG,
                "claude_code",
                {
                    "session_id": "s1",
                    "repository_url": REPO,
                    "tool_name": tool_name,
                    "tool_input": {"file_path": "/repo/b.py"},
                },
                "post_tool_use",
            )

            observation = built["observation"]
            self.assertEqual(["/repo/b.py"], observation["files_modified"])
            self.assertNotIn("files_read", observation)

    def test_notebook_edit_uses_notebook_path(self) -> None:
        built = build_generic_hook_payload(
            CONFIG,
            "claude_code",
            {
                "session_id": "s1",
                "repository_url": REPO,
                "tool_name": "NotebookEdit",
                "tool_input": {"notebook_path": "/repo/n.ipynb"},
            },
            "post_tool_use",
        )

        self.assertEqual(["/repo/n.ipynb"], built["observation"]["files_modified"])

    def test_body_truncated_to_server_limit(self) -> None:
        built = build_generic_hook_payload(
            CONFIG,
            "claude_code",
            {
                "session_id": "s1",
                "repository_url": REPO,
                "tool_name": "Bash",
                "tool_response": {"output": "x" * 50000},
            },
            "post_tool_use",
        )

        self.assertLessEqual(len(built["observation"]["body"]), 16000)

    def test_no_observation_when_nothing_to_describe(self) -> None:
        built = build_generic_hook_payload(
            CONFIG,
            "claude_code",
            {"session_id": "s1", "repository_url": REPO},
            "session_end",
        )

        self.assertNotIn("observation", built)

    def test_explicit_observation_wins_over_synthesis(self) -> None:
        built = build_generic_hook_payload(
            CONFIG,
            "claude_code",
            {
                "session_id": "s1",
                "repository_url": REPO,
                "prompt": "ignored",
                "observation": {"type": "custom", "body": "explicit"},
            },
            "user_prompt_submit",
        )

        self.assertEqual({"type": "custom", "body": "explicit"}, built["observation"])

    def test_session_start_source_lands_in_body(self) -> None:
        built = build_session_start_hook_payload(
            CONFIG,
            "claude_code",
            {"session_id": "s1", "repository_url": REPO, "source": "startup"},
        )

        self.assertIn("startup", built["observation"]["body"])


class OccurredAtTests(unittest.TestCase):
    def test_occurred_at_is_iso_datetime(self) -> None:
        built = build_generic_hook_payload(
            CONFIG,
            "claude_code",
            {"session_id": "s1", "repository_url": REPO, "prompt": "hi"},
            "user_prompt_submit",
        )

        datetime.fromisoformat(str(built["occurred_at"]))

    def test_occurred_at_does_not_break_idempotency(self) -> None:
        hook_input: dict[str, object] = {
            "session_id": "s1",
            "repository_url": REPO,
            "prompt": "hi",
        }
        first = build_generic_hook_payload(
            CONFIG, "claude_code", hook_input, "user_prompt_submit"
        )
        second = build_generic_hook_payload(
            CONFIG, "claude_code", hook_input, "user_prompt_submit"
        )

        self.assertEqual(first["content_hash"], second["content_hash"])
        self.assertEqual(first["event_id"], second["event_id"])

    def test_explicit_occurred_at_preserved(self) -> None:
        built = build_generic_hook_payload(
            CONFIG,
            "claude_code",
            {
                "session_id": "s1",
                "repository_url": REPO,
                "prompt": "hi",
                "occurred_at": "2026-07-02T10:00:00+00:00",
            },
            "user_prompt_submit",
        )

        self.assertEqual("2026-07-02T10:00:00+00:00", built["occurred_at"])


class CodexOccurrenceIdentityTests(unittest.TestCase):
    def test_turn_id_distinguishes_repeated_prompt_events(self) -> None:
        first = build_generic_hook_payload(
            CONFIG,
            "codex",
            {
                "session_id": "s1",
                "repository_url": REPO,
                "turn_id": "turn-1",
                "hook_event_name": "UserPromptSubmit",
                "prompt": "run the tests",
            },
            "user_prompt_submit",
        )
        second = build_generic_hook_payload(
            CONFIG,
            "codex",
            {
                "session_id": "s1",
                "repository_url": REPO,
                "turn_id": "turn-2",
                "hook_event_name": "UserPromptSubmit",
                "prompt": "run the tests",
            },
            "user_prompt_submit",
        )

        self.assertEqual("turn-1", first["payload"]["turn_id"])
        self.assertNotEqual(first["event_id"], second["event_id"])

    def test_tool_use_id_distinguishes_repeated_tool_events(self) -> None:
        base_input: dict[str, object] = {
            "session_id": "s1",
            "repository_url": REPO,
            "turn_id": "turn-1",
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "pytest"},
            "tool_response": {"exit_code": 0, "output": "ok"},
        }
        first = build_generic_hook_payload(
            CONFIG,
            "codex",
            {**base_input, "tool_use_id": "tool-1"},
            "post_tool_use",
        )
        second = build_generic_hook_payload(
            CONFIG,
            "codex",
            {**base_input, "tool_use_id": "tool-2"},
            "post_tool_use",
        )

        self.assertEqual("tool-1", first["payload"]["tool_use_id"])
        self.assertNotEqual(first["event_id"], second["event_id"])

    def test_stop_captures_last_assistant_message(self) -> None:
        built = build_generic_hook_payload(
            CONFIG,
            "codex",
            {
                "session_id": "s1",
                "repository_url": REPO,
                "turn_id": "turn-1",
                "hook_event_name": "Stop",
                "last_assistant_message": "Tests pass; deploy remains pending.",
            },
            "session_end",
        )

        self.assertIn("deploy remains pending", built["observation"]["body"])
        self.assertEqual("turn-1", built["payload"]["turn_id"])


class PayloadSizeBoundTests(unittest.TestCase):
    def test_oversized_tool_input_replaced_with_preview(self) -> None:
        built = build_generic_hook_payload(
            CONFIG,
            "claude_code",
            {
                "session_id": "s1",
                "repository_url": REPO,
                "tool_name": "Write",
                "tool_input": {"file_path": "/repo/big.py", "content": "y" * 100000},
            },
            "post_tool_use",
        )

        payload = built["payload"]
        self.assertNotIn("tool_input", payload)
        self.assertIn("tool_input_preview", payload)
        self.assertLessEqual(len(json.dumps(payload)), 65536)

    def test_small_tool_input_kept_verbatim(self) -> None:
        built = build_generic_hook_payload(
            CONFIG,
            "claude_code",
            {
                "session_id": "s1",
                "repository_url": REPO,
                "tool_name": "Read",
                "tool_input": {"file_path": "/repo/a.py"},
            },
            "post_tool_use",
        )

        self.assertEqual({"file_path": "/repo/a.py"}, built["payload"]["tool_input"])


if __name__ == "__main__":
    unittest.main()


class SearchPayloadTests(unittest.TestCase):
    def test_project_scoped_config_sends_project_id(self) -> None:
        from engram_cli.commands import build_search_payload

        payload = build_search_payload(
            {"project_id": "proj-1", "team_id": "team-1"},
            query="auth",
            file_paths=[],
            symbols=[],
            limit=5,
            project_id="proj-1",
            repository_url="git@github.com:acme/x.git",
        )

        self.assertEqual("proj-1", payload["project_id"])
        self.assertNotIn("repository_url", payload)

    def test_org_wide_config_sends_repository_url(self) -> None:
        from engram_cli.commands import build_search_payload

        payload = build_search_payload(
            {"project_id": "", "team_id": ""},
            query="auth",
            file_paths=["a.py"],
            symbols=[],
            limit=3,
            project_id="",
            repository_url="git@github.com:acme/x.git",
        )

        self.assertNotIn("project_id", payload)
        self.assertEqual("git@github.com:acme/x.git", payload["repository_url"])
        self.assertEqual(3, payload["limit"])


class UserPromptSubmitPayloadTests(unittest.TestCase):
    def test_query_falls_back_to_prompt_when_query_missing(self) -> None:
        built = build_user_prompt_submit_payload(
            CONFIG,
            "claude_code",
            {"session_id": "s1", "repository_url": REPO, "prompt": "how does auth work"},
        )

        self.assertEqual("how does auth work", built["query"])

    def test_explicit_query_wins_over_prompt(self) -> None:
        built = build_user_prompt_submit_payload(
            CONFIG,
            "claude_code",
            {
                "session_id": "s1",
                "repository_url": REPO,
                "query": "explicit query",
                "prompt": "ignored prompt",
            },
        )

        self.assertEqual("explicit query", built["query"])

    def test_defaults_token_budget_to_1200_when_not_provided(self) -> None:
        built = build_user_prompt_submit_payload(
            CONFIG,
            "claude_code",
            {"session_id": "s1", "repository_url": REPO, "query": "q"},
        )

        self.assertEqual(1200, built["token_budget"])

    def test_explicit_token_budget_is_preserved(self) -> None:
        built = build_user_prompt_submit_payload(
            CONFIG,
            "claude_code",
            {
                "session_id": "s1",
                "repository_url": REPO,
                "query": "q",
                "token_budget": 400,
            },
        )

        self.assertEqual(400, built["token_budget"])


class SessionStartQueryPayloadTests(unittest.TestCase):
    def test_query_stays_empty_without_prompt_fallback(self) -> None:
        built = build_session_start_payload(
            CONFIG,
            "claude_code",
            {"session_id": "s1", "repository_url": REPO, "prompt": "should not be used"},
        )

        self.assertEqual("", built["query"])

    def test_defaults_token_budget_to_2000_when_not_provided(self) -> None:
        built = build_session_start_payload(
            CONFIG,
            "claude_code",
            {"session_id": "s1", "repository_url": REPO},
        )

        self.assertEqual(2000, built["token_budget"])

    def test_explicit_token_budget_is_preserved(self) -> None:
        built = build_session_start_payload(
            CONFIG,
            "claude_code",
            {"session_id": "s1", "repository_url": REPO, "token_budget": 500},
        )

        self.assertEqual(500, built["token_budget"])


class FormatHookResponseEmptyInjectionTests(unittest.TestCase):
    def test_user_prompt_submit_claude_code_empty_items_returns_empty_dict(self) -> None:
        result = format_hook_response(
            {"status": "created", "items": [], "rendered_context": ""},
            "claude-code",
            "user-prompt-submit",
        )

        self.assertEqual({}, result)

    def test_user_prompt_submit_default_format_empty_items_returns_continue_only(
        self,
    ) -> None:
        result = format_hook_response(
            {"status": "created", "items": [], "rendered_context": ""},
            "codex",
            "user-prompt-submit",
        )

        self.assertEqual({"continue": True}, result)

    def test_user_prompt_submit_nonempty_items_still_injects(self) -> None:
        result = format_hook_response(
            {
                "status": "created",
                "items": [{"citation": "M1"}],
                "rendered_context": "Relevant Engram context",
            },
            "claude-code",
            "user-prompt-submit",
        )

        self.assertNotIn("systemMessage", result)
        self.assertEqual(
            {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": "Relevant Engram context",
            },
            result["hookSpecificOutput"],
        )

    def test_user_prompt_submit_default_format_nonempty_items_has_no_system_message(
        self,
    ) -> None:
        result = format_hook_response(
            {
                "status": "created",
                "items": [{"citation": "M1"}],
                "rendered_context": "Relevant Engram context",
            },
            "codex",
            "user-prompt-submit",
        )

        self.assertTrue(result["continue"])
        self.assertNotIn("systemMessage", result)
        self.assertEqual(
            {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": "Relevant Engram context",
            },
            result["hookSpecificOutput"],
        )

    def test_session_start_empty_items_emits_friendly_message_only(self) -> None:
        result = format_hook_response(
            {"status": "created", "items": [], "rendered_context": ""},
            "claude-code",
            "session-start",
        )

        self.assertEqual({"systemMessage": "Engram: no project memory yet."}, result)

    def test_session_start_empty_items_default_format_includes_continue(self) -> None:
        result = format_hook_response(
            {"status": "created", "items": [], "rendered_context": ""},
            "codex",
            "session-start",
        )

        self.assertEqual(
            {"continue": True, "systemMessage": "Engram: no project memory yet."},
            result,
        )

    def test_session_start_nonempty_items_renders_compact_model_index(self) -> None:
        result = format_hook_response(
            {
                "status": "created",
                "rendered_context": "Relevant Engram context",
                "items": [
                    {
                        "citation": "M1",
                        "title": "Realtime candidate gating",
                        "body": "Realtime confidence is a metadata heuristic capped at 0.6.",
                        "kind": "decision",
                        "confidence": "0.95",
                    },
                    {
                        "citation": "M2",
                        "title": "Reconciler drops stale sessions",
                        "body": "Sessions stuck for hours never resolve.",
                        "kind": "gotcha",
                        "confidence": None,
                    },
                    {
                        "citation": "M3",
                        "title": "No kind or confidence recorded",
                        "body": "Plain memory without annotation.",
                        "kind": "",
                        "confidence": None,
                    },
                ],
            },
            "claude-code",
            "session-start",
        )

        self.assertEqual(
            "\n".join(
                [
                    "# Engram context — 3 memories for this project",
                    "",
                    "- [M1] Realtime candidate gating (decision, confidence 0.95)",
                    "  Realtime confidence is a metadata heuristic capped at 0.6.",
                    "- [M2] Reconciler drops stale sessions (gotcha)",
                    "  Sessions stuck for hours never resolve.",
                    "- [M3] No kind or confidence recorded",
                    "  Plain memory without annotation.",
                    "",
                    "Before non-trivial tasks, search deeper with the engram_search "
                    "MCP tool. If any memory above is wrong or outdated, mark it via "
                    "engram_memory_feedback.",
                ]
            ),
            result["hookSpecificOutput"]["additionalContext"],
        )
        self.assertEqual("SessionStart", result["hookSpecificOutput"]["hookEventName"])
        self.assertEqual(
            "Engram: 3 memories injected (1 decision, 1 gotcha, 1 other) "
            "— search deeper: engram_search",
            result["systemMessage"],
        )

    def test_session_start_human_summary_orders_kinds_by_count_then_first_seen(
        self,
    ) -> None:
        result = format_hook_response(
            {
                "status": "created",
                "rendered_context": "Relevant Engram context",
                "items": [
                    {"citation": "M1", "title": "A", "body": "a", "kind": "digest"},
                    {"citation": "M2", "title": "B", "body": "b", "kind": "decision"},
                    {"citation": "M3", "title": "C", "body": "c", "kind": "digest"},
                    {"citation": "M4", "title": "D", "body": "d", "kind": "decision"},
                    {"citation": "M5", "title": "E", "body": "e", "kind": "gotcha"},
                ],
            },
            "claude-code",
            "session-start",
        )

        self.assertEqual(
            "Engram: 5 memories injected (2 digest, 2 decision, 1 gotcha) "
            "— search deeper: engram_search",
            result["systemMessage"],
        )

    def test_session_start_item_body_truncated_to_400_chars(self) -> None:
        long_body = "a" * 450
        result = format_hook_response(
            {
                "status": "created",
                "rendered_context": "Relevant Engram context",
                "items": [
                    {
                        "citation": "M1",
                        "title": "Long memory",
                        "body": long_body,
                        "kind": "note",
                        "confidence": "0.5",
                    },
                ],
            },
            "claude-code",
            "session-start",
        )

        additional_context = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn(f"  {'a' * 400}…", additional_context)
        self.assertNotIn("a" * 401, additional_context)

    def test_session_start_default_format_nonempty_items_includes_continue(
        self,
    ) -> None:
        result = format_hook_response(
            {
                "status": "created",
                "rendered_context": "Relevant Engram context",
                "items": [
                    {
                        "citation": "M1",
                        "title": "T",
                        "body": "B",
                        "kind": "",
                        "confidence": None,
                    },
                ],
            },
            "codex",
            "session-start",
        )

        self.assertTrue(result["continue"])
        self.assertEqual(
            "Engram: 1 memories injected (1 other) — search deeper: engram_search",
            result["systemMessage"],
        )
        self.assertIn(
            "# Engram context — 1 memories for this project",
            result["hookSpecificOutput"]["additionalContext"],
        )

    def test_server_response_format_passes_body_through_unchanged(self) -> None:
        body = {"status": "created", "items": [], "rendered_context": ""}
        result = format_hook_response(body, "server", "user-prompt-submit")

        self.assertEqual(body, result)


class HookProjectLadderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env_project_id = os.environ.pop("ENGRAM_PROJECT_ID", None)

    def tearDown(self) -> None:
        if self._env_project_id is not None:
            os.environ["ENGRAM_PROJECT_ID"] = self._env_project_id
        else:
            os.environ.pop("ENGRAM_PROJECT_ID", None)

    def test_harness_input_project_id_wins_over_env_and_config(self) -> None:
        os.environ["ENGRAM_PROJECT_ID"] = "env-project"
        config = {"project_id": "config-project", "team_id": "", "agent_version": ""}
        built = build_generic_hook_payload(
            config,
            "claude_code",
            {
                "session_id": "s1",
                "project_id": "harness-project",
                "repository_url": REPO,
            },
            "post_tool_use",
        )

        self.assertEqual("harness-project", built["project_id"])

    def test_env_project_id_wins_over_config_when_no_harness_input(self) -> None:
        os.environ["ENGRAM_PROJECT_ID"] = "env-project"
        config = {"project_id": "config-project", "team_id": "", "agent_version": ""}
        built = build_generic_hook_payload(
            config,
            "claude_code",
            {"session_id": "s1", "repository_url": REPO},
            "post_tool_use",
        )

        self.assertEqual("env-project", built["project_id"])

    def test_config_project_id_used_when_no_harness_input_or_env(self) -> None:
        config = {"project_id": "config-project", "team_id": "", "agent_version": ""}
        built = build_generic_hook_payload(
            config,
            "claude_code",
            {"session_id": "s1", "repository_url": REPO},
            "post_tool_use",
        )

        self.assertEqual("config-project", built["project_id"])
