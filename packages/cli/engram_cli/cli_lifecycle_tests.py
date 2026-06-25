from __future__ import annotations

import io
import json
import stat
import tempfile
import unittest
from pathlib import Path
from typing import Any

from engram_cli.config import credential_fingerprint
from engram_cli import main


RAW_KEY = 'egk_test_cli_0123456789abcdefghijklmnopqrstuvwxyz'
PROJECT_ID = '11111111-1111-1111-1111-111111111111'
TEAM_ID = '22222222-2222-2222-2222-222222222222'


class FakeTransport:
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
            {
                'method': method,
                'url': url,
                'headers': headers,
                'payload': payload,
                'timeout': timeout,
            },
        )
        if not self.responses:
            raise AssertionError('unexpected transport call')

        return self.responses.pop(0)


def dry_run_ok(project_id: str = PROJECT_ID) -> dict[str, object]:
    return {
        'status': 'ok',
        'request_id': 'request-1',
        'resolved_actor': {'type': 'api_key', 'id': 'api-key-1'},
        'scope': {
            'organization_id': 'org-1',
            'project_ids': [project_id],
            'team_ids': [TEAM_ID],
            'capabilities': ['observations:write', 'memories:read'],
        },
        'server': {'health': 'ok'},
    }


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding='utf-8'))


