from __future__ import annotations

import json
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
README_PATH = PACKAGE_ROOT / "README.md"
PLUGIN_MANIFEST_PATH = PACKAGE_ROOT / ".claude-plugin" / "plugin.json"
HOOK_MANIFEST_PATH = PACKAGE_ROOT / "hooks" / "hooks.json"
REQUIRED_PACKAGE_FILES = (
    README_PATH,
    PLUGIN_MANIFEST_PATH,
    HOOK_MANIFEST_PATH,
)
REQUIRED_HOOK_EVENTS = ("SessionStart", "PostToolUse", "Error", "Decision", "SessionEnd", "UserPromptSubmit")


class ClaudePluginContractTests(unittest.TestCase):
    def test_claude_plugin_contract_files_exist(self) -> None:
        for path in REQUIRED_PACKAGE_FILES:
            self.assertTrue(path.exists(), path)

    def test_claude_hook_manifest_uses_bundled_hook_commands(self) -> None:
        self.assertTrue(PLUGIN_MANIFEST_PATH.exists(), PLUGIN_MANIFEST_PATH)
        self.assertTrue(HOOK_MANIFEST_PATH.exists(), HOOK_MANIFEST_PATH)

        plugin_manifest_text = PLUGIN_MANIFEST_PATH.read_text(encoding="utf-8")
        hook_manifest_text = HOOK_MANIFEST_PATH.read_text(encoding="utf-8")
        plugin_manifest = json.loads(plugin_manifest_text)
        hook_manifest = json.loads(hook_manifest_text)
        hooks = hook_manifest["hooks"]
        commands: list[str] = []

        self.assertEqual("./hooks/hooks.json", plugin_manifest["hooks"])
        self.assertNotIn("claude-mem", plugin_manifest_text)
        self.assertNotIn("claude-mem", hook_manifest_text)

        for event_name in REQUIRED_HOOK_EVENTS:
            self.assertIn(event_name, hooks)
            for matcher in hooks[event_name]:
                for hook in matcher["hooks"]:
                    if hook["type"] == "command":
                        commands.append(hook["command"])

        self.assertEqual(len(REQUIRED_HOOK_EVENTS), len(commands))
        for command in commands:
            self.assertIn("${CLAUDE_PLUGIN_ROOT}/hooks/hook.py", command)
            self.assertIn("--agent claude_code", command)
            self.assertIn("--response-format claude-code", command)
            self.assertNotIn("claude-mem", command)


if __name__ == "__main__":
    unittest.main()
