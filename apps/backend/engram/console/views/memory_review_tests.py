from __future__ import annotations

import hashlib
import io
import json
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from engram.access.auth_services import external_id_for_user, resolve_user_scope_for_organization
from engram.access.models import (
    Capability,
    Identity,
    IdentityType,
    OrganizationMembership,
    ProjectGrant,
    Role,
    RoleCapability,
    TeamMembership,
)
from engram.console import services as console_services
from engram.console.serializers.memory_review import ConflictResolveSerializer
from engram.console.services import approve_memory_candidate, conflict_set_etag, reject_review_item
from engram.console.views import memory_review as memory_review_view_module
from engram.core.models import (
    CandidateStatus,
    Memory,
    MemoryCandidate,
    MemoryConflict,
    MemoryLink,
    MemoryReviewExample,
    MemoryStatus,
    MemoryTransition,
    MemoryTransitionType,
    Observation,
    Organization,
    Project,
    ProjectTeam,
    Team,
    VisibilityScope,
)
from engram.memory.transitions import (
    OpenMemoryConflict,
    OpenMemoryConflictInput,
    PromoteMemoryCandidate,
    ResolveMemoryConflict,
    ResolveMemoryConflictInput,
    build_memory_fence,
)
from engram.memory.transitions_test_support import (
    candidate_fence_for,
    candidate_in_scope,
    provenanced_candidate_in_scope,
    transition_request,
    transition_request_for,
)


def _make_user(username: str = 'alice') -> User:
    return User.objects.create_user(username=username, password='strong-secret-123')  # noqa: S106


def _make_role_with_capabilities(code: str, capability_codes: tuple[str, ...]) -> Role:
    role, _ = Role.objects.get_or_create(code=code, defaults={'name': code})

    for raw_code in capability_codes:
        capability, _ = Capability.objects.get_or_create(
            code=raw_code,
            defaults={'description': raw_code},
        )

        RoleCapability.objects.get_or_create(role=role, capability=capability)

    return role


def _make_identity(user: User, organization: Organization) -> Identity:
    identity, _ = Identity.objects.get_or_create(
        organization=organization,
        identity_type=IdentityType.USER,
        external_id=external_id_for_user(user),
        defaults={'display_name': user.get_username()},
    )

    return identity


def _auth_client(token: str, org: Organization) -> APIClient:
    client = APIClient()

    client.credentials(
        HTTP_AUTHORIZATION=f'Token {token}',
        HTTP_X_ENGRAM_ORGANIZATION=str(org.id),
    )

    return client


@pytest.fixture
def f_admin_token() -> str:
    user = _make_user('admin')
    org = Organization.objects.create(name='Acme', slug='acme')
    identity = _make_identity(user, org)

    role = _make_role_with_capabilities(
        'memory_admin',
        ('memories:review', 'memories:admin', 'projects:*', 'teams:*'),
    )

    OrganizationMembership.objects.create(organization=org, identity=identity, role=role)

    from rest_framework.authtoken.models import Token

    return Token.objects.get_or_create(user=user)[0].key


@pytest.fixture
def f_admin_org() -> Organization:
    return Organization.objects.get(slug='acme')


@pytest.fixture
def f_reviewer_token() -> str:
    user = _make_user('reviewer')

    other_org = Organization.objects.create(name='Reviewerco', slug='reviewerco')

    identity = _make_identity(user, other_org)

    role = _make_role_with_capabilities(
        'memory_reviewer',
        ('memories:review',),
    )

    OrganizationMembership.objects.create(organization=other_org, identity=identity, role=role)

    from rest_framework.authtoken.models import Token

    return Token.objects.get_or_create(user=user)[0].key


@pytest.fixture
def f_reviewer_org() -> Organization:
    return Organization.objects.get(slug='reviewerco')


@pytest.fixture
def f_foreign_org() -> Organization:
    return Organization.objects.create(name='Globex', slug='globex')


@pytest.fixture
def f_project(f_admin_org: Organization) -> Project:
    return Project.objects.create(
        organization=f_admin_org,
        name='Eng',
        slug='eng',
    )


@pytest.fixture
def f_team(f_admin_org: Organization) -> Team:
    return Team.objects.create(organization=f_admin_org, name='Core', slug='core')


def _make_observation(organization: Organization, project: Project) -> Observation:
    from engram.core.models import Agent, AgentSession

    agent = Agent.objects.create(
        organization=organization,
        external_id='agent-' + str(Agent.objects.count()),
    )

    session = AgentSession.objects.create(
        organization=organization,
        project=project,
        agent=agent,
        external_session_id='session-' + str(AgentSession.objects.count()),
    )

    return Observation.objects.create(
        organization=organization,
        project=project,
        agent=agent,
        session=session,
        title='Obs title',
        body='Obs body',
        observation_type='tool_use',
        content_hash='hash-obs-' + str(Observation.objects.count()),
        session_sequence=1,
    )


