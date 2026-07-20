import hashlib
import json
import uuid
from types import SimpleNamespace

import pytest

from engram.core.models import (
    MemoryCandidate,
    MemoryCandidateSource,
    MemoryCandidateSourceKind,
    Organization,
    Project,
    Team,
)
from engram.memory.import_provenance import (
    ImportProvenanceError,
    _validated_agent_anchors,
    agent_proposal_candidate_content_hash,
    agent_proposal_evidence_manifest,
    candidate_evidence_manifest,
    validated_agent_candidate_source,
)
from engram.memory.transitions_test_support import provenanced_candidate
from engram.memory.workflow_work import canonical_json_bytes

VALID_ANCHORS = {
    'schema': 'agent_proposal_source.v1',
    'actor_type': 'api_key',
    'actor_id': 'actor-1',
    'api_key_id': 'key-1',
    'request_id': 'req-1',
    'correlation_id': '',
}


def _expected_hash(title: str, body: str, kind: str, team: str | None) -> str:
    serialized = json.dumps(
        ('agent_proposal_candidate', title, body, kind, team),
        sort_keys=True,
        separators=(',', ':'),
    )

    return hashlib.sha256(serialized.encode()).hexdigest()


def _agent_source(anchors: dict[str, object] | object, anchors_hash: str | None = None, **overrides: object) -> SimpleNamespace:
    if anchors_hash is None and isinstance(anchors, dict):
        anchors_hash = hashlib.sha256(canonical_json_bytes(anchors)).hexdigest()
    base: dict[str, object] = {
        'source_kind': 'agent_proposal',
        'window_id': None,
        'stage_id': None,
        'import_source_id': None,
        'observation_id': None,
        'anchors': anchors,
        'anchors_hash': anchors_hash or 'a' * 64,
    }
    base.update(overrides)

    return SimpleNamespace(**base)


def test_agent_proposal_content_hash_matches_spec_digest() -> None:
    assert agent_proposal_candidate_content_hash('Title', 'Body', '', None) == _expected_hash('Title', 'Body', '', None)
    assert agent_proposal_candidate_content_hash('Title', 'Body', 'decision', 't1') == _expected_hash(
        'Title', 'Body', 'decision', 't1'
    )


def test_agent_proposal_content_hash_differs_on_kind_or_team() -> None:
    base = agent_proposal_candidate_content_hash('Title', 'Body', 'decision', None)
    assert agent_proposal_candidate_content_hash('Title', 'Body', 'gotcha', None) != base
    assert agent_proposal_candidate_content_hash('Title', 'Body', 'decision', 'team-1') != base


def test_agent_proposal_content_hash_uses_kind_verbatim() -> None:
    assert agent_proposal_candidate_content_hash('Title', 'Body', '', None) != agent_proposal_candidate_content_hash(
        'Title', 'Body', 'digest', None
    )


def test_agent_proposal_content_hash_team_none_is_json_null() -> None:
    assert agent_proposal_candidate_content_hash('Title', 'Body', '', None) == _expected_hash('Title', 'Body', '', None)
    assert agent_proposal_candidate_content_hash('Title', 'Body', '', None) != agent_proposal_candidate_content_hash(
        'Title', 'Body', '', 'None'
    )


def test_validated_agent_anchors_returns_anchors_on_success() -> None:
    source = _agent_source(VALID_ANCHORS)
    assert _validated_agent_anchors(source) == VALID_ANCHORS


def test_validated_agent_anchors_rejects_lineage_shape() -> None:
    for field in ('window_id', 'stage_id', 'import_source_id', 'observation_id'):
        source = _agent_source(VALID_ANCHORS, **{field: 'x'})
        with pytest.raises(ImportProvenanceError):
            _validated_agent_anchors(source)


def test_validated_agent_anchors_rejects_non_dict_and_bad_schema() -> None:
    with pytest.raises(ImportProvenanceError):
        _validated_agent_anchors(_agent_source('not-a-dict', anchors_hash='a' * 64))
    bad_schema = dict(VALID_ANCHORS, schema='other.v1')
    with pytest.raises(ImportProvenanceError):
        _validated_agent_anchors(_agent_source(bad_schema))


def test_validated_agent_anchors_rejects_missing_required_key() -> None:
    for key in ('actor_type', 'actor_id', 'api_key_id', 'request_id', 'correlation_id'):
        anchors = dict(VALID_ANCHORS)
        anchors.pop(key)
        with pytest.raises(ImportProvenanceError):
            _validated_agent_anchors(_agent_source(anchors))


def test_validated_agent_anchors_rejects_hash_mismatch() -> None:
    source = _agent_source(VALID_ANCHORS, anchors_hash='b' * 64)
    with pytest.raises(ImportProvenanceError):
        _validated_agent_anchors(source)


def _anchors_hash(anchors: dict[str, object]) -> str:
    return hashlib.sha256(canonical_json_bytes(anchors)).hexdigest()


