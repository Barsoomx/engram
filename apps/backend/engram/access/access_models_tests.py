from __future__ import annotations

from engram.access.models import ApiKey


def index_field_sets(model: type) -> set[tuple[str, ...]]:
    return {tuple(index.fields) for index in model._meta.indexes}


def test_api_key_has_key_prefix_lookup_index() -> None:
    assert ('key_prefix',) in index_field_sets(ApiKey)