def _make_candidate(
    organization: Organization,
    project: Project,
    *,
    team: Team | None = None,
    status: str = CandidateStatus.PROPOSED,
    confidence: str = '0.500',
    visibility_scope: str = VisibilityScope.PROJECT,
    evidence: list | None = None,
    source_observation: Observation | None = None,
    created_at: datetime | None = None,
    typed: bool = False,
    candidate_title: str | None = None,
    candidate_body: str | None = None,
) -> MemoryCandidate:
    counter = MemoryCandidate.objects.count()

    if typed:
        candidate, _source, _session = provenanced_candidate_in_scope(
            organization,
            project,
            team,
            suffix=f'console-memory-review-{counter}',
            title=candidate_title or f'Candidate {counter}',
            body=candidate_body or f'Body {counter}',
            visibility_scope=visibility_scope,
            confidence=Decimal(confidence),
        )
        if created_at is not None:
            MemoryCandidate.objects.filter(id=candidate.id).update(created_at=created_at)
            candidate.refresh_from_db()

        return candidate

    candidate = MemoryCandidate.objects.create(
        organization=organization,
        project=project,
        team=team,
        title=f'Candidate {counter}',
        body=f'Body {counter}',
        status=status,
        visibility_scope=visibility_scope,
        evidence=evidence if evidence is not None else [],
        content_hash='hash-c-' + str(counter),
        confidence=confidence,
        source_observation=source_observation,
    )

    if created_at is not None:
        MemoryCandidate.objects.filter(id=candidate.id).update(created_at=created_at)

        candidate.refresh_from_db()

    return candidate


def _make_memory(
    organization: Organization,
    project: Project,
    *,
    team: Team | None = None,
    status: str = MemoryStatus.APPROVED,
    confidence: str = '0.900',
    visibility_scope: str = VisibilityScope.PROJECT,
    body: str = 'memory body',
    title: str = 'memory',
    created_at: datetime | None = None,
    typed: bool = False,
) -> Memory:
    counter = Memory.objects.count()

    if typed:
        actor = Identity.objects.create(
            organization=organization,
            identity_type=IdentityType.USER,
            external_id=f'fixture-memory-review-{uuid.uuid4()}',
            display_name='Fixture memory review actor',
        )
        effective_title = title if title != 'memory' else f'Memory {counter}'
        candidate = _make_candidate(
            organization,
            project,
            team=team,
            confidence=confidence,
            visibility_scope=visibility_scope,
            typed=True,
            candidate_title=effective_title,
            candidate_body=body,
        )
        memory = approve_memory_candidate(organization, actor, candidate, 'fixture setup')
        if status == MemoryStatus.REFUTED:
            reject_review_item(organization, actor, memory, 'fixture setup')
            memory.refresh_from_db()
        elif status != MemoryStatus.APPROVED:
            raise ValueError(f'unsupported typed memory status {status}')
        if created_at is not None:
            Memory.objects.filter(id=memory.id).update(created_at=created_at)
            memory.refresh_from_db()

        return memory

    memory = Memory.objects.create(
        organization=organization,
        project=project,
        team=team,
        title=title if title != 'memory' else f'Memory {counter}',
        body=body,
        status=status,
        visibility_scope=visibility_scope,
        confidence=confidence,
    )

    if created_at is not None:
        Memory.objects.filter(id=memory.id).update(created_at=created_at)

        memory.refresh_from_db()

    return memory


# C5.4 conflict-only surface (RED) --------------------------------------------


def _list_items(response_data: Any) -> list[dict[str, Any]]:
    if isinstance(response_data, dict):
        return list(response_data.get('results', []))

    return list(response_data)


def _open_conflicts_for_candidate(
    organization: Organization,
    project: Project,
    *,
    team: Team | None = None,
    memory_count: int = 1,
    suffix: str = 'console-conflict',
) -> tuple[MemoryCandidate, list[Memory], list[MemoryConflict]]:
    base, source, _session = provenanced_candidate_in_scope(
        organization,
        project,
        team,
        suffix=f'{suffix}-base',
        title=f'{suffix} base claim',
        body=f'{suffix} base body about retrieval ranking',
        visibility_scope=VisibilityScope.PROJECT,
    )
    memories = [PromoteMemoryCandidate().execute(transition_request(base)).memory]
    for index in range(1, memory_count):
        sibling, sibling_source = candidate_in_scope(
            base,
            source,
            title=f'{suffix} compared claim {index}',
            body=f'{suffix} compared body {index}',
        )
        memories.append(PromoteMemoryCandidate().execute(transition_request(sibling)).memory)

    candidate, _candidate_source = candidate_in_scope(
        base,
        source,
        title=f'{suffix} candidate claim',
        body=f'{suffix} candidate body about retrieval ranking',
    )

    conflicts: list[MemoryConflict] = []
    for index, memory in enumerate(memories):
        conflict = OpenMemoryConflict().execute(
            OpenMemoryConflictInput(
                request=transition_request_for(
                    candidate,
                    key=f'request:{uuid.uuid4()}:conflict-open:{candidate.id}:{index}:v1',
                ),
                candidate_fence=candidate_fence_for(candidate),
                memory_fence=build_memory_fence(memory),
                evidence_hash=f'{index + 1}' * 64,
                redacted_reason='console conflict evidence',
            )
        )
        conflicts.append(MemoryConflict.objects.get(id=conflict.id))

    return candidate, memories, conflicts


