from __future__ import annotations

import ast
import inspect
import uuid
from pathlib import Path

import pytest

from engram.access.models import Identity, IdentityType
from engram.access.services import EffectiveScope
from engram.console.services import edit_memory_body
from engram.core.models import (
    AuditEvent,
    Memory,
    MemoryReviewExample,
    MemoryTransition,
    MemoryVersion,
    RetrievalDocument,
)
from engram.memory.services import MemoryFeedbackInput, RecordMemoryFeedback
from engram.memory.transitions_test_support import (
    provenanced_candidate,
    transition_request,
    transitions_module,
)


def _promoted_memory(suffix: str) -> tuple[Memory, object]:
    candidate, _source, scope = provenanced_candidate(suffix)
    result = transitions_module().PromoteMemoryCandidate().execute(transition_request(candidate))

    result.memory.refresh_from_db()

    return result.memory, scope


def _feedback_scope(memory: Memory) -> EffectiveScope:
    return EffectiveScope(
        organization_id=memory.organization_id,
        identity_id=uuid.uuid4(),
        api_key_id=uuid.uuid4(),
        project_ids=(memory.project_id,),
        team_ids=(memory.team_id,) if memory.team_id else (),
        capabilities=('memories:review',),
        actor_type='api_key',
        actor_id='c43-feedback-adapter',
        project_bound=True,
    )


def _chain_counts(memory: Memory) -> dict[str, int]:
    return {
        'versions': MemoryVersion.objects.filter(memory=memory).count(),
        'documents': RetrievalDocument.objects.filter(memory=memory).count(),
        'transitions': MemoryTransition.objects.filter(memory=memory).count(),
        'audits': AuditEvent.objects.filter(project=memory.project).count(),
        'review_examples': MemoryReviewExample.objects.filter(item_id=str(memory.id)).count(),
    }


def _ast_call_path(node: ast.Call) -> tuple[str, ...]:
    parts: list[str] = []
    cursor: ast.AST = node.func
    while True:
        if isinstance(cursor, ast.Attribute):
            parts.append(cursor.attr)
            cursor = cursor.value
        elif isinstance(cursor, ast.Call):
            cursor = cursor.func
        elif isinstance(cursor, ast.Name):
            parts.append(cursor.id)
            break
        else:
            break
    return tuple(reversed(parts))


def test_c43_writer_census_has_no_publishable_memory_writes_outside_typed_modules() -> None:
    package_root = Path(inspect.getfile(transitions_module())).parents[1]
    allowed = {
        Path('memory/transitions.py'),
        Path('memory/projections.py'),
        Path('memory/consistency.py'),
    }
    semantic_models = {
        'Memory',
        'MemoryVersion',
        'MemoryVersionSource',
        'MemoryLink',
        'MemoryConflict',
        'MemoryTransition',
        'RetrievalDocument',
    }
    write_methods = {
        'bulk_create',
        'create',
        'delete',
        'get_or_create',
        'update',
        'update_or_create',
    }
    allowed_operational_writes = {
        ('console/views/settings.py', 'Memory', 'delete'),
        ('memory/conflict_links.py', 'MemoryLink', 'delete'),
        ('memory/services.py', 'MemoryLink', 'get_or_create'),
    }
    violations: list[str] = []

    for path in sorted(package_root.rglob('*.py')):
        if path.name.endswith('_tests.py') or path.name.endswith('_test_support.py') or 'migrations' in path.parts:
            continue
        relative = path.relative_to(package_root)
        if relative in allowed:
            continue
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            call_path = _ast_call_path(node)
            matched_models = semantic_models.intersection(call_path)
            if not call_path or call_path[-1] not in write_methods or not matched_models:
                continue
            if any(
                (relative.as_posix(), model, call_path[-1]) in allowed_operational_writes for model in matched_models
            ):
                continue
            violations.append(f'{relative}:{node.lineno}: {".".join(call_path)}')

    assert violations == [], 'publishable memory writes bypass typed modules:\n' + '\n'.join(violations)


@pytest.mark.django_db
def test_console_edit_preserves_response_keys_and_commits_uniform_revision_chain() -> None:
    memory, _scope = _promoted_memory('c43-console-edit-shape')
    actor = Identity.objects.create(
        organization=memory.organization,
        identity_type=IdentityType.USER,
        external_id='c43-console-editor',
        display_name='C4.3 console editor',
    )
    prior_version = MemoryVersion.objects.get(memory=memory, version=memory.current_version)
    before_transition_count = MemoryTransition.objects.filter(memory=memory).count()
    before_commit_audit_count = AuditEvent.objects.filter(
        project=memory.project,
        event_type='MemoryTransitionCommitted',
    ).count()

    version = edit_memory_body(
        memory.organization,
        actor,
        memory,
        'edited by the converged console writer',
        'C4.3 adapter contract',
    )

    assert isinstance(version, MemoryVersion)
    memory.refresh_from_db()
    version = MemoryVersion.objects.get(memory=memory, version=memory.current_version)
    transition = MemoryTransition.objects.get(memory=memory, transition_type='revise')
    document = RetrievalDocument.objects.get(id=transition.result_exact_document_id)

    assert transition.from_version_id == prior_version.id
    assert transition.to_version_id == version.id
    assert transition.result_memory_id == memory.id
    assert transition.result_version_id == version.id
    assert transition.exact_document_id == transition.result_exact_document_id == document.id
    assert document.memory_id == memory.id
    assert document.memory_version_id == version.id
    assert document.projection_contract_version == 1
    assert transition.audit_event.event_type == 'MemoryTransitionCommitted'
    assert transition.audit_event.metadata['schema'] == 'memory_transition/v1'
    assert transition.audit_event.metadata['transition_id'] == str(transition.id)
    assert memory.current_transition_id == transition.id
    assert MemoryTransition.objects.filter(memory=memory).count() == before_transition_count + 1
    assert (
        AuditEvent.objects.filter(project=memory.project, event_type='MemoryTransitionCommitted').count()
        == before_commit_audit_count + 1
    )


