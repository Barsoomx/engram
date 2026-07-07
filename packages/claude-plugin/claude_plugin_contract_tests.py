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
REQUIRED_HOOK_EVENTS = ("SessionStart", "PostToolUse", "SessionEnd", "UserPromptSubmit")
MCP_MANIFEST_PATH = PACKAGE_ROOT / ".mcp.json"
MCP_SHIM_PATH = PACKAGE_ROOT / "hooks" / "mcp.py"
BUNDLED_MCP_MODULES = (
    PACKAGE_ROOT / "hooks" / "engram_cli" / "mcp_server.py",
    PACKAGE_ROOT / "hooks" / "engram_cli" / "mcp_tools.py",
)


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

        self.assertNotIn("hooks", plugin_manifest)
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

    def test_session_start_matcher_covers_clear_and_compact(self) -> None:
        hook_manifest = json.loads(HOOK_MANIFEST_PATH.read_text(encoding="utf-8"))
        matcher = hook_manifest["hooks"]["SessionStart"][0]["matcher"]

        self.assertEqual("startup|resume|clear|compact", matcher)

    def test_claude_plugin_ships_mcp_server(self) -> None:
        self.assertTrue(MCP_MANIFEST_PATH.exists(), MCP_MANIFEST_PATH)
        manifest = json.loads(MCP_MANIFEST_PATH.read_text(encoding="utf-8"))
        entry = manifest["mcpServers"]["engram"]

        self.assertEqual("python3", entry["command"])
        self.assertEqual(["${CLAUDE_PLUGIN_ROOT}/hooks/mcp.py"], entry["args"])
        self.assertNotIn("env", entry)
        self.assertTrue(MCP_SHIM_PATH.exists(), MCP_SHIM_PATH)
        shim_text = MCP_SHIM_PATH.read_text(encoding="utf-8")
        self.assertIn('main(["mcp", "serve"])', shim_text)
        for path in BUNDLED_MCP_MODULES:
            self.assertTrue(path.exists(), path)

    def test_plugin_versions_match(self) -> None:
        plugin_version = json.loads(
            PLUGIN_MANIFEST_PATH.read_text(encoding="utf-8")
        )["version"]
        marketplace = json.loads(
            (PACKAGE_ROOT.parents[1] / ".claude-plugin" / "marketplace.json").read_text(
                encoding="utf-8"
            )
        )

        self.assertEqual(plugin_version, marketplace["plugins"][0]["version"])


if __name__ == "__main__":
    unittest.main()
