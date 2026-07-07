from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest
from rest_framework.test import APIClient

from engram.access.models import (
    ApiKey,
    ApiKeyCapability,
    Capability,
    Identity,
    OrganizationMembership,
    ProjectGrant,
    Role,
    RoleCapability,
)
from engram.access.services import api_key_fingerprint, api_key_prefix, hash_api_key
from engram.core.models import (
    AgentSession,
    AuditEvent,
    Memory,
    Organization,
    Project,
    ProjectTeam,
    RetrievalDocument,
    Team,
)
from engram.imports.models import ImportJob, ImportJobStatus
from engram.imports.services import ClaudeMemImporter, ClaudeMemImportError

RAW_KEY = 'egk_test_m1_import_0123456789abcdefghijklmnopqrstuvwxyz'
OTHER_RAW_KEY = 'egk_test_m1_other0_0123456789abcdefghijklmnopqrstuvwxyz'
CAPABILITIES = ('memories:admin', 'memories:read')


@dataclass(frozen=True)
class ImportScope:
    organization: Organization
    project: Project
    team: Team
    raw_key: str


def _ensure_capability(code: str) -> Capability:
    capability, _created = Capability.objects.get_or_create(code=code, defaults={'description': code})

    return capability


def create_admin_scope(slug: str, raw_key: str, capabilities: tuple[str, ...] = CAPABILITIES) -> ImportScope:
    organization = Organization.objects.create(name=f'Org {slug}', slug=f'org-{slug}')
    project = Project.objects.create(
        organization=organization,
        name=f'Project {slug}',
        slug=f'project-{slug}',
        repository_root='/workspace/example-repo',
    )
    team = Team.objects.create(organization=organization, name=f'Team {slug}', slug=f'team-{slug}')
    ProjectTeam.objects.create(organization=organization, project=project, team=team)
    owner = Identity.objects.create(
        organization=organization,
        identity_type='service_account',
        external_id=f'svc-{slug}',
        display_name=f'Import owner {slug}',
    )
    role, _created = Role.objects.get_or_create(
        code='import-admin', defaults={'name': 'Import Admin', 'built_in': True}
    )
    for code in capabilities:
        RoleCapability.objects.get_or_create(role=role, capability=_ensure_capability(code))
    OrganizationMembership.objects.create(organization=organization, identity=owner, role=role)
    ProjectGrant.objects.create(organization=organization, project=project, identity=owner, role=role)
    api_key = ApiKey.objects.create(
        organization=organization,
        owner_identity=owner,
        name=f'Import key {slug}',
        key_prefix=api_key_prefix(raw_key),
        key_hash=hash_api_key(raw_key),
        key_fingerprint=api_key_fingerprint(raw_key),
        project=project,
    )
    for code in capabilities:
        ApiKeyCapability.objects.create(api_key=api_key, capability=_ensure_capability(code))

    return ImportScope(organization=organization, project=project, team=team, raw_key=raw_key)


def auth_headers(raw_key: str) -> dict[str, str]:
    return {'HTTP_AUTHORIZATION': f'Bearer {raw_key}'}


def session_row() -> dict[str, Any]:
    return {
        'id': 1,
        'content_session_id': 'content-session-fixture-001',
        'memory_session_id': 'memory-session-fixture-001',
        'project': '/workspace/example-repo',
        'platform_source': 'codex',
        'user_prompt': 'Review sanitized import fixture behavior.',
        'started_at': '2026-06-25T09:00:00Z',
        'started_at_epoch': 1782378000000,
        'completed_at': '2026-06-25T09:10:00Z',
        'completed_at_epoch': 1782378600000,
        'status': 'completed',
        'worker_port': None,
        'prompt_counter': 1,
        'custom_title': 'Sanitized import fixture',
    }


def prompt_row() -> dict[str, Any]:
    return {
        'id': 1,
        'content_session_id': 'content-session-fixture-001',
        'prompt_number': 1,
        'prompt_text': 'Please verify redaction in fixture import.',
        'created_at': '2026-06-25T09:01:00Z',
        'created_at_epoch': 1782378060000,
    }