@pytest.mark.django_db
def test_human_inbox_returns_only_open_conflicts(
    f_admin_token: str,
    f_admin_org: Organization,
    f_project: Project,
) -> None:
    conflict_candidate, _memories, _conflicts = _open_conflicts_for_candidate(
        f_admin_org,
        f_project,
        suffix='inbox-open',
    )
    plain_candidate = _make_candidate(f_admin_org, f_project, typed=True)

    client = _auth_client(f_admin_token, f_admin_org)

    response = client.get('/v1/admin/memory-review/')

    assert response.status_code == 200

    items = _list_items(response.data)
    ids = {str(item['id']) for item in items}

    assert str(conflict_candidate.id) in ids
    assert str(plain_candidate.id) not in ids
    assert all(item['type'] == 'conflict' for item in items)
    assert all(item['state'] == 'open' for item in items)
    conflict_item = next(item for item in items if str(item['id']) == str(conflict_candidate.id))
    assert conflict_item['conflict_ids']


@pytest.mark.django_db
def test_low_confidence_refuted_and_proposed_rows_never_enter_inbox(
    f_admin_token: str,
    f_admin_org: Organization,
    f_project: Project,
) -> None:
    conflict_candidate, _memories, _conflicts = _open_conflicts_for_candidate(
        f_admin_org,
        f_project,
        suffix='inbox-filter',
    )
    low_confidence_proposed = _make_candidate(
        f_admin_org,
        f_project,
        confidence='0.100',
        typed=True,
    )
    refuted_memory = _make_memory(f_admin_org, f_project, status=MemoryStatus.REFUTED, typed=True)

    client = _auth_client(f_admin_token, f_admin_org)

    response = client.get('/v1/admin/memory-review/')

    assert response.status_code == 200

    ids = {str(item['id']) for item in _list_items(response.data)}

    assert ids == {str(conflict_candidate.id)}
    assert str(low_confidence_proposed.id) not in ids
    assert str(refuted_memory.id) not in ids


@pytest.mark.django_db
def test_conflict_detail_contains_candidate_and_all_compared_claims(
    f_admin_token: str,
    f_admin_org: Organization,
    f_project: Project,
) -> None:
    candidate, memories, conflicts = _open_conflicts_for_candidate(
        f_admin_org,
        f_project,
        memory_count=2,
        suffix='detail',
    )

    client = _auth_client(f_admin_token, f_admin_org)

    response = client.get(f'/v1/admin/memory-review/{candidate.id}/')

    assert response.status_code == 200
    assert response.get('ETag')

    body = response.data
    assert str(body['id']) == str(candidate.id)
    assert body['type'] == 'conflict'
    assert body['candidate_claim']

    compared_memory_ids = {str(claim['memory_id']) for claim in body['existing_claims']}
    assert compared_memory_ids == {str(memory.id) for memory in memories}
    assert set(map(str, body['conflict_ids'])) == {str(conflict.id) for conflict in conflicts}


