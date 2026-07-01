from __future__ import annotations

import json
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parents[1]
SOURCE_DIR = REPO_ROOT / "packages" / "cli" / "engram_cli"
BUNDLE_DIR = PACKAGE_ROOT / "hooks" / "engram_cli"
PLUGIN_MANIFEST_PATH = PACKAGE_ROOT / ".claude-plugin" / "plugin.json"
MARKETPLACE_MANIFEST_PATH = REPO_ROOT / ".claude-plugin" / "marketplace.json"
HOOK_MANIFEST_PATH = PACKAGE_ROOT / "hooks" / "hooks.json"
HOOK_ENTRYPOINT = "${CLAUDE_PLUGIN_ROOT}/hooks/hook.py"


def runtime_module_names() -> tuple[str, ...]:
    return tuple(
        sorted(
            path.name
            for path in SOURCE_DIR.iterdir()
            if path.is_file()
            and path.suffix == ".py"
            and not path.name.endswith("_tests.py")
        )
    )
REQUIRED_HOOK_EVENTS = ("SessionStart", "PostToolUse", "Error", "Decision", "SessionEnd", "UserPromptSubmit")


class BundleSyncTests(unittest.TestCase):
    def test_bundled_files_byte_match_source(self) -> None:
        for name in runtime_module_names():
            bundled = BUNDLE_DIR / name
            self.assertTrue(bundled.exists(), bundled)
            self.assertEqual((SOURCE_DIR / name).read_bytes(), bundled.read_bytes(), name)

    def test_bundle_has_no_extra_or_test_files(self) -> None:
        bundled_files = sorted(path.name for path in BUNDLE_DIR.iterdir() if path.is_file())
        self.assertEqual(sorted(runtime_module_names()), bundled_files)
        for name in bundled_files:
            self.assertFalse(name.endswith("_tests.py"), name)

    def test_plugin_version_matches_marketplace_version(self) -> None:
        plugin_manifest = json.loads(PLUGIN_MANIFEST_PATH.read_text(encoding="utf-8"))
        marketplace = json.loads(MARKETPLACE_MANIFEST_PATH.read_text(encoding="utf-8"))
        self.assertEqual(plugin_manifest["version"], marketplace["plugins"][0]["version"])

    def test_hook_commands_reference_bundled_entrypoint(self) -> None:
        hook_manifest = json.loads(HOOK_MANIFEST_PATH.read_text(encoding="utf-8"))
        commands: list[str] = []
        for matchers in hook_manifest["hooks"].values():
            for matcher in matchers:
                for hook in matcher["hooks"]:
                    if hook["type"] == "command":
                        commands.append(hook["command"])

        self.assertEqual(len(REQUIRED_HOOK_EVENTS), len(commands))
        for command in commands:
            self.assertIn(HOOK_ENTRYPOINT, command)


if __name__ == "__main__":
    unittest.main()