class CliLifecycleTests(unittest.TestCase):
    def run_cli(
        self,
        argv: list[str],
        transport: FakeTransport,
    ) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()

        exit_code = main.main(argv, stdout=stdout, stderr=stderr, transport=transport)

        return exit_code, stdout.getvalue(), stderr.getvalue()

    def connect(
        self,
        config_dir: Path,
        responses: list[tuple[int, dict[str, object]]] | None = None,
    ) -> FakeTransport:
        transport = FakeTransport(responses or [(200, dry_run_ok()), (200, dry_run_ok())])
        exit_code, _stdout, stderr = self.run_cli(
            [
                'connect',
                '--server',
                'https://engram.example/',
                '--api-key',
                RAW_KEY,
                '--project',
                PROJECT_ID,
                '--team',
                TEAM_ID,
                '--config-dir',
                str(config_dir),
            ],
            transport,
        )
        self.assertEqual(0, exit_code, stderr)

        return transport

    def snapshot_files(self, root: Path) -> dict[str, bytes]:
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(root.rglob('*'))
            if path.is_file()
        }

    def test_connect_verifies_dry_run_then_writes_redacted_local_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            transport = FakeTransport([(200, dry_run_ok()), (200, dry_run_ok())])

            exit_code, stdout, stderr = self.run_cli(
                [
                    'connect',
                    '--server',
                    'https://engram.example/',
                    '--api-key',
                    RAW_KEY,
                    '--project',
                    PROJECT_ID,
                    '--team',
                    TEAM_ID,
                    '--config-dir',
                    str(config_dir),
                ],
                transport,
            )

            self.assertEqual(0, exit_code, stderr)
            self.assertEqual('', stderr)
            self.assertEqual(2, len(transport.calls))
            self.assertEqual(['codex', 'claude_code'], [call['payload']['agent_runtime'] for call in transport.calls])
            self.assertEqual(
                ['https://engram.example/v1/hooks/dry-run', 'https://engram.example/v1/hooks/dry-run'],
                [call['url'] for call in transport.calls],
            )
            self.assertTrue(all(call['headers']['Authorization'] == f'Bearer {RAW_KEY}' for call in transport.calls))

            config_path = config_dir / 'config.json'
            credentials_path = config_dir / 'credentials.json'
            codex_hook_path = config_dir / 'hooks' / 'codex.json'
            claude_hook_path = config_dir / 'hooks' / 'claude_code.json'
            for path in (config_path, credentials_path, codex_hook_path, claude_hook_path):
                self.assertTrue(path.exists(), path)

            config = read_json(config_path)
            credentials = read_json(credentials_path)
            codex_hook = read_json(codex_hook_path)
            claude_hook = read_json(claude_hook_path)
            public_state = f'{config} {codex_hook} {claude_hook}'

            self.assertEqual('https://engram.example', config['server_url'])
            self.assertEqual(PROJECT_ID, config['project_id'])
            self.assertEqual(TEAM_ID, config['team_id'])
            self.assertEqual(['codex', 'claude_code'], config['agent_runtimes'])
            self.assertEqual(RAW_KEY, credentials['api_key'])
            self.assertNotIn(RAW_KEY, public_state)
            self.assertEqual(0o600, stat.S_IMODE(credentials_path.stat().st_mode))
            self.assertIn('connected', stdout)
            self.assertIn(PROJECT_ID, stdout)
            self.assertIn('codex', stdout)
            self.assertIn('claude_code', stdout)
            self.assertIn('sha256:', stdout)
            self.assertNotIn(RAW_KEY, stdout)
            self.assertNotIn(RAW_KEY, stderr)

    def test_connect_fingerprint_uses_only_derived_material_for_short_keys(self) -> None:
        short_key = 'short'
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            transport = FakeTransport([(200, dry_run_ok()), (200, dry_run_ok())])

            exit_code, stdout, stderr = self.run_cli(
                [
                    'connect',
                    '--server',
                    'https://engram.example',
                    '--api-key',
                    short_key,
                    '--project',
                    PROJECT_ID,
                    '--config-dir',
                    str(config_dir),
                ],
                transport,
            )

            self.assertEqual(0, exit_code, stderr)
            self.assertEqual('', stderr)
            public_state = ' '.join(
                [
                    stdout,
                    (config_dir / 'config.json').read_text(encoding='utf-8'),
                    (config_dir / 'hooks' / 'codex.json').read_text(encoding='utf-8'),
                    (config_dir / 'hooks' / 'claude_code.json').read_text(encoding='utf-8'),
                ],
            )

            self.assertIn('sha256:', public_state)
            self.assertNotIn(short_key, public_state)
            self.assertNotIn(short_key, credential_fingerprint(short_key))

    def test_connect_writes_nothing_when_dry_run_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            transport = FakeTransport(
                [
                    (
                        403,
                        {
                            'code': 'project_scope_denied',
                            'detail': 'API key cannot access requested project',
                        },
                    ),
                ],
            )

            exit_code, stdout, stderr = self.run_cli(
                [
                    'connect',
                    '--server',
                    'https://engram.example',
                    '--api-key',
                    RAW_KEY,
                    '--project',
                    PROJECT_ID,
                    '--config-dir',
                    str(config_dir),
                ],
                transport,
            )

            self.assertEqual(1, exit_code)
            self.assertEqual('', stdout)
            self.assertIn('project_scope_denied', stderr)
            self.assertNotIn(RAW_KEY, stderr)
            self.assertEqual([], list(config_dir.rglob('*')))

    def test_connect_redacts_raw_key_from_server_error_detail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            transport = FakeTransport(
                [
                    (
                        401,
                        {
                            'code': 'invalid_key',
                            'detail': f'API key {RAW_KEY} is invalid',
                        },
                    ),
                ],
            )

            exit_code, _stdout, stderr = self.run_cli(
                [
                    'connect',
                    '--server',
                    'https://engram.example',
                    '--api-key',
                    RAW_KEY,
                    '--project',
                    PROJECT_ID,
                    '--config-dir',
                    tmp,
                ],
                transport,
            )

            self.assertEqual(1, exit_code)
            self.assertIn('invalid_key', stderr)
            self.assertIn('[REDACTED]', stderr)
            self.assertNotIn(RAW_KEY, stderr)

    def test_connect_rejects_malformed_server_url_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            exit_code, stdout, stderr = self.run_cli(
                [
                    'connect',
                    '--server',
                    'not-a-url',
                    '--api-key',
                    RAW_KEY,
                    '--project',
                    PROJECT_ID,
                    '--config-dir',
                    tmp,
                ],
                FakeTransport([]),
            )

            self.assertEqual(1, exit_code)
            self.assertEqual('', stdout)
            self.assertIn('server_unavailable', stderr)
            self.assertIn('http:// or https://', stderr)
            self.assertNotIn('Traceback', stderr)

    def test_doctor_passes_when_config_health_hooks_and_dry_run_are_valid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            before = self.snapshot_files(config_dir)
            transport = FakeTransport(
                [
                    (200, {'status': 'ok', 'checks': {'process': 'ok'}}),
                    (200, dry_run_ok()),
                    (200, dry_run_ok()),
                ],
            )

            exit_code, stdout, stderr = self.run_cli(
                ['doctor', '--config-dir', str(config_dir)],
                transport,
            )

            self.assertEqual(0, exit_code, stderr)
            self.assertIn('All required checks passed', stdout)
            self.assertEqual(before, self.snapshot_files(config_dir))
            self.assertEqual(['GET', 'POST', 'POST'], [call['method'] for call in transport.calls])
            self.assertEqual('https://engram.example/-/healthz/', transport.calls[0]['url'])

    def test_doctor_reports_missing_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            exit_code, stdout, stderr = self.run_cli(
                ['doctor', '--config-dir', tmp],
                FakeTransport([]),
            )

            self.assertEqual(1, exit_code)
            self.assertIn('missing_config', stderr)
            self.assertNotIn('All required checks passed', stdout)

    def test_doctor_reports_missing_credential(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            (config_dir / 'credentials.json').unlink()

            exit_code, _stdout, stderr = self.run_cli(
                ['doctor', '--config-dir', str(config_dir)],
                FakeTransport([]),
            )

            self.assertEqual(1, exit_code)
            self.assertIn('missing_credential', stderr)

    def test_doctor_reports_missing_hook_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            (config_dir / 'hooks' / 'codex.json').unlink()

            exit_code, _stdout, stderr = self.run_cli(
                ['doctor', '--config-dir', str(config_dir)],
                FakeTransport([]),
            )

            self.assertEqual(1, exit_code)
            self.assertIn('missing_hook_config', stderr)

    def test_doctor_reports_server_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            transport = FakeTransport(
                [
                    (503, {'status': 'unavailable', 'checks': {'process': 'unavailable'}}),
                ],
            )

            exit_code, _stdout, stderr = self.run_cli(
                ['doctor', '--config-dir', str(config_dir)],
                transport,
            )

            self.assertEqual(1, exit_code)
            self.assertIn('server_unavailable', stderr)

    def test_doctor_rejects_malformed_stored_server_url_without_transport(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            config_path = config_dir / 'config.json'
            config = read_json(config_path)
            config['server_url'] = 'not-a-url'
            config_path.write_text(json.dumps(config), encoding='utf-8')

            exit_code, _stdout, stderr = self.run_cli(
                ['doctor', '--config-dir', str(config_dir)],
                FakeTransport([]),
            )

            self.assertEqual(1, exit_code)
            self.assertIn('server_unavailable', stderr)
            self.assertIn('http:// or https://', stderr)

    def test_doctor_reports_invalid_key_from_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            transport = FakeTransport(
                [
                    (200, {'status': 'ok', 'checks': {'process': 'ok'}}),
                    (401, {'code': 'invalid_key', 'detail': 'API key is invalid'}),
                ],
            )

            exit_code, _stdout, stderr = self.run_cli(
                ['doctor', '--config-dir', str(config_dir)],
                transport,
            )

            self.assertEqual(1, exit_code)
            self.assertIn('invalid_key', stderr)
            self.assertNotIn(RAW_KEY, stderr)

    def test_disconnect_removes_only_engram_owned_state_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            self.connect(config_dir)
            keep_path = config_dir / 'keep.txt'
            keep_path.write_text('user data', encoding='utf-8')

            first_exit, first_stdout, first_stderr = self.run_cli(
                ['disconnect', '--config-dir', str(config_dir)],
                FakeTransport([]),
            )
            second_exit, second_stdout, second_stderr = self.run_cli(
                ['disconnect', '--config-dir', str(config_dir)],
                FakeTransport([]),
            )

            self.assertEqual(0, first_exit, first_stderr)
            self.assertEqual(0, second_exit, second_stderr)
            self.assertIn('disconnected', first_stdout)
            self.assertIn('nothing connected', second_stdout)
            self.assertTrue(keep_path.exists())
            self.assertFalse((config_dir / 'config.json').exists())
            self.assertFalse((config_dir / 'credentials.json').exists())
            self.assertFalse((config_dir / 'hooks' / 'codex.json').exists())
            self.assertFalse((config_dir / 'hooks' / 'claude_code.json').exists())

    def test_connect_requires_server_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            exit_code, _stdout, stderr = self.run_cli(
                ['connect', '--api-key', RAW_KEY, '--project', PROJECT_ID, '--config-dir', tmp],
                FakeTransport([]),
            )

            self.assertEqual(1, exit_code)
            self.assertIn('missing_server_url', stderr)

    def test_connect_requires_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            exit_code, _stdout, stderr = self.run_cli(
                ['connect', '--server', 'https://engram.example', '--project', PROJECT_ID, '--config-dir', tmp],
                FakeTransport([]),
            )

            self.assertEqual(1, exit_code)
            self.assertIn('missing_api_key', stderr)

    def test_connect_requires_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            exit_code, _stdout, stderr = self.run_cli(
                ['connect', '--server', 'https://engram.example', '--api-key', RAW_KEY, '--config-dir', tmp],
                FakeTransport([]),
            )

            self.assertEqual(1, exit_code)
            self.assertIn('missing_project', stderr)


if __name__ == '__main__':
    unittest.main()