@pytest.mark.django_db
def test_conflict_detail_exposes_decision_and_bounded_evidence_context(
    f_admin_token: str,
    f_admin_org: Organization,
    f_project: Project,
) -> None:
    from engram.core.models import (
        CurationDecision,
        CurationOutcome,
        CurationReasonCode,
        EvidenceTier,
        MemoryCandidateSource,
        WorkflowSubjectType,
        WorkflowWork,
        WorkflowWorkType,
    )
    from engram.memory.candidate_decision_work import evidence_manifest
    from engram.model_policy.models import ModelPolicy, ProviderCallRecord, ProviderSecret

    candidate, memories, conflicts = _open_conflicts_for_candidate(
        f_admin_org,
        f_project,
        suffix='detail-evidence-context',
    )
    memory = memories[0]
    conflict = conflicts[0]
    candidate_source = candidate.sources.select_related('observation').get()
    version_source = conflict.memory_version.provenance_sources.select_related(
        'candidate_source__observation',
    ).get()
    assert version_source.candidate_source_id is not None

    manifest_entries, manifest_hash = evidence_manifest(candidate)
    assert [entry['observation_id'] for entry in manifest_entries] == [
        str(candidate_source.observation_id),
    ]
    evidence_membership = {
        'candidate': [
            {
                'reference_id': str(candidate_source.id),
                'source_kind': candidate_source.source_kind,
                'observation_id': str(candidate_source.observation_id),
            },
        ],
        'targets': [
            {
                'memory_version_id': str(conflict.memory_version_id),
                'sources': [
                    {
                        'reference_id': str(version_source.candidate_source_id),
                        'source_kind': version_source.candidate_source.source_kind,
                        'observation_id': str(version_source.candidate_source.observation_id),
                    },
                ],
            },
        ],
    }

    work = WorkflowWork.objects.create(
        organization=f_admin_org,
        project=f_project,
        work_type=WorkflowWorkType.CANDIDATE_DECISION,
        subject_type=WorkflowSubjectType.MEMORY_CANDIDATE,
        subject_id=candidate.id,
        contract_version=1,
        occurrence_key='',
        input_fingerprint='a' * 64,
        input_snapshot={
            'schema': 'candidate_decision_input/v1',
            'candidate_id': str(candidate.id),
            'evidence_manifest_hash': manifest_hash,
        },
    )
    secret = ProviderSecret.objects.create(
        organization=f_admin_org,
        name='Conflict detail curation secret',
        provider='openai',
        scope='organization',
    )
    policy = ModelPolicy.objects.create(
        organization=f_admin_org,
        project=f_project,
        secret=secret,
        name='Conflict detail curation policy',
        scope='project',
        task_type='curation',
        provider='openai',
        model='gpt-4.1-mini',
        version=7,
    )
    provider_call = ProviderCallRecord.objects.create(
        organization=f_admin_org,
        project=f_project,
        policy=policy,
        secret=secret,
        provider='openai',
        model='gpt-4.1-mini',
        task_type='curation',
        policy_version=7,
        request_id='conflict-detail-curation-call',
        redaction_state='redacted',
    )
    judge_reason = 'Both supported claims apply to the same project and cannot both be true.'
    decision = CurationDecision.objects.create(
        organization=f_admin_org,
        project=f_project,
        work=work,
        candidate=candidate,
        input_fingerprint=work.input_fingerprint,
        evidence_manifest_hash=manifest_hash,
        comparison_manifest_hash='b' * 64,
        outcome=CurationOutcome.OPEN_CONFLICT,
        reason_code=CurationReasonCode.SAME_SCOPE_CONTRADICTION,
        redacted_reason=judge_reason,
        effective_visibility_scope=VisibilityScope.PROJECT,
        target_memory_version=conflict.memory_version,
        evidence_tier=EvidenceTier.SUPPORTED,
        provider_call_record=provider_call,
        policy=policy,
        policy_version=7,
        transition=conflict.opened_transition,
        conflict=conflict,
        payload_hash='c' * 64,
        applicability='same',
        evidence_membership=evidence_membership,
    )

    _late_candidate, late_template, _late_session = provenanced_candidate_in_scope(
        f_admin_org,
        f_project,
        None,
        suffix='detail-evidence-late',
        title='Late evidence template',
        body='This source arrived only after the conflict decision.',
    )
    late_source = MemoryCandidateSource.objects.create(
        organization=f_admin_org,
        project=f_project,
        candidate=candidate,
        window=late_template.window,
        observation=late_template.observation,
        stage=late_template.stage,
        anchors=late_template.anchors,
        anchors_hash=late_template.anchors_hash,
    )
    _current_entries, current_manifest_hash = evidence_manifest(candidate)
    assert current_manifest_hash != decision.evidence_manifest_hash

    client = _auth_client(f_admin_token, f_admin_org)
    response = client.get(f'/v1/admin/memory-review/{candidate.id}/')

    assert response.status_code == 200
    body = response.data
    assert {
        'conflicts',
        'decision',
        'effective_applicability',
    } <= set(body)

    def assert_semantic_subset(actual: dict[str, Any], expected: dict[str, Any]) -> None:
        for key, expected_value in expected.items():
            assert key in actual
            actual_value = actual[key]
            if isinstance(expected_value, dict):
                assert isinstance(actual_value, dict)
                assert_semantic_subset(actual_value, expected_value)
            else:
                assert actual_value == expected_value

    conflict_payload = next(item for item in body['conflicts'] if str(item['id']) == str(conflict.id))
    assert_semantic_subset(
        conflict_payload,
        {
            'id': str(conflict.id),
            'opened_transition_id': str(conflict.opened_transition_id),
            'decision_id': str(decision.id),
            'evidence_hash': conflict.evidence_hash,
        },
    )
    assert_semantic_subset(
        body['decision'],
        {
            'id': str(decision.id),
            'work_id': str(work.id),
            'outcome': 'open_conflict',
            'reason_code': 'same_scope_contradiction',
            'target_memory_version_id': str(conflict.memory_version_id),
            'transition_id': str(conflict.opened_transition_id),
            'conflict_id': str(conflict.id),
            'evidence_tier': 'supported',
            'evidence_manifest_hash': manifest_hash,
            'comparison_manifest_hash': 'b' * 64,
            'effective_scope': {
                'project_id': str(f_project.id),
                'visibility_scope': 'project',
                'team_id': None,
            },
            'judge': {
                'status': 'succeeded',
                'reason': judge_reason,
                'provider_call_record_id': str(provider_call.id),
                'policy_id': str(policy.id),
                'policy_version': 7,
                'provider': 'openai',
                'model': 'gpt-4.1-mini',
            },
        },
    )
    assert_semantic_subset(
        body['effective_applicability'],
        {
            'verdict': 'same',
            'candidate': {
                'project_id': str(f_project.id),
                'visibility_scope': 'project',
                'team_id': None,
            },
        },
    )
    applicability_target = next(
        item for item in body['effective_applicability']['targets'] if str(item['memory_id']) == str(memory.id)
    )
    assert_semantic_subset(
        applicability_target,
        {
            'memory_id': str(memory.id),
            'version_id': str(conflict.memory_version_id),
            'project_id': str(f_project.id),
            'visibility_scope': 'project',
            'team_id': None,
        },
    )

    candidate_evidence = body['candidate_claim']['evidence']
    assert [str(item['reference_id']) for item in candidate_evidence] == [
        str(candidate_source.id),
    ]
    assert_semantic_subset(
        candidate_evidence[0],
        {
            'source_kind': candidate_source.source_kind,
            'observation_id': str(candidate_source.observation_id),
        },
    )
    assert candidate_evidence[0]['summary']

    existing_claim = next(
        item for item in body['existing_claims'] if str(item['version_id']) == str(conflict.memory_version_id)
    )
    target_evidence = existing_claim['evidence']
    assert [str(item['reference_id']) for item in target_evidence] == [
        str(version_source.candidate_source_id),
    ]
    assert_semantic_subset(
        target_evidence[0],
        {
            'source_kind': version_source.candidate_source.source_kind,
            'observation_id': str(version_source.candidate_source.observation_id),
        },
    )
    assert target_evidence[0]['summary']

    returned_reference_ids = {
        str(item['reference_id'])
        for claim in [body['candidate_claim'], *body['existing_claims']]
        for item in claim['evidence']
    }
    assert str(late_source.id) not in returned_reference_ids


