from __future__ import annotations

from datetime import UTC, datetime
from io import StringIO

import pytest
from django.core.management import CommandError, call_command

from engram.core.models import (
    AuditEvent,
    Memory,
    MemoryCandidate,
    MemoryTransition,
    MemoryVersion,
    MemoryVersionSource,
    RetrievalDocument,
    WorkflowWork,
)
from engram.memory import consistency, transitions
from engram.memory.transitions_test_support import (
    candidate_in_scope,
    provenanced_candidate,
    transition_request,
)

Scope = tuple[object, object, object]

_EXACT_DOCUMENT_FIELDS = (
    'visibility_scope',
    'source_observation_ids',
    'file_paths',
    'symbols',
    'exact_terms',
    'full_text',
    'stale',
    'refuted',
    'metadata',
    'projection_contract_version',
    'exact_projection_hash',
)


def _promoted(suffix: str) -> tuple[object, Scope]:
    candidate, _source, scope = provenanced_candidate(suffix)
    result = transitions.PromoteMemoryCandidate().execute(transition_request(candidate))
    return result, scope


def _report_input(scope: Scope, *, as_of: datetime, **overrides: object) -> object:
    organization, project, _session = scope
    values = {
        'organization_id': organization.id,
        'project_id': project.id,
        'as_of': as_of,
        'after_id': None,
        'sample_limit': 20,
    }
    values.update(overrides)
    return consistency.ConsistencyReportInput(**values)


def _rebuild_input(scope: Scope, *, kind: str = 'exact', apply: bool = False, **overrides: object) -> object:
    organization, project, _session = scope
    values = {
        'organization_id': organization.id,
        'project_id': project.id,
        'as_of': datetime.now(UTC),
        'kind': kind,
        'apply': apply,
        'after_id': None,
        'batch_size': 200,
    }
    values.update(overrides)
    return consistency.RebuildProjectionInput(**values)


def _semantic_snapshot(
    memory_id: object,
    *,
    candidate_id: object | None = None,
    version_id: object | None = None,
    transition_id: object | None = None,
) -> dict[str, object]:
    memory = Memory.objects.get(id=memory_id)
    version = (
        MemoryVersion.objects.get(id=version_id)
        if version_id is not None
        else MemoryVersion.objects.filter(memory_id=memory_id).order_by('-version').first()
    )
    assert version is not None
    transition = MemoryTransition.objects.get(id=transition_id or memory.current_transition_id)
    audit = AuditEvent.objects.get(id=transition.audit_event_id)
    return {
        'candidate': list(
            MemoryCandidate.objects.filter(id=candidate_id).values('id', 'status', 'promoted_memory_id')
            if candidate_id is not None
            else MemoryCandidate.objects.filter(promoted_memory_id=memory_id).values(
                'id', 'status', 'promoted_memory_id'
            )
        ),
        'memory': Memory.objects.filter(id=memory_id)
        .values('id', 'current_version', 'current_transition_id', 'title', 'body', 'status', 'stale', 'refuted')
        .get(),
        'version': MemoryVersion.objects.filter(id=version.id)
        .values('id', 'memory_id', 'version', 'body', 'content_hash')
        .get(),
        'transition': MemoryTransition.objects.filter(id=transition.id).values().get(),
        'audit': AuditEvent.objects.filter(id=audit.id).values().get(),
        'sources': list(MemoryVersionSource.objects.filter(memory_version_id=version.id).values().order_by('id')),
    }


@pytest.mark.django_db(transaction=True)
@pytest.mark.transactional
def test_report_is_scoped_deterministic_and_read_only() -> None:
    target_result, target_scope = _promoted('consistency-target')
    foreign_result, _foreign_scope = _promoted('consistency-foreign')
    target_document = target_result.retrieval_document
    RetrievalDocument.objects.filter(id=target_document.id).update(
        full_text='corrupt exact text',
        exact_projection_hash='',
        projection_contract_version=0,
    )
    before = _semantic_snapshot(target_result.memory.id)
    as_of = datetime.now(UTC)
    input_data = _report_input(target_scope, as_of=as_of)

    first = consistency.MemoryConsistencyReporter().execute(input_data)
    second = consistency.MemoryConsistencyReporter().execute(input_data)

    assert [(issue.memory_id, issue.code, issue.classification) for issue in first.issues] == [
        (issue.memory_id, issue.code, issue.classification) for issue in second.issues
    ]
    assert first.next_after_id == second.next_after_id
    assert all(issue.memory_id == target_result.memory.id for issue in first.issues)
    assert all(issue.memory_id != foreign_result.memory.id for issue in first.issues)
    assert _semantic_snapshot(target_result.memory.id) == before


