from __future__ import annotations

import unittest

from engram_cli.commands import build_session_start_hook_payload

CONFIG: dict[str, object] = {'project_id': '', 'team_id': '', 'agent_version': ''}
REPO = 'git@github.com:acme/engram.git'


class ModelIdFromModelIdKeyTests(unittest.TestCase):
    def test_model_id_key_lands_in_outgoing_payload(self) -> None:
        built = build_session_start_hook_payload(
            CONFIG,
            'claude_code',
            {'session_id': 's1', 'repository_url': REPO, 'model_id': 'claude-opus-4-6'},
        )

        self.assertEqual('claude-opus-4-6', built['payload']['model_id'])


class ModelIdFromModelStringTests(unittest.TestCase):
    def test_model_string_lands_in_outgoing_payload(self) -> None:
        built = build_session_start_hook_payload(
            CONFIG,
            'claude_code',
            {'session_id': 's1', 'repository_url': REPO, 'model': 'claude-sonnet-5'},
        )

        self.assertEqual('claude-sonnet-5', built['payload']['model_id'])


class ModelIdFromModelDictTests(unittest.TestCase):
    def test_model_dict_id_lands_in_outgoing_payload(self) -> None:
        built = build_session_start_hook_payload(
            CONFIG,
            'claude_code',
            {
                'session_id': 's1',
                'repository_url': REPO,
                'model': {'id': 'claude-haiku-5', 'display_name': 'Haiku'},
            },
        )

        self.assertEqual('claude-haiku-5', built['payload']['model_id'])


class ModelIdWithExplicitPayloadTests(unittest.TestCase):
    def test_model_id_key_lands_in_outgoing_payload_when_payload_already_present(
        self,
    ) -> None:
        built = build_session_start_hook_payload(
            CONFIG,
            'claude_code',
            {
                'session_id': 's1',
                'repository_url': REPO,
                'model_id': 'claude-opus-4-6',
                'payload': {'trigger': 'session_start'},
            },
        )

        self.assertEqual('claude-opus-4-6', built['payload']['model_id'])

    def test_existing_model_id_in_payload_is_not_overwritten(self) -> None:
        built = build_session_start_hook_payload(
            CONFIG,
            'claude_code',
            {
                'session_id': 's1',
                'repository_url': REPO,
                'model_id': 'claude-opus-4-6',
                'payload': {'trigger': 'session_start', 'model_id': 'already-set'},
            },
        )

        self.assertEqual('already-set', built['payload']['model_id'])


class ModelIdAbsentTests(unittest.TestCase):
    def test_no_model_info_leaves_model_id_key_absent(self) -> None:
        built = build_session_start_hook_payload(
            CONFIG,
            'claude_code',
            {'session_id': 's1', 'repository_url': REPO},
        )

        self.assertNotIn('model_id', built['payload'])


if __name__ == '__main__':
    unittest.main()
