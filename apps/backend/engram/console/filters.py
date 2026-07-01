from __future__ import annotations

import datetime

import django_filters
from django.db.models import Q, QuerySet
from django.utils import timezone

from engram.core.models import AuditEvent, Memory, MemoryCandidate, WorkflowRun


class AuditEventFilterSet(django_filters.FilterSet):
    event_type = django_filters.CharFilter(field_name='event_type')
    result = django_filters.CharFilter(field_name='result')
    actor_id = django_filters.CharFilter(field_name='actor_id')
    target_type = django_filters.CharFilter(field_name='target_type')
    project_id = django_filters.UUIDFilter(field_name='project_id')
    team_id = django_filters.UUIDFilter(field_name='team_id')
    created_at__gte = django_filters.IsoDateTimeFilter(field_name='created_at', lookup_expr='gte')
    created_at__lt = django_filters.IsoDateTimeFilter(field_name='created_at', lookup_expr='lt')

    class Meta:
        model = AuditEvent
        fields = [
            'event_type',
            'result',
            'actor_id',
            'target_type',
            'project_id',
            'team_id',
            'created_at__gte',
            'created_at__lt',
        ]


class WorkflowRunFilterSet(django_filters.FilterSet):
    run_type = django_filters.CharFilter(field_name='run_type')
    status = django_filters.CharFilter(field_name='status')
    project_id = django_filters.UUIDFilter(field_name='project_id')
    team_id = django_filters.UUIDFilter(field_name='team_id')
    escalation = django_filters.BooleanFilter(field_name='escalation')
    created_at__gte = django_filters.IsoDateTimeFilter(field_name='created_at', lookup_expr='gte')
    created_at__lte = django_filters.IsoDateTimeFilter(field_name='created_at', lookup_expr='lte')

    class Meta:
        model = WorkflowRun
        fields = [
            'run_type',
            'status',
            'project_id',
            'team_id',
            'escalation',
            'created_at__gte',
            'created_at__lte',
        ]


class _MemoryReviewFilterSetBase(django_filters.FilterSet):
    team_id = django_filters.UUIDFilter(field_name='team_id')
    project_id = django_filters.UUIDFilter(field_name='project_id')
    visibility_scope = django_filters.CharFilter(field_name='visibility_scope')
    search = django_filters.CharFilter(method='filter_search')
    confidence__gte = django_filters.NumberFilter(field_name='confidence', lookup_expr='gte')
    confidence__lte = django_filters.NumberFilter(field_name='confidence', lookup_expr='lte')
    age_days__gte = django_filters.NumberFilter(method='filter_age_days_gte')

    def filter_search(self, queryset: QuerySet, name: str, value: str) -> QuerySet:
        return queryset.filter(Q(title__icontains=value) | Q(body__icontains=value))

    def filter_age_days_gte(
        self,
        queryset: QuerySet,
        name: str,
        value: object,
    ) -> QuerySet:
        try:
            days = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            days = 0

        if days > 0:
            cutoff = timezone.now() - datetime.timedelta(days=days)

            return queryset.filter(created_at__lte=cutoff)

        return queryset


class MemoryReviewCandidateFilterSet(_MemoryReviewFilterSetBase):
    source_type = django_filters.CharFilter(method='filter_source_type')

    class Meta:
        model = MemoryCandidate
        fields = [
            'team_id',
            'project_id',
            'visibility_scope',
            'search',
            'source_type',
            'confidence__gte',
            'confidence__lte',
            'age_days__gte',
        ]

    def filter_source_type(self, queryset: QuerySet, name: str, value: str) -> QuerySet:
        return queryset.filter(source_observation__sources__source_type=value).distinct()


class MemoryReviewMemoryFilterSet(_MemoryReviewFilterSetBase):
    source_type = django_filters.CharFilter(method='filter_source_type')

    class Meta:
        model = Memory
        fields = [
            'team_id',
            'project_id',
            'visibility_scope',
            'search',
            'source_type',
            'confidence__gte',
            'confidence__lte',
            'age_days__gte',
        ]

    def filter_source_type(self, queryset: QuerySet, name: str, value: str) -> QuerySet:
        return queryset.filter(versions__source_observation__sources__source_type=value).distinct()