@pytest.mark.django_db(transaction=True)
@pytest.mark.transactional
def test_exact_rebuild_dry_run_is_inert_and_apply_changes_only_exact_fields() -> None:
    result, scope = _promoted('consistency-exact-rebuild')
    document = result.retrieval_document
    RetrievalDocument.objects.filter(id=document.id).update(
        full_text='corrupt exact text',
        exact_terms=['corrupt'],
        exact_projection_hash='',
        projection_contract_version=0,
    )
    document.refresh_from_db()
    before = {
        field.attname: getattr(document, field.attname)
        for field in document._meta.get_fields()
        if hasattr(field, 'attname')
    }
    semantic_before = _semantic_snapshot(result.memory.id)
    work_count_before = WorkflowWork.objects.filter(subject_id=document.id).count()

    dry_run = consistency.RebuildMemoryProjections().execute(_rebuild_input(scope))

    document.refresh_from_db()
    assert dry_run.changed == 0
    assert {
        field.attname: getattr(document, field.attname)
        for field in document._meta.get_fields()
        if hasattr(field, 'attname')
    } == before
    assert WorkflowWork.objects.filter(subject_id=document.id).count() == work_count_before
    assert _semantic_snapshot(result.memory.id) == semantic_before

    applied = consistency.RebuildMemoryProjections().execute(_rebuild_input(scope, apply=True))

    document.refresh_from_db()
    assert applied.changed == 1
    assert applied.skipped == 0
    assert document.full_text != 'corrupt exact text'
    assert document.exact_projection_hash
    assert document.projection_contract_version == 1
    for field in before:
        if field not in _EXACT_DOCUMENT_FIELDS and field not in {'updated_at'}:
            assert getattr(document, field) == before[field]
    assert WorkflowWork.objects.filter(subject_id=document.id).count() == work_count_before
    assert _semantic_snapshot(result.memory.id) == semantic_before


@pytest.mark.django_db(transaction=True)
@pytest.mark.transactional
def test_exact_rebuild_apply_is_idempotent() -> None:
    result, scope = _promoted('consistency-exact-idempotent')
    RetrievalDocument.objects.filter(id=result.retrieval_document.id).update(
        full_text='corrupt exact text',
        exact_projection_hash='',
        projection_contract_version=0,
    )

    first = consistency.RebuildMemoryProjections().execute(_rebuild_input(scope, apply=True))
    document = RetrievalDocument.objects.get(id=result.retrieval_document.id)
    stable = {field: getattr(document, field) for field in _EXACT_DOCUMENT_FIELDS}
    second = consistency.RebuildMemoryProjections().execute(_rebuild_input(scope, apply=True))

    document.refresh_from_db()
    assert first.changed == 1
    assert second.changed == 0
    assert second.skipped == 1
    assert {field: getattr(document, field) for field in _EXACT_DOCUMENT_FIELDS} == stable