def observation_row() -> dict[str, Any]:
    return {
        'id': 1,
        'memory_session_id': 'memory-session-fixture-001',
        'project': '/workspace/example-repo',
        'text': 'Importer fixture records a generated observation with file citation metadata.',
        'type': 'discovery',
        'title': 'Fixture import mapping',
        'subtitle': 'Sanitized observation source',
        'facts': '["Fixture data is sanitized"]',
        'narrative': 'The agent reviewed a fixture file and captured import mapping notes.',
        'concepts': '["migration","fixture"]',
        'files_read': '[{"path":"/workspace/example-repo/src/example.py","line_start":1,"line_end":12}]',
        'files_modified': '[]',
        'prompt_number': 1,
        'content_hash': 'fixture-observation-hash-001',
        'agent_type': 'codex',
        'agent_id': 'fixture-agent',
        'generated_by_model': 'fake-provider/fake-model',
        'metadata': '{"redaction_test":true}',
        'created_at': '2026-06-25T09:02:00Z',
        'created_at_epoch': 1782378120000,
    }


def summary_row() -> dict[str, Any]:
    return {
        'id': 1,
        'memory_session_id': 'memory-session-fixture-001',
        'project': '/workspace/example-repo',
        'request': 'Validate the sanitized migration fixture.',
        'investigated': 'Checked the minimal upstream tables and fixture layout.',
        'learned': 'The importer should report deferred runtime artifacts explicitly.',
        'completed': 'Created a reviewed text fixture for importer tests.',
        'next_steps': 'Use the fixture in dry-run and apply importer tests.',
        'files_read': '["/workspace/example-repo/src/example.py"]',
        'files_edited': '[]',
        'notes': 'All data is synthetic and local paths are examples.',
        'prompt_number': 1,
        'created_at': '2026-06-25T09:08:00Z',
        'created_at_epoch': 1782378480000,
    }


def manifest() -> dict[str, Any]:
    return {
        'schema_version_head': 1,
        'tables': {'sdk_sessions': 1, 'user_prompts': 1, 'observations': 1, 'session_summaries': 1},
    }


@pytest.fixture
def f_scope() -> ImportScope:
    return create_admin_scope('alpha', RAW_KEY)


