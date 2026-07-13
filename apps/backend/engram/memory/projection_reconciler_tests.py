from __future__ import annotations

from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from engram.core.models import (
    Memory,
    MemoryStatus,
    MemoryVersion,
    Organization,
    Project,
    RetrievalDocument,
    VisibilityScope,
)
from engram.memory import projection_reconciler
from engram.memory.observation_work_tests import create_scope

Scope = tuple[Organization, Project, object]


def _memory(scope: Scope, suffix: str, *, body: str | None = None) -> Memory:
    organization, project, session = scope

    return Memory.objects.create(
        organization=organization,
        project=project,
        team=session.team,
        title=f'memory {suffix}',
        body=body or f'body {suffix}',
        status=MemoryStatus.APPROVED,
        visibility_scope=VisibilityScope.PROJECT,
        confidence=Decimal('0.900'),
        current_version=1,
    )


def _version(memory: Memory, *, body: str | None = None) -> MemoryVersion:
    return MemoryVersion.objects.create(
        organization=memory.organization,
        project=memory.project,
        memory=memory,
        version=memory.current_version,
        body=body if body is not None else memory.body,
        content_hash=f'version-hash-{memory.id}',
    )


def _document(memory: Memory, version: MemoryVersion) -> RetrievalDocument:
    return RetrievalDocument.objects.create(
        organization=memory.organization,
        project=memory.project,
        team=memory.team,
        memory=memory,
        memory_version=version,
        visibility_scope=memory.visibility_scope,
        full_text='retrieval text',
    )


def _inspect(scope: Scope, *, as_of: object) -> list[object]:
    organization, project, _session = scope

    return list(
        projection_reconciler.inspect_projection(
            organization_id=organization.id,
            project_id=project.id,
            as_of=as_of,
        )
    )


def _one(findings: list[object], code: str) -> object:
    matches = [finding for finding in findings if finding.code == code]
    assert len(matches) == 1, f'expected exactly one {code!r}, got {[f.code for f in findings]}'

    return matches[0]


@pytest.mark.django_db
def test_missing_current_version_is_reported_and_defers_to_cp4() -> None:
    scope = create_scope('projection-missing-version')
    memory = _memory(scope, '1')

    findings = _inspect(scope, as_of=timezone.now())

    finding = _one(findings, 'current_projection_missing_or_inconsistent')
    assert finding.entity_id == str(memory.id)
    assert finding.proposed_action == 'defer_to_cp4'
    assert finding.auto_repair_eligible is False


@pytest.mark.django_db
def test_coherent_memory_yields_no_finding() -> None:
    scope = create_scope('projection-coherent')
    memory = _memory(scope, '1')
    version = _version(memory)
    _document(memory, version)

    assert _inspect(scope, as_of=timezone.now()) == []


@pytest.mark.django_db
def test_findings_carry_ids_only_no_bodies() -> None:
    scope = create_scope('projection-content-free')
    memory = _memory(scope, 'secret-body')

    findings = _inspect(scope, as_of=timezone.now())

    finding = _one(findings, 'current_projection_missing_or_inconsistent')
    assert memory.body not in repr(finding)
    for field_name in ('title', 'body', 'full_text'):
        assert not hasattr(finding, field_name)


@pytest.mark.django_db
def test_inspector_is_read_only() -> None:
    scope = create_scope('projection-read-only')
    _memory(scope, '1')
    memories_before = Memory.objects.count()
    versions_before = MemoryVersion.objects.count()
    documents_before = RetrievalDocument.objects.count()

    with CaptureQueriesContext(connection) as queries:
        findings = _inspect(scope, as_of=timezone.now())

    assert findings
    writes = [
        entry['sql']
        for entry in queries.captured_queries
        if entry['sql'].strip().upper().startswith(('INSERT', 'UPDATE', 'DELETE'))
    ]
    assert writes == []
    assert Memory.objects.count() == memories_before
    assert MemoryVersion.objects.count() == versions_before
    assert RetrievalDocument.objects.count() == documents_before


@pytest.mark.django_db
def test_foreign_scope_negative_control() -> None:
    scope = create_scope('projection-owned')
    foreign = create_scope('projection-foreign')
    _memory(scope, '1')

    assert _inspect(foreign, as_of=timezone.now()) == []
