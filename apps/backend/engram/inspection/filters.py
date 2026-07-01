from __future__ import annotations

import django_filters

from engram.core.api.filters import SinceUntilFilterSet
from engram.core.models import AuditEvent, ContextBundle, Memory


class InspectionMemoryFilterSet(django_filters.FilterSet):
    status = django_filters.CharFilter(field_name='status')
    kind = django_filters.CharFilter(field_name='kind')

    class Meta:
        model = Memory
        fields = ['status', 'kind']


class InspectionContextBundleFilterSet(SinceUntilFilterSet):
    class Meta:
        model = ContextBundle
        fields = ['since', 'until']


class InspectionAuditEventFilterSet(SinceUntilFilterSet):
    event_type = django_filters.CharFilter(field_name='event_type')
    correlation_id = django_filters.CharFilter(field_name='correlation_id')

    class Meta:
        model = AuditEvent
        fields = ['event_type', 'correlation_id', 'since', 'until']