@pytest.mark.django_db
def test_conflict_resolution_requires_current_etag(
    f_admin_token: str,
    f_admin_org: Organization,
    f_project: Project,
) -> None:
    candidate, _memories, conflicts = _open_conflicts_for_candidate(
        f_admin_org,
        f_project,
        suffix='etag',
    )

    client = _auth_client(f_admin_token, f_admin_org)

    missing = client.post(
        f'/v1/admin/memory-review/{candidate.id}/resolve/',
        {'action': 'reject_candidate', 'reason': 'no precondition supplied'},
        format='json',
    )
    assert missing.status_code == 428

    stale = client.post(
        f'/v1/admin/memory-review/{candidate.id}/resolve/',
        {'action': 'reject_candidate', 'reason': 'stale precondition supplied'},
        format='json',
        HTTP_IF_MATCH='"stale-etag-value"',
    )
    assert stale.status_code == 412

    for conflict in conflicts:
        conflict.refresh_from_db()
        assert conflict.resolved_transition_id is None


@pytest.mark.django_db(transaction=True)
def test_conflict_resolution_rechecks_if_match_after_candidate_lock(
    monkeypatch: pytest.MonkeyPatch,
    f_admin_token: str,
    f_admin_org: Organization,
    f_project: Project,
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from django.db import close_old_connections

    from engram.console.views import memory_review as memory_review_view

    candidate, _memories, reviewed_conflicts = _open_conflicts_for_candidate(
        f_admin_org,
        f_project,
        suffix='etag-toctou',
    )
    late_memory_candidate = _make_candidate(
        f_admin_org,
        f_project,
        typed=True,
        candidate_title='Late compared claim',
        candidate_body='This compared claim was not present in the reviewed conflict set.',
    )
    late_memory = PromoteMemoryCandidate().execute(transition_request(late_memory_candidate)).memory

    client = _auth_client(f_admin_token, f_admin_org)
    detail = client.get(f'/v1/admin/memory-review/{candidate.id}/')
    assert detail.status_code == 200
    reviewed_etag = detail.get('ETag')
    assert reviewed_etag

    after_precondition = Event()
    continue_resolution = Event()
    original_resolve = memory_review_view.resolve_candidate_conflicts

    def pause_before_transaction(*args: Any, **kwargs: Any) -> dict[str, Any]:
        after_precondition.set()
        assert continue_resolution.wait(timeout=10)
        return original_resolve(*args, **kwargs)

    monkeypatch.setattr(memory_review_view, 'resolve_candidate_conflicts', pause_before_transaction)

    def post_resolution() -> Any:
        close_old_connections()
        try:
            request_client = _auth_client(f_admin_token, f_admin_org)
            return request_client.post(
                f'/v1/admin/memory-review/{candidate.id}/resolve/',
                {'action': 'reject_candidate', 'reason': 'resolve only the reviewed conflict set'},
                format='json',
                HTTP_IF_MATCH=reviewed_etag,
            )
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=1) as executor:
        response_future = executor.submit(post_resolution)
        assert after_precondition.wait(timeout=10)

        try:
            late_conflict = OpenMemoryConflict().execute(
                OpenMemoryConflictInput(
                    request=transition_request_for(
                        candidate,
                        key=f'request:{uuid.uuid4()}:conflict-open:{candidate.id}:late:v1',
                    ),
                    candidate_fence=candidate_fence_for(candidate),
                    memory_fence=build_memory_fence(late_memory),
                    evidence_hash='f' * 64,
                    redacted_reason='conflict opened after If-Match validation',
                )
            )
            changed_detail = client.get(f'/v1/admin/memory-review/{candidate.id}/')
            assert changed_detail.status_code == 200
            assert changed_detail.get('ETag') != reviewed_etag
        finally:
            continue_resolution.set()

        response = response_future.result(timeout=10)

    assert response.status_code == 412
    for conflict in [*reviewed_conflicts, late_conflict]:
        conflict.refresh_from_db()
        assert conflict.resolved_transition_id is None


