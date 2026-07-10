from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parents[1]
PLUGIN_MANIFEST_PATH = PACKAGE_ROOT / '.codex-plugin' / 'plugin.json'
HOOK_MANIFEST_PATH = PACKAGE_ROOT / 'hooks' / 'hooks.json'
MCP_MANIFEST_PATH = PACKAGE_ROOT / '.mcp.json'
MARKETPLACE_MANIFEST_PATH = REPO_ROOT / '.agents' / 'plugins' / 'marketplace.json'
EXPECTED_SKILLS = ('how-it-works', 'learn-codebase', 'mem-search')
EXPECTED_HOOKS = {
    'SessionStart': [
        {
            'matcher': 'startup|resume|clear|compact',
            'hooks': [
                {
                    'type': 'command',
                    'command': (
                        'python3 "$PLUGIN_ROOT/hooks/hook.py" hook session-start '
                        '--agent codex --response-format codex'
                    ),
                    'timeout': 60,
                }
            ],
        }
    ],
    'UserPromptSubmit': [
        {
            'hooks': [
                {
                    'type': 'command',
                    'command': (
                        'python3 "$PLUGIN_ROOT/hooks/hook.py" hook user-prompt-submit '
                        '--agent codex --response-format codex'
                    ),
                    'timeout': 60,
                }
            ],
        }
    ],
    'PostToolUse': [
        {
            'matcher': '*',
            'hooks': [
                {
                    'type': 'command',
                    'command': (
                        'python3 "$PLUGIN_ROOT/hooks/hook.py" hook post-tool-use '
                        '--agent codex --response-format codex'
                    ),
                    'timeout': 120,
                }
            ],
        }
    ],
    'Stop': [
        {
            'hooks': [
                {
                    'type': 'command',
                    'command': (
                        'python3 "$PLUGIN_ROOT/hooks/hook.py" hook session-end '
                        '--agent codex --response-format codex'
                    ),
                    'timeout': 60,
                }
            ],
        }
    ],
}
SECRET_CONFIG_KEYS = {
    'api_key',
    'authorization',
    'bearer_token',
    'bearer_token_env_var',
    'env',
    'env_http_headers',
    'env_vars',
    'http_headers',
    'password',
    'secret',
    'token',
}


def _collect_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return {
            *(str(key).lower() for key in value),
            *(key for item in value.values() for key in _collect_keys(item)),
        }
    if isinstance(value, list):
        return {key for item in value for key in _collect_keys(item)}

    return set()


