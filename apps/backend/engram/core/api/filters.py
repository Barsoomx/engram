from __future__ import annotations

import django_filters


class SinceUntilFilterSet(django_filters.FilterSet):
    since = django_filters.IsoDateTimeFilter(field_name='created_at', lookup_expr='gte')
    until = django_filters.IsoDateTimeFilter(field_name='created_at', lookup_expr='lt')
