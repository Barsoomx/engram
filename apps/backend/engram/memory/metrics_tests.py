from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from engram.core.models import RetrievalDocument
from engram.memory import consistency, metrics, transitions
from engram.memory.transitions_test_support import provenanced_candidate, transition_request


@pytest.fixture(autouse=True)
def f_reset_memory_counters() -> Iterator[None]:
    metrics.consistency_issues_total.reset()
    metrics.projection_rebuilds_total.reset()

    yield

    metrics.consistency_issues_total.reset()
    metrics.projection_rebuilds_total.reset()


def _promoted(suffix: str) -> tuple[object, tuple[object, object, object]]:
    candidate, _source, scope = provenanced_candidate(suffix)
    return transitions.PromoteMemoryCandidate().execute(transition_request(candidate)), scope


def _report_input(scope: tuple[object, object, object]) -> consistency.ConsistencyReportInput:
    organization, project, _session = scope
    return consistency.ConsistencyReportInput(
        organization_id=organization.id,
        project_id=project.id,
        as_of=datetime.now(UTC),
        after_id=None,
        sample_limit=20,
    )


def _rebuild_input(
    scope: tuple[object, object, object],
    *,
    kind: str = 'exact',
    apply: bool = False,
) -> consistency.RebuildProjectionInput:
    organization, project, _session = scope
    return consistency.RebuildProjectionInput(
        organization_id=organization.id,
        project_id=project.id,
        as_of=datetime.now(UTC),
        kind=kind,
        apply=apply,
        after_id=None,
        batch_size=200,
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.transactional
def test_consistency_reporter_counts_issues_by_bounded_code_and_classification() -> None:
    result, scope = _promoted('metrics-reporter')
    RetrievalDocument.objects.filter(id=result.retrieval_document.id).update(
        full_text='corrupt exact text',
        exact_projection_hash='',
        projection_contract_version=0,
    )

    report = consistency.MemoryConsistencyReporter().execute(_report_input(scope))

    assert any(issue.code == 'exact_projection_missing_or_mismatched' for issue in report.issues)
    assert (
        metrics.consistency_issues_total.value(
            code='exact_projection_missing_or_mismatched',
            classification='rebuild_exact',
        )
        == 1.0
    )
    assert all(
        set(labels) == {'code', 'classification'} for labels, _value in metrics.consistency_issues_total.samples()
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.transactional
def test_projection_rebuild_counts_kind_mode_and_outcome_only() -> None:
    result, scope = _promoted('metrics-rebuild')
    RetrievalDocument.objects.filter(id=result.retrieval_document.id).update(
        full_text='corrupt exact text',
        exact_projection_hash='',
        projection_contract_version=0,
    )

    consistency.RebuildMemoryProjections().execute(_rebuild_input(scope, apply=False))
    consistency.RebuildMemoryProjections().execute(_rebuild_input(scope, apply=True))
    consistency.RebuildMemoryProjections().execute(_rebuild_input(scope, kind='embedding', apply=False))

    assert metrics.projection_rebuilds_total.value(kind='exact', mode='dry_run', outcome='skipped') == 1.0
    assert metrics.projection_rebuilds_total.value(kind='exact', mode='apply', outcome='changed') == 1.0
    assert metrics.projection_rebuilds_total.value(kind='embedding', mode='dry_run', outcome='skipped') == 1.0
    assert all(
        set(labels) == {'kind', 'mode', 'outcome'} for labels, _value in metrics.projection_rebuilds_total.samples()
    )
