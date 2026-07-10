from __future__ import annotations

import json
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parents[1]
SOURCE_DIR = REPO_ROOT / 'packages' / 'cli' / 'engram_cli'
BUNDLE_DIR = PACKAGE_ROOT / 'hooks' / 'engram_cli'
HOOK_MANIFEST_PATH = PACKAGE_ROOT / 'hooks' / 'hooks.json'
HOOK_ENTRYPOINT = '$PLUGIN_ROOT/hooks/hook.py'


def runtime_module_names() -> tuple[str, ...]:
    return tuple(
        sorted(
            path.name
            for path in SOURCE_DIR.iterdir()
            if path.is_file()
            and path.suffix == '.py'
            and not path.name.endswith('_tests.py')
        )
    )


class BundleSyncTests(unittest.TestCase):
    def test_bundled_files_byte_match_canonical_cli(self) -> None:
        self.assertTrue(BUNDLE_DIR.is_dir(), BUNDLE_DIR)
        for name in runtime_module_names():
            bundled = BUNDLE_DIR / name
            self.assertTrue(bundled.is_file(), bundled)
            self.assertEqual((SOURCE_DIR / name).read_bytes(), bundled.read_bytes(), name)

    def test_bundle_has_no_extra_or_test_modules(self) -> None:
        self.assertTrue(BUNDLE_DIR.is_dir(), BUNDLE_DIR)
        bundled_files = sorted(path.name for path in BUNDLE_DIR.iterdir() if path.is_file())

        self.assertEqual(sorted(runtime_module_names()), bundled_files)
        for name in bundled_files:
            self.assertFalse(name.endswith('_tests.py'), name)

    def test_hook_and_mcp_shims_launch_bundled_cli(self) -> None:
        hook_shim = PACKAGE_ROOT / 'hooks' / 'hook.py'
        mcp_shim = PACKAGE_ROOT / 'hooks' / 'mcp.py'

        self.assertTrue(hook_shim.is_file(), hook_shim)
        self.assertTrue(mcp_shim.is_file(), mcp_shim)
        for path in (hook_shim, mcp_shim):
            text = path.read_text(encoding='utf-8')
            self.assertIn('from engram_cli.main import main', text)
            self.assertNotIn('claude-mem', text.lower())

        self.assertIn('main()', hook_shim.read_text(encoding='utf-8'))
        mcp_shim_text = mcp_shim.read_text(encoding='utf-8')
        self.assertIn('main(["mcp", "serve"])', mcp_shim_text)
        self.assertIn('os.environ["ENGRAM_MCP_CODEX_SCOPE"] = "1"', mcp_shim_text)

    def test_hook_commands_reference_bundled_entrypoint(self) -> None:
        self.assertTrue(HOOK_MANIFEST_PATH.is_file(), HOOK_MANIFEST_PATH)
        hook_manifest = json.loads(HOOK_MANIFEST_PATH.read_text(encoding='utf-8'))
        commands = [
            hook['command']
            for matchers in hook_manifest['hooks'].values()
            for matcher in matchers
            for hook in matcher['hooks']
            if hook['type'] == 'command'
        ]

        self.assertEqual(4, len(commands))
        for command in commands:
            self.assertIn(HOOK_ENTRYPOINT, command)


if __name__ == '__main__':
    unittest.main()
