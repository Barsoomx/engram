from __future__ import annotations

import io
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

from engram_cli import main
from engram_cli.commands import run_install


RAW_KEY = "egk_test_cli_0123456789abcdefghijklmnopqrstuvwxyz"
PROJECT_ID = "11111111-1111-1111-1111-111111111111"
TEAM_ID = "22222222-2222-2222-2222-222222222222"


def dry_run_ok() -> dict[str, object]:
    return {
        "status": "ok",
        "request_id": "request-1",
        "resolved_actor": {"type": "api_key", "id": "api-key-1"},
        "scope": {
            "organization_id": "org-1",
            "project_ids": [PROJECT_ID],
            "team_ids": [TEAM_ID],
            "capabilities": ["observations:write", "memories:read"],
        },
        "server": {"health": "ok"},
    }


def health_ok() -> dict[str, object]:
    return {"status": "ok", "checks": {"process": "ok"}}


class StubTransport:
    def __init__(self, responses: list[tuple[int, dict[str, object]]]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def __call__(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        payload: dict[str, object] | None,
        timeout: float,
    ) -> tuple[int, dict[str, object]]:
        self.calls.append(
            {"method": method, "url": url, "headers": headers, "payload": payload},
        )
        if not self.responses:
            raise AssertionError("unexpected transport call")

        return self.responses.pop(0)


class StubRunner:
    def __init__(self, responses: list[tuple[int, str, str]] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str]) -> tuple[int, str, str]:
        self.calls.append(list(command))
        if self.responses:
            return self.responses.pop(0)

        return (0, "", "")


