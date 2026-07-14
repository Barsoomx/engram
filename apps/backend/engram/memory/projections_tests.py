from __future__ import annotations

import copy
import uuid
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest import mock

import pytest
from django.db import connection, transaction

from engram.core.models import Memory, MemoryVersion, RetrievalDocument, WorkflowWork
from engram.memory.workflow_work_tests import create_scope


def _projection_module() -> ModuleType:
    from engram.memory import projections

    return projections


def _projection_objects() -> tuple[SimpleNamespace, SimpleNamespace, SimpleNamespace, uuid.UUID]:
    organization_id = uuid.uuid4()
    project_id = uuid.uuid4()
    team_id = uuid.uuid4()
    memory_id = uuid.uuid4()
    version_id = uuid.uuid4()
    source_id = uuid.uuid4()
    transition_id = uuid.uuid4()
    source = SimpleNamespace(
        id=source_id,
        source_kind='candidate_source',
        source_type='candidate_source',
        kind='candidate_source',
        source_content_hash='a' * 64,
        content_hash='a' * 64,
        candidate_source_id=source_id,
        source_memory_version_id=None,
        memory_version_id=None,
    )
    memory = SimpleNamespace(
        id=memory_id,
        organization_id=organization_id,
        project_id=project_id,
        team_id=team_id,
        title='A memory title',
        body='A memory body',
        visibility_scope='project',
        status='approved',
        stale=False,
        refuted=False,
        file_paths=['src/memory.py'],
        symbols=['MemoryThing'],
        exact_terms=['memory'],
        source_observation_ids=[str(uuid.uuid4())],
        full_text='A memory title\nA memory body',
        metadata={
            'file_paths': ['src/memory.py'],
            'symbols': ['MemoryThing'],
            'exact_terms': ['memory'],
            'source_observation_ids': [],
            'full_text': 'A memory title\nA memory body',
        },
    )
    version = SimpleNamespace(
        id=version_id,
        memory_id=memory_id,
        organization_id=organization_id,
        project_id=project_id,
        team_id=team_id,
        content_hash='b' * 64,
        body=memory.body,
        file_paths=list(memory.file_paths),
        symbols=list(memory.symbols),
        exact_terms=list(memory.exact_terms),
        source_observation_ids=list(memory.source_observation_ids),
        full_text=memory.full_text,
        source_metadata=dict(memory.metadata),
    )

    return memory, version, source, transition_id


def _source_for_version(version: MemoryVersion) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        source_kind='memory_version',
        source_type='memory_version',
        kind='memory_version',
        source_content_hash=version.content_hash,
        content_hash=version.content_hash,
        candidate_source_id=None,
        source_memory_version_id=version.id,
        memory_version_id=version.id,
    )


def test_exact_projection_is_deterministic_and_provider_free() -> None:
    module = _projection_module()
    memory, version, source, transition_id = _projection_objects()

    first = module.build_exact_memory_projection(
        memory=memory,
        version=version,
        transition_id=transition_id,
        sources=[source],
    )
    second = module.build_exact_memory_projection(
        memory=copy.deepcopy(memory),
        version=copy.deepcopy(version),
        transition_id=transition_id,
        sources=[copy.deepcopy(source)],
    )

    assert first == second
    assert len(first.exact_projection_hash) == 64
    assert first.exact_projection_hash == first.exact_projection_hash.lower()
    assert 'embedding' not in first.document_values
    assert 'provider' not in first.document_values


