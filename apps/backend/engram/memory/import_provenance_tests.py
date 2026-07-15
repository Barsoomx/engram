from __future__ import annotations

import importlib

from engram.imports.services import ClaudeMemImporter


def test_import_candidate_hash_helper_matches_legacy_import_algorithm() -> None:
    provenance = importlib.import_module('engram.memory.import_provenance')
    helper = getattr(provenance, 'import_candidate_content_hash', None)
    assert callable(helper)

    source_id = 'claude-mem:fixture-store:observation:session-1:1'
    observation_content_hash = 'a' * 64
    expected = ClaudeMemImporter()._content_hash(
        'memory-candidate',
        source_id,
        observation_content_hash,
    )
    assert helper(source_id, observation_content_hash) == expected
