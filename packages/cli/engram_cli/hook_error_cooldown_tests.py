from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engram_cli.commands import (
    HOOK_ERROR_COOLDOWN_SECONDS,
    hook_error_marker_path,
    should_emit_hook_error,
    touch_hook_error_marker,
)


class HookErrorMarkerPathTests(unittest.TestCase):
    def test_marker_path_uses_sha256_prefix_of_server_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch(
                "engram_cli.commands.tempfile.gettempdir", return_value=tmp
            ):
                marker = hook_error_marker_path("https://engram.example")

            digest = hashlib.sha256(b"https://engram.example").hexdigest()[:12]
            self.assertEqual(
                Path(tmp) / f"engram-hook-error-{digest}", marker
            )

    def test_marker_path_uses_default_suffix_when_server_url_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch(
                "engram_cli.commands.tempfile.gettempdir", return_value=tmp
            ):
                marker = hook_error_marker_path("")

            self.assertEqual(Path(tmp) / "engram-hook-error-default", marker)

    def test_different_server_urls_get_different_markers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch(
                "engram_cli.commands.tempfile.gettempdir", return_value=tmp
            ):
                first = hook_error_marker_path("https://a.example")
                second = hook_error_marker_path("https://b.example")

            self.assertNotEqual(first, second)


class HookErrorCooldownTests(unittest.TestCase):
    def test_emits_when_no_marker_exists_yet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch(
                "engram_cli.commands.tempfile.gettempdir", return_value=tmp
            ):
                self.assertTrue(should_emit_hook_error("https://engram.example"))

    def test_suppresses_immediately_after_touch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch(
                "engram_cli.commands.tempfile.gettempdir", return_value=tmp
            ):
                touch_hook_error_marker("https://engram.example")

                self.assertFalse(should_emit_hook_error("https://engram.example"))

    def test_emits_again_once_marker_is_older_than_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch(
                "engram_cli.commands.tempfile.gettempdir", return_value=tmp
            ):
                touch_hook_error_marker("https://engram.example")
                marker = hook_error_marker_path("https://engram.example")
                stale_time = os.stat(marker).st_mtime - HOOK_ERROR_COOLDOWN_SECONDS - 1
                os.utime(marker, (stale_time, stale_time))

                self.assertTrue(should_emit_hook_error("https://engram.example"))


if __name__ == "__main__":
    unittest.main()
