from __future__ import annotations

from engram.console.model_presets import ALL_TASK_TYPES, PRESETS


def test_all_task_types_has_exactly_four_real_types() -> None:
    assert ALL_TASK_TYPES == ('generation', 'embedding', 'curation', 'digest')


def test_no_preset_contains_rerank_or_admin_assistant_task_model() -> None:
    for preset in PRESETS:
        task_types = {tm['task_type'] for tm in preset['task_models']}
        assert 'rerank' not in task_types, f'preset {preset["key"]} still has rerank'
        assert 'admin_assistant' not in task_types, f'preset {preset["key"]} still has admin_assistant'