@pytest.mark.django_db(transaction=True)
@pytest.mark.transactional
@pytest.mark.parametrize(
    ('mismatch', 'expected_code'),
    (
        ('candidate', 'candidate_transition_missing_or_mismatched'),
        ('current_transition', 'current_transition_missing_or_mismatched'),
        ('current_version', 'current_version_pointer_mismatched'),
        ('provenance', 'version_provenance_missing_or_mismatched'),
        ('audit', 'transition_audit_missing_or_mismatched'),
    ),
)
def test_authoritative_mismatches_are_report_only_and_never_mutated(
    mismatch: str,
    expected_code: str,
) -> None:
    result, scope = _promoted(f'consistency-authoritative-{mismatch}')
    memory = result.memory
    candidate = MemoryCandidate.objects.get(promoted_memory_id=memory.id)
    snapshot_ids = {
        'candidate_id': candidate.id,
        'version_id': result.memory_version.id,
        'transition_id': result.transition.id,
    }
    if mismatch in {'candidate', 'current_transition'}:
        original_candidate, source, _ = provenanced_candidate(f'consistency-wrong-{mismatch}')
        wrong_candidate, _ = candidate_in_scope(
            original_candidate,
            source,
            title=f'Wrong {mismatch}',
            body=f'Wrong {mismatch} body',
        )
        wrong_result = transitions.PromoteMemoryCandidate().execute(transition_request(wrong_candidate))
        if mismatch == 'candidate':
            MemoryCandidate.objects.filter(id=candidate.id).update(promoted_memory_id=wrong_result.memory.id)
        else:
            Memory.objects.filter(id=memory.id).update(current_transition_id=wrong_result.transition.id)
    elif mismatch == 'current_version':
        Memory.objects.filter(id=memory.id).update(current_version=memory.current_version + 1)
    elif mismatch == 'provenance':
        source = MemoryVersionSource.objects.get(memory_version_id=result.memory_version.id)
        MemoryVersionSource.objects.filter(id=source.id).update(source_content_hash='f' * 64)
    else:
        AuditEvent.objects.filter(id=result.transition.audit_event_id).update(metadata={})

    before = _semantic_snapshot(memory.id, **snapshot_ids)
    report = consistency.MemoryConsistencyReporter().execute(_report_input(scope, as_of=datetime.now(UTC)))

    issues = [issue for issue in report.issues if issue.code == expected_code]
    assert len(issues) == 1
    assert issues[0].classification == 'report_only'
    assert _semantic_snapshot(memory.id, **snapshot_ids) == before


@pytest.mark.parametrize('sample_limit', (0, 21))
@pytest.mark.django_db
def test_report_rejects_sample_limit_outside_contract(sample_limit: int) -> None:
    _result, scope = _promoted(f'consistency-invalid-sample-{sample_limit}')
    with pytest.raises(ValueError):
        consistency.MemoryConsistencyReporter().execute(
            _report_input(scope, as_of=datetime.now(UTC), sample_limit=sample_limit)
        )


@pytest.mark.parametrize('batch_size', (0, 201))
@pytest.mark.django_db
def test_rebuild_rejects_batch_size_outside_contract(batch_size: int) -> None:
    _result, scope = _promoted(f'consistency-invalid-batch-{batch_size}')
    with pytest.raises(ValueError):
        consistency.RebuildMemoryProjections().execute(_rebuild_input(scope, batch_size=batch_size))


@pytest.mark.django_db
def test_rebuild_rejects_invalid_kind_and_cross_scope() -> None:
    _result, scope = _promoted('consistency-scope-validation')
    _foreign_result, foreign_scope = _promoted('consistency-scope-validation-foreign')
    organization, _project, _session = scope
    _foreign_organization, foreign_project, _foreign_session = foreign_scope

    with pytest.raises(ValueError):
        consistency.RebuildMemoryProjections().execute(_rebuild_input(scope, kind='semantic'))
    with pytest.raises(ValueError):
        consistency.RebuildMemoryProjections().execute(
            consistency.RebuildProjectionInput(
                organization_id=organization.id,
                project_id=foreign_project.id,
                as_of=datetime.now(UTC),
                kind='exact',
                apply=False,
                after_id=None,
                batch_size=200,
            )
        )
    assert organization.id != _foreign_organization.id


@pytest.mark.django_db(transaction=True)
@pytest.mark.transactional
def test_rebuild_command_defaults_to_dry_run() -> None:
    result, scope = _promoted('consistency-command-dry-run')
    organization, project, _session = scope
    RetrievalDocument.objects.filter(id=result.retrieval_document.id).update(
        full_text='corrupt exact text',
        exact_projection_hash='',
        projection_contract_version=0,
    )
    output = StringIO()

    call_command(
        'engram_rebuild_memory_projections',
        organization=str(organization.id),
        project=str(project.id),
        kind='exact',
        stdout=output,
    )

    document = RetrievalDocument.objects.get(id=result.retrieval_document.id)
    assert document.full_text == 'corrupt exact text'
    assert output.getvalue()


@pytest.mark.django_db
def test_consistency_command_validates_one_project_scope() -> None:
    _result, scope = _promoted('consistency-command-scope')
    _foreign_result, foreign_scope = _promoted('consistency-command-scope-foreign')
    organization, project, _session = scope
    _foreign_organization, foreign_project, _foreign_session = foreign_scope

    with pytest.raises(CommandError):
        call_command(
            'engram_memory_consistency',
            organization=str(organization.id),
            project=str(foreign_project.id),
        )
    assert project.organization_id == organization.id