@pytest.fixture
def m_monkeypatch(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    return monkeypatch


def _create_job(scope: ImportScope, store: str = 'store-alpha') -> str:
    response = APIClient().post(
        '/v1/imports/claude-mem',
        {'project_id': str(scope.project.id), 'source_store_id': store, 'manifest': manifest()},
        format='json',
        **auth_headers(scope.raw_key),
    )
    assert response.status_code == 201, response.data

    return response.data['import_id']


def _apply_batch(scope: ImportScope, import_id: str, seq: int, table: str, rows: list[dict[str, Any]]) -> Any:
    return APIClient().post(
        f'/v1/imports/claude-mem/{import_id}/batches',
        {'seq': seq, 'table': table, 'rows': rows},
        format='json',
        **auth_headers(scope.raw_key),
    )


def _stream_all(scope: ImportScope, import_id: str) -> None:
    assert _apply_batch(scope, import_id, 0, 'sdk_sessions', [session_row()]).status_code == 200
    assert _apply_batch(scope, import_id, 1, 'user_prompts', [prompt_row()]).status_code == 200
    assert _apply_batch(scope, import_id, 2, 'observations', [observation_row()]).status_code == 200
    assert _apply_batch(scope, import_id, 3, 'session_summaries', [summary_row()]).status_code == 200


@pytest.mark.django_db
def test_create_import_job_returns_import_id_and_emits_audit(f_scope: ImportScope) -> None:
    import_id = _create_job(f_scope)

    job = ImportJob.objects.get(id=import_id)
    assert job.status == ImportJobStatus.CREATED
    assert job.organization_id == f_scope.organization.id
    assert job.project_id == f_scope.project.id
    assert AuditEvent.objects.filter(
        organization=f_scope.organization,
        event_type='ImportStarted',
        target_id=str(job.id),
    ).exists()


@pytest.mark.django_db
def test_create_rejects_second_active_job_for_same_store(f_scope: ImportScope) -> None:
    _create_job(f_scope, store='dup-store')

    response = APIClient().post(
        '/v1/imports/claude-mem',
        {'project_id': str(f_scope.project.id), 'source_store_id': 'dup-store', 'manifest': manifest()},
        format='json',
        **auth_headers(f_scope.raw_key),
    )

    assert response.status_code == 409


@pytest.mark.django_db
def test_full_stream_promotes_with_expected_confidence(f_scope: ImportScope) -> None:
    import_id = _create_job(f_scope)
    _stream_all(f_scope, import_id)

    observation_memory = Memory.objects.get(
        organization=f_scope.organization,
        metadata__event_type='claude_mem.observation',
    )
    summary_memory = Memory.objects.get(
        organization=f_scope.organization,
        metadata__event_type='claude_mem.session_summary',
    )

    assert observation_memory.confidence == Decimal('0.700')
    assert summary_memory.confidence == Decimal('0.800')
    assert AgentSession.objects.filter(organization=f_scope.organization).count() == 1


@pytest.mark.django_db
def test_batch_replay_is_idempotent(f_scope: ImportScope) -> None:
    import_id = _create_job(f_scope)

    first = _apply_batch(f_scope, import_id, 0, 'sdk_sessions', [session_row()])
    second = _apply_batch(f_scope, import_id, 0, 'sdk_sessions', [session_row()])

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.data['created'] == first.data['created']

    job = ImportJob.objects.get(id=import_id)
    assert job.batches_applied == 1
    assert AgentSession.objects.filter(organization=f_scope.organization).count() == 1


@pytest.mark.django_db
def test_batch_rejects_table_order_violation(f_scope: ImportScope) -> None:
    import_id = _create_job(f_scope)
    _apply_batch(f_scope, import_id, 0, 'sdk_sessions', [session_row()])
    _apply_batch(f_scope, import_id, 1, 'observations', [observation_row()])

    response = _apply_batch(f_scope, import_id, 2, 'user_prompts', [prompt_row()])

    assert response.status_code == 409
    assert response.data['code'] == 'table_order_violation'
    assert AuditEvent.objects.filter(
        organization=f_scope.organization,
        event_type='ImportBatchRejected',
    ).exists()


@pytest.mark.django_db
def test_batch_rejects_out_of_order_seq(f_scope: ImportScope) -> None:
    import_id = _create_job(f_scope)
    _apply_batch(f_scope, import_id, 0, 'sdk_sessions', [session_row()])

    response = _apply_batch(f_scope, import_id, 2, 'user_prompts', [prompt_row()])

    assert response.status_code == 409
    assert response.data['code'] == 'out_of_order_seq'


@pytest.mark.django_db
def test_batch_rejects_too_many_rows(f_scope: ImportScope) -> None:
    import_id = _create_job(f_scope)

    rows = [session_row() for _ in range(201)]
    response = _apply_batch(f_scope, import_id, 0, 'sdk_sessions', rows)

    assert response.status_code == 400
    assert AuditEvent.objects.filter(
        organization=f_scope.organization,
        event_type='ImportBatchRejected',
    ).exists()


@pytest.mark.django_db
def test_missing_capability_is_denied() -> None:
    scope = create_admin_scope('nocap', RAW_KEY, capabilities=('memories:read',))

    response = APIClient().post(
        '/v1/imports/claude-mem',
        {'project_id': str(scope.project.id), 'source_store_id': 'store', 'manifest': manifest()},
        format='json',
        **auth_headers(scope.raw_key),
    )

    assert response.status_code == 403


@pytest.mark.django_db
def test_key_from_other_org_cannot_write_foreign_project(f_scope: ImportScope) -> None:
    other = create_admin_scope('beta', OTHER_RAW_KEY)

    response = APIClient().post(
        '/v1/imports/claude-mem',
        {'project_id': str(f_scope.project.id), 'source_store_id': 'cross', 'manifest': manifest()},
        format='json',
        **auth_headers(other.raw_key),
    )

    assert response.status_code in (403, 404)
    assert not ImportJob.objects.filter(project=f_scope.project).exists()


@pytest.mark.django_db
def test_key_from_other_org_cannot_apply_foreign_batch(f_scope: ImportScope) -> None:
    import_id = _create_job(f_scope)
    other = create_admin_scope('gamma', OTHER_RAW_KEY)

    response = _apply_batch(other, import_id, 0, 'sdk_sessions', [session_row()])

    assert response.status_code == 404


@pytest.mark.django_db
def test_deferred_embedding_skips_provider_call(f_scope: ImportScope, m_monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []

    from engram.context.services import IndexMemoryVersion

    def m_embed(self: Any, document: Any, memory: Any, version: Any) -> None:
        calls.append(document)

    m_monkeypatch.setattr(IndexMemoryVersion, '_embed_document', m_embed)

    import_id = _create_job(f_scope)
    _apply_batch(f_scope, import_id, 0, 'sdk_sessions', [session_row()])
    _apply_batch(f_scope, import_id, 1, 'observations', [observation_row()])

    assert calls == []
    document = RetrievalDocument.objects.get(organization=f_scope.organization)
    assert document.embedding_reference == ''


@pytest.mark.django_db
def test_finalize_returns_report_and_is_idempotent(f_scope: ImportScope) -> None:
    import_id = _create_job(f_scope)
    _stream_all(f_scope, import_id)

    response = APIClient().post(
        f'/v1/imports/claude-mem/{import_id}/finalize',
        {'client_row_counts': {'sdk_sessions': 1, 'user_prompts': 1, 'observations': 1, 'session_summaries': 1}},
        format='json',
        **auth_headers(f_scope.raw_key),
    )

    assert response.status_code == 200
    assert response.data['status'] == ImportJobStatus.SUCCEEDED
    report = response.data['report']
    assert report['created']['memories'] == 2
    assert report['counts']['observations']['client_rows'] == 1
    assert AuditEvent.objects.filter(
        organization=f_scope.organization,
        event_type='ImportCompleted',
    ).exists()

    replay = APIClient().post(
        f'/v1/imports/claude-mem/{import_id}/finalize',
        {'client_row_counts': {'sdk_sessions': 1}},
        format='json',
        **auth_headers(f_scope.raw_key),
    )
    assert replay.status_code == 200
    assert replay.data['status'] == ImportJobStatus.SUCCEEDED


@pytest.mark.django_db
def test_detail_reports_progress(f_scope: ImportScope) -> None:
    import_id = _create_job(f_scope)
    _apply_batch(f_scope, import_id, 0, 'sdk_sessions', [session_row()])

    response = APIClient().get(
        f'/v1/imports/claude-mem/{import_id}',
        **auth_headers(f_scope.raw_key),
    )

    assert response.status_code == 200
    assert response.data['status'] == ImportJobStatus.RECEIVING
    assert response.data['progress']['batches_applied'] == 1
    assert response.data['progress']['rows_created'] == 1


@pytest.mark.django_db
def test_batch_apply_error_marks_job_failed_and_rolls_back(
    f_scope: ImportScope,
    m_monkeypatch: pytest.MonkeyPatch,
) -> None:
    import_id = _create_job(f_scope)

    def m_import_batch(self: object, *args: object, **kwargs: object) -> None:
        raise ClaudeMemImportError('import failed')

    m_monkeypatch.setattr(ClaudeMemImporter, 'import_batch', m_import_batch)

    response = _apply_batch(f_scope, import_id, 0, 'sdk_sessions', [session_row()])

    assert response.status_code == 409
    job = ImportJob.objects.get(id=import_id)
    assert job.status == ImportJobStatus.FAILED
    assert job.failure_reason == 'batch_apply_error'
    assert AgentSession.objects.filter(organization=f_scope.organization).count() == 0
    assert AuditEvent.objects.filter(
        organization=f_scope.organization,
        event_type='ImportFailed',
    ).exists()
