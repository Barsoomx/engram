from __future__ import annotations

import json
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
PLUGIN_MANIFEST_PATH = PACKAGE_ROOT / '.codex-plugin' / 'plugin.json'
HOOK_MANIFEST_PATH = PACKAGE_ROOT / 'plugin' / 'hooks' / 'codex-hooks.json'
REQUIRED_HOOK_EVENTS = ('SessionStart', 'PostToolUse', 'Error', 'Decision')


class CodexPluginContractTests(unittest.TestCase):
    def test_codex_plugin_contract_files_exist(self) -> None:
        self.assertTrue(PLUGIN_MANIFEST_PATH.exists(), PLUGIN_MANIFEST_PATH)
        self.assertTrue(HOOK_MANIFEST_PATH.exists(), HOOK_MANIFEST_PATH)

    def test_codex_hook_manifest_uses_engram_codex_commands(self) -> None:
        self.assertTrue(HOOK_MANIFEST_PATH.exists(), HOOK_MANIFEST_PATH)

        hook_manifest = json.loads(HOOK_MANIFEST_PATH.read_text(encoding='utf-8'))
        hooks = hook_manifest['hooks']
        commands: list[str] = []

        for event_name in REQUIRED_HOOK_EVENTS:
            self.assertIn(event_name, hooks)
            for matcher in hooks[event_name]:
                for hook in matcher['hooks']:
                    if hook['type'] == 'command':
                        commands.append(hook['command'])

        self.assertEqual(len(REQUIRED_HOOK_EVENTS), len(commands))
        for command in commands:
            self.assertIn('engram hook', command)
            self.assertIn('--agent codex', command)
            self.assertIn('--response-format codex', command)
            self.assertNotIn('claude-mem', command)


if __name__ == '__main__':
    unittest.main()