@pytest.mark.django_db
def test_conflict_resolution_closes_complete_candidate_set_in_one_cp4_transition(
    f_admin_token: str,
    f_admin_org: Organization,
    f_project: Project,
) -> None:
    candidate, _memories, conflicts = _open_conflicts_for_candidate(
        f_admin_org,
        f_project,
        memory_count=2,
        suffix='close-set',
    )

    client = _auth_client(f_admin_token, f_admin_org)

    detail = client.get(f'/v1/admin/memory-review/{candidate.id}/')
    assert detail.status_code == 200
    etag = detail.get('ETag')

    response = client.post(
        f'/v1/admin/memory-review/{candidate.id}/resolve/',
        {'action': 'reject_candidate', 'reason': 'close the whole conflict set'},
        format='json',
        HTTP_IF_MATCH=etag,
    )

    assert response.status_code == 200

    transition_id = str(response.data['transition_id'])
    assert set(map(str, response.data['conflict_ids'])) == {str(conflict.id) for conflict in conflicts}
    assert response.data['state'] == 'resolved'

    for conflict in conflicts:
        conflict.refresh_from_db()
        assert conflict.resolved_transition_id is not None
        assert str(conflict.resolved_transition_id) == transition_id

    resolve_transitions = MemoryTransition.objects.filter(
        id__in=[conflict.resolved_transition_id for conflict in conflicts],
        transition_type=MemoryTransitionType.CONFLICT_RESOLVE,
    )
    assert resolve_transitions.count() == 1

    candidate.refresh_from_db()
    assert candidate.status == CandidateStatus.REJECTED


@pytest.mark.django_db
def test_resolved_conflict_disappears_without_deleting_evidence(
    f_admin_token: str,
    f_admin_org: Organization,
    f_project: Project,
) -> None:
    candidate, _memories, conflicts = _open_conflicts_for_candidate(
        f_admin_org,
        f_project,
        suffix='disappears',
    )
    link_ids = [conflict.semantic_link_id for conflict in conflicts]

    client = _auth_client(f_admin_token, f_admin_org)

    detail = client.get(f'/v1/admin/memory-review/{candidate.id}/')
    assert detail.status_code == 200

    resolve = client.post(
        f'/v1/admin/memory-review/{candidate.id}/resolve/',
        {'action': 'reject_candidate', 'reason': 'resolve and retain evidence'},
        format='json',
        HTTP_IF_MATCH=detail.get('ETag'),
    )
    assert resolve.status_code == 200

    after_list = client.get('/v1/admin/memory-review/')
    assert str(candidate.id) not in {str(item['id']) for item in _list_items(after_list.data)}

    after_detail = client.get(f'/v1/admin/memory-review/{candidate.id}/')
    assert after_detail.status_code == 404

    assert MemoryConflict.objects.filter(candidate=candidate).count() == len(conflicts)
    for conflict in conflicts:
        conflict.refresh_from_db()
        assert conflict.resolved_transition_id is not None
    assert MemoryLink.objects.filter(id__in=link_ids).count() == len(link_ids)


@pytest.mark.django_db
def test_foreign_conflict_list_detail_and_resolve_return_no_existence(
    f_admin_token: str,
    f_admin_org: Organization,
) -> None:
    foreign_org = Organization.objects.create(name='Foreignco', slug='foreignco')
    foreign_project = Project.objects.create(organization=foreign_org, name='Foreign', slug='foreign')
    foreign_candidate, _memories, _conflicts = _open_conflicts_for_candidate(
        foreign_org,
        foreign_project,
        suffix='foreign',
    )

    client = _auth_client(f_admin_token, f_admin_org)

    listing = client.get('/v1/admin/memory-review/')
    assert listing.status_code == 200
    assert str(foreign_candidate.id) not in {str(item['id']) for item in _list_items(listing.data)}

    detail = client.get(f'/v1/admin/memory-review/{foreign_candidate.id}/')
    assert detail.status_code == 404

    resolve = client.post(
        f'/v1/admin/memory-review/{foreign_candidate.id}/resolve/',
        {'action': 'reject_candidate', 'reason': 'foreign scope'},
        format='json',
        HTTP_IF_MATCH='"any"',
    )
    assert resolve.status_code == 404


@pytest.mark.django_db
def test_conflict_resolution_records_exported_review_example(
    f_admin_token: str,
    f_admin_org: Organization,
    f_project: Project,
) -> None:
    candidate, _memories, _conflicts = _open_conflicts_for_candidate(
        f_admin_org,
        f_project,
        suffix='export-example',
    )

    client = _auth_client(f_admin_token, f_admin_org)

    detail = client.get(f'/v1/admin/memory-review/{candidate.id}/')
    assert detail.status_code == 200

    resolve = client.post(
        f'/v1/admin/memory-review/{candidate.id}/resolve/',
        {'action': 'reject_candidate', 'reason': 'record human example for eval'},
        format='json',
        HTTP_IF_MATCH=detail.get('ETag'),
    )
    assert resolve.status_code == 200

    example = MemoryReviewExample.objects.filter(
        organization=f_admin_org,
        item_id=str(candidate.id),
        action='reject_candidate',
    ).first()
    assert example is not None
    assert example.curator_context.get('conflict_ids')

    buffer = io.StringIO()
    call_command('engram_export_review_examples', organization=str(f_admin_org.id), stdout=buffer)

    exported = [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]
    conflict_rows = [row for row in exported if row['item_id'] == str(candidate.id)]
    assert conflict_rows
    assert conflict_rows[0]['action'] == 'reject_candidate'