@pytest.mark.parametrize(
    'changes',
    [
        pytest.param({'transition_id': uuid.uuid4()}, id='transition-id'),
        pytest.param(
            {
                'memory.id': uuid.UUID('00000000-0000-4000-8000-000000000011'),
                'version.memory_id': uuid.UUID('00000000-0000-4000-8000-000000000011'),
            },
            id='memory-id',
        ),
        pytest.param({'version.id': uuid.uuid4()}, id='version-id'),
        pytest.param({'version.content_hash': 'c' * 64}, id='content-hash'),
        pytest.param({'memory.title': 'Changed title'}, id='title'),
        pytest.param({'memory.body': 'Changed body', 'version.body': 'Changed body'}, id='body'),
        pytest.param(
            {
                'memory.organization_id': uuid.UUID('00000000-0000-4000-8000-000000000012'),
                'version.organization_id': uuid.UUID('00000000-0000-4000-8000-000000000012'),
            },
            id='organization-id',
        ),
        pytest.param(
            {
                'memory.project_id': uuid.UUID('00000000-0000-4000-8000-000000000013'),
                'version.project_id': uuid.UUID('00000000-0000-4000-8000-000000000013'),
            },
            id='project-id',
        ),
        pytest.param(
            {
                'memory.team_id': uuid.UUID('00000000-0000-4000-8000-000000000014'),
                'version.team_id': uuid.UUID('00000000-0000-4000-8000-000000000014'),
            },
            id='team-id',
        ),
        pytest.param({'memory.visibility_scope': 'team'}, id='visibility'),
        pytest.param({'memory.status': 'archived'}, id='status'),
        pytest.param({'memory.stale': True}, id='stale'),
        pytest.param({'memory.refuted': True}, id='refuted'),
        pytest.param(
            {
                'source.id': uuid.UUID('00000000-0000-4000-8000-000000000015'),
                'source.candidate_source_id': uuid.UUID('00000000-0000-4000-8000-000000000015'),
            },
            id='source-id',
        ),
        pytest.param(
            {'source.source_content_hash': 'd' * 64, 'source.content_hash': 'd' * 64},
            id='source-hash',
        ),
        pytest.param(
            {
                'memory.file_paths': ['src/other.py'],
                'memory.metadata.file_paths': ['src/other.py'],
                'version.file_paths': ['src/other.py'],
                'version.source_metadata.file_paths': ['src/other.py'],
            },
            id='file-paths',
        ),
        pytest.param(
            {
                'memory.symbols': ['OtherThing'],
                'memory.metadata.symbols': ['OtherThing'],
                'version.symbols': ['OtherThing'],
                'version.source_metadata.symbols': ['OtherThing'],
            },
            id='symbols',
        ),
        pytest.param(
            {
                'memory.exact_terms': ['other'],
                'memory.metadata.exact_terms': ['other'],
                'version.exact_terms': ['other'],
                'version.source_metadata.exact_terms': ['other'],
            },
            id='exact-terms',
        ),
        pytest.param(
            {
                'memory.source_observation_ids': ['00000000-0000-4000-8000-000000000001'],
                'memory.metadata.source_observation_ids': ['00000000-0000-4000-8000-000000000001'],
                'version.source_observation_ids': ['00000000-0000-4000-8000-000000000001'],
                'version.source_metadata.source_observation_ids': ['00000000-0000-4000-8000-000000000001'],
            },
            id='source-observation-ids',
        ),
        pytest.param(
            {
                'memory.full_text': 'Changed full text',
                'memory.metadata.full_text': 'Changed full text',
                'version.full_text': 'Changed full text',
                'version.source_metadata.full_text': 'Changed full text',
            },
            id='full-text',
        ),
    ],
)
def test_exact_projection_hash_changes_for_each_named_input(changes: dict[str, Any]) -> None:
    module = _projection_module()
    memory, version, source, transition_id = _projection_objects()
    baseline = module.build_exact_memory_projection(
        memory=memory,
        version=version,
        transition_id=transition_id,
        sources=[source],
    ).exact_projection_hash

    changed_memory = copy.deepcopy(memory)
    changed_version = copy.deepcopy(version)
    changed_source = copy.deepcopy(source)
    objects = {'memory': changed_memory, 'version': changed_version, 'source': changed_source}
    for path, value in changes.items():
        if path == 'transition_id':
            transition_id = value
            continue
        root, *parts = path.split('.')
        target = objects[root]
        for part in parts[:-1]:
            target = target[part] if isinstance(target, dict) else getattr(target, part)
        if isinstance(target, dict):
            target[parts[-1]] = copy.deepcopy(value)
        else:
            setattr(target, parts[-1], copy.deepcopy(value))

    changed = module.build_exact_memory_projection(
        memory=changed_memory,
        version=changed_version,
        transition_id=transition_id,
        sources=[changed_source],
    )
    assert changed.exact_projection_hash != baseline


