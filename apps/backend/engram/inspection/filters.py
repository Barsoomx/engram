from __future__ import annotations

import django_filters
from django.db.models import Q, QuerySet

from engram.core.api.filters import SinceUntilFilterSet
from engram.core.models import AuditEvent, ContextBundle, Memory


class InspectionMemoryFilterSet(django_filters.FilterSet):
    status = django_filters.CharFilter(field_name='status')
    kind = django_filters.CharFilter(field_name='kind')
    search = django_filters.CharFilter(method='filter_search')

    class Meta:
        model = Memory
        fields = ['status', 'kind', 'search']

    def filter_search(self, queryset: QuerySet, name: str, value: str) -> QuerySet:
        return queryset.filter(Q(title__icontains=value) | Q(body__icontains=value))


class InspectionContextBundleFilterSet(SinceUntilFilterSet):
    status = django_filters.CharFilter(field_name='status')
    session_id = django_filters.UUIDFilter(field_name='session_id')

    class Meta:
        model = ContextBundle
        fields = ['since', 'until', 'status', 'session_id']


class InspectionAuditEventFilterSet(SinceUntilFilterSet):
    event_type = django_filters.CharFilter(field_name='event_type')
    correlation_id = django_filters.CharFilter(field_name='correlation_id')

    class Meta:
        model = AuditEvent
        fields = ['event_type', 'correlation_id', 'since', 'until']
