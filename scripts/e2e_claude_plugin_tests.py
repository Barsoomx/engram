from __future__ import annotations

from scripts.e2e_claude_plugin import verification_query


def test_verification_query_uses_relational_cp3_candidate_provenance() -> None:
    query = verification_query()

    assert 'MemoryCandidateSource' in query
    assert 'MemoryCandidateSource.objects.filter(candidate__project=project)' in query
    assert "c.evidence[0].get('kind') == 'session_distillation'" not in query
