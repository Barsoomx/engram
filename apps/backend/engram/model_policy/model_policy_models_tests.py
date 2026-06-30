from __future__ import annotations

from engram.model_policy.models import ProviderCallRecord


def index_field_sets(model: type) -> set[tuple[str, ...]]:
    return {tuple(index.fields) for index in model._meta.indexes}


def test_provider_call_record_has_idempotency_lookup_composite_index() -> None:
    assert ('organization', 'project', 'task_type', 'request_id') in index_field_sets(ProviderCallRecord)