@pytest.fixture
def f_agent_scope() -> tuple[Organization, Team, Project]:
    organization = Organization.objects.create(name='Engram', slug='engram')
    team = Team.objects.create(organization=organization, name='Platform', slug='platform')
    project = Project.objects.create(organization=organization, name='Backend', slug='backend')

    return organization, team, project


def _build_agent_candidate(
    scope: tuple[Organization, Team, Project],
) -> tuple[MemoryCandidate, MemoryCandidateSource, dict[str, object]]:
    organization, team, project = scope
    candidate = MemoryCandidate.objects.create(
        organization=organization,
        project=project,
        team=team,
        title='Agent fact',
        body='Agent proposal body',
        content_hash=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
        decision_work_contract_version=1,
    )
    anchors = dict(VALID_ANCHORS)
    source = MemoryCandidateSource.objects.create(
        organization=organization,
        project=project,
        team=team,
        candidate=candidate,
        source_kind=MemoryCandidateSourceKind.AGENT_PROPOSAL,
        anchors=anchors,
        anchors_hash=_anchors_hash(anchors),
    )

    return candidate, source, anchors


@pytest.mark.django_db
def test_candidate_evidence_manifest_agent_only_single_entry(
    f_agent_scope: tuple[Organization, Team, Project],
) -> None:
    candidate, source, anchors = _build_agent_candidate(f_agent_scope)

    entries, digest = candidate_evidence_manifest(candidate, sources=[source])

    assert entries == [{'anchors': anchors, 'anchors_hash': source.anchors_hash}]
    assert digest == hashlib.sha256(canonical_json_bytes(entries)).hexdigest()

    manifest_entries, manifest_digest = agent_proposal_evidence_manifest(candidate, sources=[source])
    assert manifest_entries == entries
    assert manifest_digest == digest

    query_entries, query_digest = candidate_evidence_manifest(candidate)
    assert query_digest == digest


@pytest.mark.django_db
def test_agent_manifest_rejects_corrupted_hash(
    f_agent_scope: tuple[Organization, Team, Project],
) -> None:
    candidate, source, _ = _build_agent_candidate(f_agent_scope)
    MemoryCandidateSource.objects.filter(id=source.id).update(anchors_hash='b' * 64)
    source.refresh_from_db()

    with pytest.raises(ImportProvenanceError):
        validated_agent_candidate_source(candidate, sources=[source])
    with pytest.raises(ImportProvenanceError):
        agent_proposal_evidence_manifest(candidate, sources=[source])


@pytest.mark.django_db
def test_agent_manifest_rejects_wrong_shape(
    f_agent_scope: tuple[Organization, Team, Project],
) -> None:
    candidate, _source, anchors = _build_agent_candidate(f_agent_scope)
    organization, team, project = f_agent_scope
    malformed = MemoryCandidateSource(
        organization=organization,
        project=project,
        team=team,
        candidate=candidate,
        source_kind=MemoryCandidateSourceKind.AGENT_PROPOSAL,
        observation_id=uuid.uuid4(),
        anchors=anchors,
        anchors_hash=_anchors_hash(anchors),
    )

    with pytest.raises(ImportProvenanceError):
        validated_agent_candidate_source(candidate, sources=[malformed])


@pytest.mark.django_db
def test_agent_manifest_rejects_foreign_scope(
    f_agent_scope: tuple[Organization, Team, Project],
) -> None:
    candidate, _source, anchors = _build_agent_candidate(f_agent_scope)
    _organization, team, project = f_agent_scope
    foreign = MemoryCandidateSource(
        organization_id=uuid.uuid4(),
        project=project,
        team=team,
        candidate=candidate,
        source_kind=MemoryCandidateSourceKind.AGENT_PROPOSAL,
        anchors=anchors,
        anchors_hash=_anchors_hash(anchors),
    )

    with pytest.raises(ImportProvenanceError):
        validated_agent_candidate_source(candidate, sources=[foreign])


@pytest.mark.django_db
def test_agent_manifest_rejects_foreign_candidate(
    f_agent_scope: tuple[Organization, Team, Project],
) -> None:
    candidate, _source, _ = _build_agent_candidate(f_agent_scope)
    other_candidate, other_source, _ = _build_agent_candidate(f_agent_scope)

    assert other_source.candidate_id != candidate.id
    with pytest.raises(ImportProvenanceError):
        validated_agent_candidate_source(candidate, sources=[other_source])


@pytest.mark.django_db
def test_candidate_evidence_manifest_rejects_mixed_kinds() -> None:
    distill_candidate, distill_source, _ = provenanced_candidate('mixedmanifest')
    anchors = dict(VALID_ANCHORS)
    agent_source = MemoryCandidateSource(
        organization=distill_candidate.organization,
        project=distill_candidate.project,
        team=distill_candidate.team,
        candidate=distill_candidate,
        source_kind=MemoryCandidateSourceKind.AGENT_PROPOSAL,
        anchors=anchors,
        anchors_hash=_anchors_hash(anchors),
    )

    with pytest.raises(ImportProvenanceError):
        candidate_evidence_manifest(distill_candidate, sources=[distill_source, agent_source])