@pytest.mark.django_db
def test_feedback_preserves_response_keys_and_commits_uniform_stale_chain() -> None:
    memory, _scope = _promoted_memory('c43-feedback-shape')
    before_transition_count = MemoryTransition.objects.filter(memory=memory).count()
    before_commit_audit_count = AuditEvent.objects.filter(
        project=memory.project,
        event_type='MemoryTransitionCommitted',
    ).count()
    result = RecordMemoryFeedback().execute(
        MemoryFeedbackInput(
            scope=_feedback_scope(memory),
            memory_id=memory.id,
            project_id=memory.project_id,
            team_id=memory.team_id,
            action='stale',
            reason='C4.3 adapter contract',
            request_id='c43-feedback-shape-request',
            correlation_id='c43-feedback-shape-correlation',
        ),
    )
    response = result.to_response()

    required_keys = {
        'memory_id',
        'project_id',
        'team_id',
        'action',
        'stale',
        'refuted',
        'retrieval_documents_updated',
        'already_applied',
    }
    transition = MemoryTransition.objects.get(memory=memory, transition_type='mark_stale')
    document = RetrievalDocument.objects.get(id=transition.result_exact_document_id)
    memory.refresh_from_db()

    assert required_keys <= response.keys()
    assert response['memory_id'] == str(memory.id)
    assert response['action'] == 'stale'
    assert response['stale'] is True
    assert response.get('transition_id', str(transition.id)) == str(transition.id)
    assert transition.from_version_id == transition.to_version_id == transition.result_version_id
    assert transition.result_memory_id == memory.id
    assert transition.exact_document_id == transition.result_exact_document_id == document.id
    assert document.memory_id == memory.id
    assert document.memory_version_id == transition.to_version_id
    assert document.stale is True
    assert document.projection_contract_version == 1
    assert transition.audit_event.event_type == 'MemoryTransitionCommitted'
    assert transition.audit_event.metadata['schema'] == 'memory_transition/v1'
    assert memory.current_transition_id == transition.id
    assert MemoryTransition.objects.filter(memory=memory).count() == before_transition_count + 1
    assert (
        AuditEvent.objects.filter(project=memory.project, event_type='MemoryTransitionCommitted').count()
        == before_commit_audit_count + 1
    )


class InjectedC43Error(RuntimeError):
    pass


@pytest.mark.django_db(transaction=True)
@pytest.mark.transactional
@pytest.mark.parametrize('boundary', ('exact_document', 'audit'))
def test_console_edit_outer_transaction_rolls_back_typed_chain_faults(
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    memory, _scope = _promoted_memory(f'c43-console-edit-rollback-{boundary}')
    actor = Identity.objects.create(
        organization=memory.organization,
        identity_type=IdentityType.USER,
        external_id=f'c43-console-editor-{boundary}',
        display_name='C4.3 console editor',
    )
    before_counts = _chain_counts(memory)
    before_state = (memory.body, memory.current_version, memory.current_transition_id)

    def fault(point: str) -> None:
        if point == boundary:
            raise InjectedC43Error(point)

    monkeypatch.setattr(transitions_module(), '_fault_boundary', fault)
    with pytest.raises(InjectedC43Error, match=boundary):
        edit_memory_body(
            memory.organization,
            actor,
            memory,
            'must not survive the fault',
            'C4.3 rollback contract',
        )

    memory.refresh_from_db()
    assert (memory.body, memory.current_version, memory.current_transition_id) == before_state
    assert _chain_counts(memory) == before_counts


@pytest.mark.django_db(transaction=True)
@pytest.mark.transactional
@pytest.mark.parametrize('boundary', ('exact_document', 'audit'))
def test_feedback_outer_transaction_rolls_back_typed_chain_faults(
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    memory, _scope = _promoted_memory(f'c43-feedback-rollback-{boundary}')
    before_counts = _chain_counts(memory)
    before_state = (memory.stale, memory.refuted, memory.current_transition_id)

    def fault(point: str) -> None:
        if point == boundary:
            raise InjectedC43Error(point)

    monkeypatch.setattr(transitions_module(), '_fault_boundary', fault)
    with pytest.raises(InjectedC43Error, match=boundary):
        RecordMemoryFeedback().execute(
            MemoryFeedbackInput(
                scope=_feedback_scope(memory),
                memory_id=memory.id,
                project_id=memory.project_id,
                team_id=memory.team_id,
                action='stale',
                reason='C4.3 rollback contract',
                request_id=f'c43-feedback-rollback-{boundary}',
                correlation_id=f'c43-feedback-rollback-correlation-{boundary}',
            ),
        )

    memory.refresh_from_db()
    assert (memory.stale, memory.refuted, memory.current_transition_id) == before_state
    assert _chain_counts(memory) == before_counts