@pytest.mark.django_db
def test_conflict_review_enforces_effective_project_and_team_scope() -> None:
    organization = Organization.objects.create(name='Scoped conflict org', slug='scoped-conflict-org')

    allowed_project = Project.objects.create(
        organization=organization,
        name='Allowed project',
        slug='allowed-project',
    )
    allowed_team = Team.objects.create(
        organization=organization,
        name='Allowed team',
        slug='allowed-team',
    )
    ProjectTeam.objects.create(
        organization=organization,
        project=allowed_project,
        team=allowed_team,
    )

    foreign_project = Project.objects.create(
        organization=organization,
        name='Foreign project',
        slug='foreign-project',
    )
    foreign_team = Team.objects.create(
        organization=organization,
        name='Foreign team',
        slug='foreign-team',
    )
    ProjectTeam.objects.create(
        organization=organization,
        project=foreign_project,
        team=foreign_team,
    )

    user = _make_user('scoped-conflict-admin')
    identity = _make_identity(user, organization)
    membership_role = _make_role_with_capabilities('scoped_conflict_member', ())
    scoped_role = _make_role_with_capabilities(
        'scoped_conflict_admin',
        ('memories:review', 'memories:admin'),
    )
    OrganizationMembership.objects.create(
        organization=organization,
        identity=identity,
        role=membership_role,
    )
    ProjectGrant.objects.create(
        organization=organization,
        project=allowed_project,
        identity=identity,
        role=scoped_role,
    )
    TeamMembership.objects.create(
        organization=organization,
        team=allowed_team,
        identity=identity,
        role=scoped_role,
    )

    scope = resolve_user_scope_for_organization(user, organization)
    assert scope.project_ids == (allowed_project.id,)
    assert scope.team_ids == (allowed_team.id,)
    assert foreign_project.id not in scope.project_ids
    assert foreign_team.id not in scope.team_ids
    assert {'memories:review', 'memories:admin'} <= set(scope.capabilities)

    candidate, _memories, conflicts = _open_conflicts_for_candidate(
        organization,
        foreign_project,
        team=foreign_team,
        suffix='same-org-outside-effective-scope',
    )
    etag = conflict_set_etag(candidate)
    client = _auth_client(Token.objects.create(user=user).key, organization)

    listing = client.get('/v1/admin/memory-review/')
    detail = client.get(f'/v1/admin/memory-review/{candidate.id}/')
    resolve = client.post(
        f'/v1/admin/memory-review/{candidate.id}/resolve/',
        {'action': 'reject_candidate', 'reason': 'must not cross effective scope'},
        format='json',
        HTTP_IF_MATCH=etag,
    )

    candidate.refresh_from_db()
    for conflict in conflicts:
        conflict.refresh_from_db()

    listed_ids = {str(item['id']) for item in _list_items(listing.data)}
    observed = (
        str(candidate.id) in listed_ids,
        detail.status_code,
        resolve.status_code,
        candidate.status,
        all(conflict.resolved_transition_id is None for conflict in conflicts),
    )
    assert observed == (
        False,
        404,
        404,
        CandidateStatus.PROPOSED,
        True,
    )


# C5.4 list/detail contract gaps (codex 32, RED) ------------------------------


@pytest.mark.django_db
def test_conflict_list_cursor_reaches_candidate_after_first_fifty(
    f_admin_token: str,
    f_admin_org: Organization,
    f_project: Project,
) -> None:
    candidate_ids: set[str] = set()

    for index in range(51):
        candidate, _memories, _conflicts = _open_conflicts_for_candidate(
            f_admin_org,
            f_project,
            suffix=f'pagination-{index}',
        )
        candidate_ids.add(str(candidate.id))

    client = _auth_client(f_admin_token, f_admin_org)

    first = client.get('/v1/admin/memory-review/')

    assert first.status_code == 200
    assert len(first.data['results']) == 50
    assert first.data['next'] is not None

    second = client.get(first.data['next'])

    assert second.status_code == 200
    returned_ids = {str(item['id']) for item in [*first.data['results'], *second.data['results']]}
    assert returned_ids == candidate_ids


@pytest.mark.django_db
def test_conflict_search_matches_compared_claim_body(
    f_admin_token: str,
    f_admin_org: Organization,
    f_project: Project,
) -> None:
    token = 'compared-only-zebra-417'
    base, source, _session = provenanced_candidate_in_scope(
        f_admin_org,
        f_project,
        None,
        suffix='search-compared-claim',
        title='Existing compared claim',
        body=f'This immutable compared body contains {token}.',
        visibility_scope=VisibilityScope.PROJECT,
    )
    memory = PromoteMemoryCandidate().execute(transition_request(base)).memory
    candidate, _candidate_source = candidate_in_scope(
        base,
        source,
        title='Candidate claim without the marker',
        body='Candidate body without the marker.',
    )
    opened = OpenMemoryConflict().execute(
        OpenMemoryConflictInput(
            request=transition_request_for(
                candidate,
                key=f'request:{uuid.uuid4()}:conflict-open:{candidate.id}:search',
            ),
            candidate_fence=candidate_fence_for(candidate),
            memory_fence=build_memory_fence(memory),
            evidence_hash='a' * 64,
            redacted_reason='compared-claim search contract',
        ),
    )
    conflict = MemoryConflict.objects.select_related('memory_version').get(id=opened.id)
    assert token in conflict.memory_version.body
    assert token not in candidate.title
    assert token not in candidate.body

    client = _auth_client(f_admin_token, f_admin_org)

    response = client.get('/v1/admin/memory-review/', {'search': token})

    assert response.status_code == 200
    assert str(candidate.id) in {str(item['id']) for item in _list_items(response.data)}


