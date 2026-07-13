from __future__ import annotations

import hashlib
from datetime import timedelta

import pytest
from django.db import transaction
from django.utils import timezone

from engram.context.services import IndexMemoryVersion, IndexMemoryVersionInput
from engram.core.models import (
    Memory,
    MemoryStatus,
    MemoryVersion,
    Organization,
    Project,
    VisibilityScope,
    WorkflowSubjectType,
    WorkflowWork,
    WorkflowWorkType,
)
from engram.memory.tasks import generate_weekly_digest_work_v1
from engram.memory.workflow_work import CreateWorkflowWorkInput

_UNPROVEN = 'digest_visibility_unproven'


def _load_digest_visibility() -> object:
    import engram.memory.digest_visibility as digest_visibility

    return digest_visibility


def make_org_project(suffix: str) -> tuple[Organization, Project]:
    organization = Organization.objects.create(name=f'DV Org {suffix}', slug=f'dv-org-{suffix}')
    project = Project.objects.create(
        organization=organization,
        name=f'DV Project {suffix}',
        slug=f'dv-project-{suffix}',
    )

    return organization, project


def make_source_memory(organization: Organization, project: Project, *, title: str, body: str) -> Memory:
    memory = Memory.objects.create(
        organization=organization,
        project=project,
        title=title,
        body=body,
        status=MemoryStatus.APPROVED,
        visibility_scope=VisibilityScope.PROJECT,
        metadata={},
    )
    MemoryVersion.objects.create(
        organization=organization,
        project=project,
        memory=memory,
        version=memory.current_version,
        body=body,
        content_hash=hashlib.sha256(body.encode()).hexdigest(),
    )

    return memory


def build_proven_weekly_digest(
    organization: Organization,
    project: Project,
    *,
    schedule_key: str = 'weekly:proven',
) -> Memory:
    import engram.memory.digest_work as digest_work

    make_source_memory(organization, project, title='Source Alpha', body='source body alpha')
    now = timezone.now()
    with transaction.atomic():
        snapshot = digest_work.freeze_weekly_digest_input(
            organization_id=organization.id,
            project_id=project.id,
            team_id=None,
            window_start=now - timedelta(days=7),
            window_end=now + timedelta(minutes=5),
            schedule_key=schedule_key,
        )
        work, _created = digest_work.create_digest_work_and_signal(
            data=CreateWorkflowWorkInput(
                organization_id=organization.id,
                project_id=project.id,
                work_type=WorkflowWorkType.WEEKLY_DIGEST,
                subject_type=WorkflowSubjectType.PROJECT,
                subject_id=project.id,
                input_snapshot=snapshot,
                occurrence_key=schedule_key,
            ),
            signal_task=generate_weekly_digest_work_v1,
        )
    generate_weekly_digest_work_v1(str(work.id))

    return Memory.objects.get(organization=organization, project=project, kind='digest')


def build_legacy_digest(
    organization: Organization,
    project: Project,
    *,
    title: str = 'Legacy Digest',
    body: str = 'legacy digest body',
    digest_kind: str = 'weekly_structured',
    metadata: dict[str, object] | None = None,
) -> Memory:
    resolved_metadata = metadata if metadata is not None else {'kind': 'digest', 'digest_kind': digest_kind}
    memory = Memory.objects.create(
        organization=organization,
        project=project,
        title=title,
        body=body,
        status=MemoryStatus.APPROVED,
        visibility_scope=VisibilityScope.PROJECT,
        metadata=resolved_metadata,
    )
    version = MemoryVersion.objects.create(
        organization=organization,
        project=project,
        memory=memory,
        version=memory.current_version,
        body=body,
        content_hash=hashlib.sha256(body.encode()).hexdigest(),
    )
    IndexMemoryVersion().execute(IndexMemoryVersionInput(memory_version_id=version.id, defer_embedding=True))

    return memory


def _reload(memory: Memory) -> Memory:
    return Memory.objects.get(id=memory.id)


def _mutate_visibility_metadata(memory: Memory, key: str, value: object) -> Memory:
    metadata = dict(memory.metadata)
    visibility = dict(metadata['digest_visibility'])
    visibility[key] = value
    metadata['digest_visibility'] = visibility
    memory.metadata = metadata
    memory.save(update_fields=['metadata', 'updated_at'])

    return _reload(memory)


@pytest.mark.django_db
def test_proven_weekly_digest_is_proven() -> None:
    digest_visibility = _load_digest_visibility()
    organization, project = make_org_project('proven')
    digest = build_proven_weekly_digest(organization, project)

    assert digest.metadata['digest_visibility']['schema'] == 'digest_visibility/v1'
    assert digest_visibility.proven_digest_memory(_reload(digest)) is True
    assert digest_visibility.digest_visibility_failure(_reload(digest)) is None


@pytest.mark.django_db
def test_legacy_digest_without_visibility_metadata_is_unproven() -> None:
    digest_visibility = _load_digest_visibility()
    organization, project = make_org_project('legacy')
    digest = build_legacy_digest(organization, project)

    assert 'digest_visibility' not in digest.metadata
    assert digest_visibility.proven_digest_memory(_reload(digest)) is False
    assert digest_visibility.digest_visibility_failure(_reload(digest)) == _UNPROVEN


@pytest.mark.django_db
def test_tampered_input_digest_is_unproven() -> None:
    digest_visibility = _load_digest_visibility()
    organization, project = make_org_project('tampered-input')
    digest = build_proven_weekly_digest(organization, project)
    tampered = _mutate_visibility_metadata(digest, 'input_digest', 'deadbeef' * 8)

    assert digest_visibility.proven_digest_memory(tampered) is False
    assert digest_visibility.digest_visibility_failure(tampered) == _UNPROVEN


@pytest.mark.django_db
def test_tampered_output_identity_is_unproven() -> None:
    digest_visibility = _load_digest_visibility()
    organization, project = make_org_project('tampered-identity')
    digest = build_proven_weekly_digest(organization, project)
    tampered = _mutate_visibility_metadata(digest, 'output_identity', 'f' * 64)

    assert digest_visibility.proven_digest_memory(tampered) is False
    assert digest_visibility.digest_visibility_failure(tampered) == _UNPROVEN


@pytest.mark.django_db
def test_missing_linked_work_is_unproven() -> None:
    digest_visibility = _load_digest_visibility()
    organization, project = make_org_project('missing-work')
    digest = build_proven_weekly_digest(organization, project)
    work_id = digest.metadata['digest_visibility']['workflow_work_id']
    WorkflowWork.objects.filter(id=work_id).delete()

    assert digest_visibility.proven_digest_memory(_reload(digest)) is False
    assert digest_visibility.digest_visibility_failure(_reload(digest)) == _UNPROVEN


@pytest.mark.django_db
def test_non_digest_memory_is_not_subject_to_predicate() -> None:
    digest_visibility = _load_digest_visibility()
    organization, project = make_org_project('non-digest')
    memory = make_source_memory(organization, project, title='Plain', body='plain body')

    assert memory.kind != 'digest'
    assert digest_visibility.proven_digest_memory(_reload(memory)) is True
    assert digest_visibility.digest_visibility_failure(_reload(memory)) is None
