import pytest


def _is_transactional_test(item: pytest.Item) -> bool:
    return item.path.name == 'migrations_tests.py' or any(
        marker.kwargs.get('transaction') is True or marker.kwargs.get('serialized_rollback') is True
        for marker in item.iter_markers(name='django_db')
    )


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if _is_transactional_test(item):
            item.add_marker(pytest.mark.transactional)


@pytest.fixture(autouse=True)
def _reset_candidate_decision_work_builder() -> object:
    from engram.memory import candidate_work_reconciler

    candidate_work_reconciler.set_candidate_decision_work_builder(None)
    yield None
    candidate_work_reconciler.set_candidate_decision_work_builder(None)