def test_conflict_resolve_rejects_title_above_memory_column_limit() -> None:
    serializer = ConflictResolveSerializer(
        data={
            'action': 'merge_candidate',
            'reason': 'exercise the persistence boundary',
            'target_memory_id': str(uuid.uuid4()),
            'merged_title': 'x' * 256,
            'merged_body': 'valid merged body',
        },
    )

    assert serializer.is_valid() is False
    assert serializer.errors['merged_title'][0].code == 'max_length'


@pytest.mark.django_db
def test_conflict_detail_body_matches_its_pinned_version_id(
    f_admin_token: str,
    f_admin_org: Organization,
    f_project: Project,
) -> None:
    candidate, memories, conflicts = _open_conflicts_for_candidate(
        f_admin_org,
        f_project,
        suffix='pinned-detail',
    )
    memory = memories[0]
    pinned_version = conflicts[0].memory_version

    # codex 14 (this branch) blocks ReviseMemory while a conflict pins the memory,
    # so the divergence between the immutable pinned version and the live row is
    # seeded directly to prove the serializer reads the pinned version snapshot.
    Memory.objects.filter(id=memory.id).update(
        title='Live revised title',
        body='Live revised body',
        current_version=pinned_version.version + 1,
    )

    client = _auth_client(f_admin_token, f_admin_org)

    response = client.get(f'/v1/admin/memory-review/{candidate.id}/')

    assert response.status_code == 200
    claim = next(item for item in response.data['existing_claims'] if str(item['memory_id']) == str(memory.id))
    assert str(claim['version_id']) == str(pinned_version.id)
    assert claim['body'] == pinned_version.body
    assert claim['body_hash'] == hashlib.sha256(pinned_version.body.encode()).hexdigest()


def _resolve_conflicts_during_read(
    candidate: MemoryCandidate,
    memories: list[Memory],
    conflicts: list[MemoryConflict],
    *,
    key: str,
) -> None:
    ResolveMemoryConflict().execute(
        ResolveMemoryConflictInput(
            request=transition_request_for(candidate, key=key),
            candidate_fence=candidate_fence_for(candidate),
            conflict_ids=tuple(conflict.id for conflict in conflicts),
            conflict_memory_fences=tuple(build_memory_fence(memory) for memory in memories),
            resolution='reject_candidate',
        ),
    )


@pytest.mark.django_db
def test_conflict_list_skips_candidate_resolved_between_read_queries(
    f_admin_token: str,
    f_admin_org: Organization,
    f_project: Project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, memories, conflicts = _open_conflicts_for_candidate(
        f_admin_org,
        f_project,
        suffix='list-resolution-race',
    )
    real_fetch = console_services.open_conflicts_for_candidates
    resolved = False

    def resolve_then_fetch(
        organization: Organization,
        candidate_ids: list[uuid.UUID],
    ) -> dict[uuid.UUID, list[MemoryConflict]]:
        nonlocal resolved

        if not resolved:
            resolved = True
            _resolve_conflicts_during_read(
                candidate,
                memories,
                conflicts,
                key=f'list-resolution-race:{candidate.id}',
            )

        return real_fetch(organization, candidate_ids)

    monkeypatch.setattr(
        memory_review_view_module,
        'open_conflicts_for_candidates',
        resolve_then_fetch,
    )
    client = _auth_client(f_admin_token, f_admin_org)

    response = client.get('/v1/admin/memory-review/')

    assert response.status_code == 200
    assert str(candidate.id) not in {str(item['id']) for item in _list_items(response.data)}


@pytest.mark.django_db
def test_conflict_detail_returns_404_when_resolved_between_read_queries(
    f_admin_token: str,
    f_admin_org: Organization,
    f_project: Project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, memories, conflicts = _open_conflicts_for_candidate(
        f_admin_org,
        f_project,
        suffix='detail-resolution-race',
    )
    real_fetch = console_services.open_conflicts_for_candidates
    resolved = False

    def resolve_then_fetch(
        organization: Organization,
        candidate_ids: list[uuid.UUID],
    ) -> dict[uuid.UUID, list[MemoryConflict]]:
        nonlocal resolved

        if not resolved:
            resolved = True
            _resolve_conflicts_during_read(
                candidate,
                memories,
                conflicts,
                key=f'detail-resolution-race:{candidate.id}',
            )

        return real_fetch(organization, candidate_ids)

    monkeypatch.setattr(
        memory_review_view_module,
        'open_conflicts_for_candidates',
        resolve_then_fetch,
    )
    client = _auth_client(f_admin_token, f_admin_org)

    response = client.get(f'/v1/admin/memory-review/{candidate.id}/')

    assert response.status_code == 404