class CodexPluginContractTests(unittest.TestCase):
    def test_codex_plugin_uses_default_native_layout(self) -> None:
        required_paths = (
            PACKAGE_ROOT / 'README.md',
            PLUGIN_MANIFEST_PATH,
            HOOK_MANIFEST_PATH,
            MCP_MANIFEST_PATH,
            PACKAGE_ROOT / 'hooks' / 'hook.py',
            PACKAGE_ROOT / 'hooks' / 'mcp.py',
        )

        for path in required_paths:
            self.assertTrue(path.is_file(), path)

        manifest = json.loads(PLUGIN_MANIFEST_PATH.read_text(encoding='utf-8'))
        self.assertNotIn('hooks', manifest)

    def test_plugin_manifest_has_publishable_metadata_and_real_paths(self) -> None:
        self.assertTrue(PLUGIN_MANIFEST_PATH.is_file(), PLUGIN_MANIFEST_PATH)
        manifest = json.loads(PLUGIN_MANIFEST_PATH.read_text(encoding='utf-8'))
        required_keys = {
            'name',
            'version',
            'description',
            'author',
            'homepage',
            'repository',
            'license',
            'skills',
            'mcpServers',
            'interface',
        }

        self.assertFalse(required_keys - set(manifest))

        self.assertEqual('engram', manifest['name'])
        for key in ('version', 'description', 'homepage', 'repository', 'license'):
            self.assertIsInstance(manifest[key], str, key)
            self.assertTrue(manifest[key].strip(), key)

        self.assertEqual('Engram', manifest['author']['name'])
        self.assertEqual('./skills/', manifest['skills'])
        self.assertEqual('./.mcp.json', manifest['mcpServers'])

        interface = manifest['interface']
        self.assertEqual('Engram', interface['displayName'])
        self.assertEqual('Engram', interface['developerName'])
        for key in ('shortDescription', 'longDescription', 'category', 'websiteURL'):
            self.assertIsInstance(interface[key], str, key)
            self.assertTrue(interface[key].strip(), key)

        default_prompts = interface['defaultPrompt']
        self.assertGreaterEqual(len(default_prompts), 1)
        self.assertLessEqual(len(default_prompts), 3)
        for prompt in default_prompts:
            self.assertIsInstance(prompt, str)
            self.assertTrue(prompt.strip())
            self.assertLessEqual(len(prompt), 128)

        for key in ('skills', 'mcpServers'):
            relative_path = manifest[key]
            self.assertTrue(relative_path.startswith('./'), relative_path)
            self.assertTrue((PACKAGE_ROOT / relative_path).exists(), relative_path)

    def test_hook_manifest_matches_only_native_codex_events(self) -> None:
        self.assertTrue(HOOK_MANIFEST_PATH.is_file(), HOOK_MANIFEST_PATH)
        hook_manifest = json.loads(HOOK_MANIFEST_PATH.read_text(encoding='utf-8'))

        self.assertEqual(EXPECTED_HOOKS, hook_manifest['hooks'])

    def test_codex_plugin_ships_secret_free_bundled_mcp_server(self) -> None:
        self.assertTrue(MCP_MANIFEST_PATH.is_file(), MCP_MANIFEST_PATH)
        manifest = json.loads(MCP_MANIFEST_PATH.read_text(encoding='utf-8'))
        self.assertEqual(
            {
                'mcpServers': {
                    'engram': {
                        'command': 'python3',
                        'args': ['./hooks/mcp.py'],
                        'cwd': '.',
                    }
                }
            },
            manifest,
        )
        self.assertFalse(SECRET_CONFIG_KEYS & _collect_keys(manifest))

    def test_codex_plugin_ships_runtime_neutral_skill_parity(self) -> None:
        codex_skills = PACKAGE_ROOT / 'skills'
        claude_skills = REPO_ROOT / 'packages' / 'claude-plugin' / 'skills'

        self.assertTrue(codex_skills.is_dir(), codex_skills)
        self.assertEqual(
            list(EXPECTED_SKILLS),
            sorted(path.name for path in codex_skills.iterdir() if path.is_dir()),
        )
        for skill_name in EXPECTED_SKILLS:
            codex_skill = codex_skills / skill_name / 'SKILL.md'
            claude_skill = claude_skills / skill_name / 'SKILL.md'
            self.assertTrue(codex_skill.is_file(), codex_skill)
            self.assertEqual(claude_skill.read_bytes(), codex_skill.read_bytes(), skill_name)
            self.assertNotIn('claude-mem', codex_skill.read_text(encoding='utf-8').lower())

    def test_repo_marketplace_points_to_codex_package_without_secrets(self) -> None:
        self.assertTrue(MARKETPLACE_MANIFEST_PATH.is_file(), MARKETPLACE_MANIFEST_PATH)
        marketplace = json.loads(MARKETPLACE_MANIFEST_PATH.read_text(encoding='utf-8'))
        entries = [entry for entry in marketplace['plugins'] if entry['name'] == 'engram']

        self.assertEqual('engram-marketplace', marketplace['name'])
        self.assertEqual('Engram', marketplace['interface']['displayName'])
        self.assertEqual(1, len(entries))
        entry = entries[0]
        self.assertEqual(
            {'source': 'local', 'path': './packages/codex-plugin'},
            entry['source'],
        )
        self.assertEqual(
            {'installation': 'AVAILABLE', 'authentication': 'ON_INSTALL'},
            entry['policy'],
        )
        self.assertEqual('Productivity', entry['category'])
        self.assertFalse(SECRET_CONFIG_KEYS & _collect_keys(marketplace))

    def test_public_codex_manifests_do_not_reference_legacy_runtime(self) -> None:
        for path in (PLUGIN_MANIFEST_PATH, HOOK_MANIFEST_PATH, MCP_MANIFEST_PATH):
            self.assertTrue(path.is_file(), path)
            text = path.read_text(encoding='utf-8').lower()
            self.assertNotIn('claude-mem', text, path)


if __name__ == '__main__':
    unittest.main()