class InstallTests(unittest.TestCase):
    def install_args(self, config_dir: Path, extra: list[str] | None = None) -> Namespace:
        argv = [
            "install",
            "--server",
            "https://engram.example/",
            "--api-key",
            RAW_KEY,
            "--project",
            PROJECT_ID,
            "--team",
            TEAM_ID,
            "--config-dir",
            str(config_dir),
        ]
        argv.extend(extra or [])

        return main.build_parser().parse_args(argv)

    def run_install_command(
        self,
        args: Namespace,
        transport: StubTransport,
        runner: StubRunner,
    ) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        exit_code = run_install(
            args, stdout, stderr, transport=transport, runner=runner
        )

        return exit_code, stdout.getvalue(), stderr.getvalue()

    def test_install_happy_path_connects_installs_plugin_then_runs_doctor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            transport = StubTransport(
                [
                    (200, dry_run_ok()),
                    (200, health_ok()),
                    (200, dry_run_ok()),
                ],
            )
            runner = StubRunner()

            with mock.patch(
                "engram_cli.commands.shutil.which", return_value="/usr/bin/claude"
            ):
                exit_code, stdout, stderr = self.run_install_command(
                    self.install_args(config_dir), transport, runner
                )

            self.assertEqual(0, exit_code, stderr)
            self.assertEqual(
                [
                    [
                        "/usr/bin/claude",
                        "plugin",
                        "marketplace",
                        "add",
                        "Barsoomx/engram",
                    ],
                    [
                        "/usr/bin/claude",
                        "plugin",
                        "install",
                        "engram@engram-marketplace",
                    ],
                ],
                runner.calls,
            )
            self.assertTrue((config_dir / "config.json").exists())
            self.assertTrue((config_dir / "credentials.json").exists())
            self.assertTrue((config_dir / "hooks" / "claude_code.json").exists())
            self.assertIn("All required checks passed", stdout)
            self.assertNotIn(RAW_KEY, stdout)
            self.assertNotIn(RAW_KEY, stderr)

    def test_install_reports_claude_cli_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            transport = StubTransport([(200, dry_run_ok())])
            runner = StubRunner()

            with mock.patch(
                "engram_cli.commands.shutil.which", return_value=None
            ):
                exit_code, _stdout, stderr = self.run_install_command(
                    self.install_args(config_dir), transport, runner
                )

            self.assertEqual(1, exit_code)
            self.assertIn("claude_cli_not_found", stderr)
            self.assertEqual([], runner.calls)
            self.assertTrue((config_dir / "config.json").exists())

    def test_install_reports_plugin_install_failure_with_redacted_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            transport = StubTransport([(200, dry_run_ok())])
            runner = StubRunner([(1, "", "marketplace source unreachable")])

            with mock.patch(
                "engram_cli.commands.shutil.which", return_value="/usr/bin/claude"
            ):
                exit_code, _stdout, stderr = self.run_install_command(
                    self.install_args(config_dir), transport, runner
                )

            self.assertEqual(1, exit_code)
            self.assertIn("plugin_install_failed", stderr)
            self.assertIn("marketplace source unreachable", stderr)
            self.assertEqual(1, len(runner.calls))
            self.assertNotIn(RAW_KEY, stderr)

    def test_install_bad_key_fails_at_connect_without_running_plugin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            transport = StubTransport(
                [(401, {"code": "invalid_key", "detail": "API key is invalid"})]
            )
            runner = StubRunner()

            with mock.patch(
                "engram_cli.commands.shutil.which", return_value="/usr/bin/claude"
            ) as m_which:
                exit_code, _stdout, stderr = self.run_install_command(
                    self.install_args(config_dir), transport, runner
                )

            self.assertEqual(1, exit_code)
            self.assertIn("invalid_key", stderr)
            self.assertEqual([], runner.calls)
            m_which.assert_not_called()
            self.assertEqual([], list(config_dir.rglob("*")))

    def test_install_skip_plugin_install_runs_connect_and_doctor_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            transport = StubTransport(
                [
                    (200, dry_run_ok()),
                    (200, health_ok()),
                    (200, dry_run_ok()),
                ],
            )
            runner = StubRunner()

            exit_code, stdout, stderr = self.run_install_command(
                self.install_args(config_dir, ["--skip-plugin-install"]),
                transport,
                runner,
            )

            self.assertEqual(0, exit_code, stderr)
            self.assertEqual([], runner.calls)
            self.assertIn("All required checks passed", stdout)
            self.assertTrue((config_dir / "config.json").exists())

    def test_install_is_idempotent_across_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            transport = StubTransport(
                [
                    (200, dry_run_ok()),
                    (200, health_ok()),
                    (200, dry_run_ok()),
                    (200, dry_run_ok()),
                    (200, health_ok()),
                    (200, dry_run_ok()),
                ],
            )
            runner = StubRunner()

            with mock.patch(
                "engram_cli.commands.shutil.which", return_value="/usr/bin/claude"
            ):
                first, _stdout1, stderr1 = self.run_install_command(
                    self.install_args(config_dir), transport, runner
                )
                second, _stdout2, stderr2 = self.run_install_command(
                    self.install_args(config_dir), transport, runner
                )

            self.assertEqual(0, first, stderr1)
            self.assertEqual(0, second, stderr2)
            self.assertEqual(4, len(runner.calls))
            self.assertTrue((config_dir / "config.json").exists())
            self.assertTrue((config_dir / "credentials.json").exists())

    def test_install_dispatches_through_main_entrypoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            transport = StubTransport(
                [
                    (200, dry_run_ok()),
                    (200, health_ok()),
                    (200, dry_run_ok()),
                ],
            )
            stdout = io.StringIO()
            stderr = io.StringIO()

            exit_code = main.main(
                [
                    "install",
                    "--server",
                    "https://engram.example/",
                    "--api-key",
                    RAW_KEY,
                    "--project",
                    PROJECT_ID,
                    "--config-dir",
                    str(config_dir),
                    "--skip-plugin-install",
                ],
                stdout=stdout,
                stderr=stderr,
                transport=transport,
            )

            self.assertEqual(0, exit_code, stderr.getvalue())
            self.assertIn("All required checks passed", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