@pytest.mark.django_db(transaction=True)
def test_exact_writer_marks_old_document_stale_and_clears_embedding_atomically() -> None:
    module = _projection_module()
    organization, team, project, _agent, _session = create_scope('projection-writer')
    memory = Memory.objects.create(
        organization=organization,
        project=project,
        team=team,
        title='Current memory',
        body='Current body',
        current_version=2,
    )
    old_version = MemoryVersion.objects.create(
        organization=organization,
        project=project,
        memory=memory,
        version=1,
        body='Old body',
        content_hash='1' * 64,
    )
    current_version = MemoryVersion.objects.create(
        organization=organization,
        project=project,
        memory=memory,
        version=2,
        body=memory.body,
        content_hash='2' * 64,
    )
    old_document = RetrievalDocument.objects.create(
        organization=organization,
        project=project,
        team=team,
        memory=memory,
        memory_version=old_version,
        full_text='Old body',
        embedding_reference='provider:old',
        embedding_vector=[0.1],
    )
    with mock.patch.object(module, 'get_provider_gateway', create=True) as provider_gateway:
        with transaction.atomic():
            document = module.write_exact_memory_projection(
                memory=memory,
                version=current_version,
                transition_id=uuid.uuid4(),
                sources=[_source_for_version(current_version)],
            )
    provider_gateway.assert_not_called()

    old_document.refresh_from_db()
    document.refresh_from_db()
    assert old_document.stale is True
    assert document.stale is False
    assert document.embedding_reference == ''
    assert document.embedding_vector == []
    assert getattr(document, 'embedding_projection_hash', '') == ''
    assert getattr(document, 'embedding_projected_at', None) is None


@pytest.mark.django_db(transaction=True)
def test_exact_writer_and_embedding_intent_roll_back_when_package_fails() -> None:
    module = _projection_module()
    organization, team, project, _agent, _session = create_scope('projection-rollback')
    memory = Memory.objects.create(
        organization=organization,
        project=project,
        team=team,
        title='Rollback memory',
        body='Rollback body',
    )
    version = MemoryVersion.objects.create(
        organization=organization,
        project=project,
        memory=memory,
        version=1,
        body=memory.body,
        content_hash='3' * 64,
    )
    with mock.patch('engram.memory.work_dispatch.app.send_task', side_effect=RuntimeError('broker down')):
        with pytest.raises(RuntimeError, match='broker down'):
            with transaction.atomic():
                document = module.write_exact_memory_projection(
                    memory=memory,
                    version=version,
                    transition_id=uuid.uuid4(),
                    sources=[_source_for_version(version)],
                )
                module.create_embedding_work_and_signal(document=document)

    assert RetrievalDocument.objects.filter(memory=memory).count() == 0
    assert WorkflowWork.objects.filter(project=project).count() == 0


@pytest.mark.django_db(transaction=True)
def test_embedding_work_snapshot_is_exact_and_signal_is_emitted_once() -> None:
    module = _projection_module()
    organization, team, project, _agent, _session = create_scope('embedding-intent')
    memory = Memory.objects.create(
        organization=organization,
        project=project,
        team=team,
        title='Embedding memory',
        body='Embedding body',
    )
    version = MemoryVersion.objects.create(
        organization=organization,
        project=project,
        memory=memory,
        version=1,
        body=memory.body,
        content_hash='4' * 64,
    )
    with transaction.atomic():
        document = module.write_exact_memory_projection(
            memory=memory,
            version=version,
            transition_id=uuid.uuid4(),
            sources=[_source_for_version(version)],
        )
        with mock.patch('engram.memory.work_dispatch.app.send_task') as send_task:
            work, created = module.create_embedding_work_and_signal(document=document)

    assert created is True
    assert work.subject_id == document.id
    assert work.input_snapshot == {
        'schema': 'memory_embedding/v1',
        'retrieval_document_id': str(document.id),
        'memory_id': str(memory.id),
        'memory_version_id': str(version.id),
        'exact_projection_hash': document.exact_projection_hash,
    }
    send_task.assert_called_once()
    assert connection.in_atomic_block is False


@pytest.mark.django_db(transaction=True)
def test_exact_projection_rejects_foreign_memory_version_scope_before_writes() -> None:
    module = _projection_module()
    organization, team, project, _agent, _session = create_scope('projection-scope')
    foreign_organization, _foreign_team, foreign_project, _foreign_agent, _foreign_session = create_scope(
        'projection-scope-foreign'
    )
    memory = Memory.objects.create(
        organization=organization,
        project=project,
        team=team,
        title='Scoped memory',
        body='Scoped body',
    )
    foreign_version = MemoryVersion.objects.create(
        organization=organization,
        project=project,
        memory=memory,
        version=1,
        body=memory.body,
        content_hash='5' * 64,
    )
    MemoryVersion.objects.filter(id=foreign_version.id).update(
        organization_id=foreign_organization.id,
        project_id=foreign_project.id,
    )
    foreign_version.refresh_from_db()

    with pytest.raises(Exception, match='scope'):
        with transaction.atomic():
            module.write_exact_memory_projection(
                memory=memory,
                version=foreign_version,
                transition_id=uuid.uuid4(),
                sources=[_source_for_version(foreign_version)],
            )

    assert RetrievalDocument.objects.filter(memory=memory).count() == 0
