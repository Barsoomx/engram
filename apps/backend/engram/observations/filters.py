from __future__ import annotations

import django_filters

from engram.core.api.filters import SinceUntilFilterSet
from engram.core.models import Observation


class ObservationFilterSet(SinceUntilFilterSet):
    team_id = django_filters.UUIDFilter(field_name='team_id')
    observation_type = django_filters.CharFilter(field_name='observation_type')
    session_id = django_filters.UUIDFilter(field_name='session_id')
    correlation_id = django_filters.CharFilter(field_name='raw_event__correlation_id')

    class Meta:
        model = Observation
        fields = ['team_id', 'observation_type', 'session_id', 'correlation_id', 'since', 'until']
