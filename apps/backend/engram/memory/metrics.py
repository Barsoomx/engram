from __future__ import annotations

from engram.core.observability.metrics import Counter

consistency_issues_total = Counter(
    name='engram_memory_consistency_issues_total',
    help_text='Total memory consistency issues observed.',
    label_names=('code', 'classification'),
)

projection_rebuilds_total = Counter(
    name='engram_memory_projection_rebuilds_total',
    help_text='Total memory projection rebuild outcomes.',
    label_names=('kind', 'mode', 'outcome'),
)
