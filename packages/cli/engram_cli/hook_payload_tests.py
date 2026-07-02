from __future__ import annotations

import unittest

from engram_cli.commands import build_generic_hook_payload

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


if __name__ == "__main__":
    unittest.main()
