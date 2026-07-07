from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import structlog
from django.core.cache import cache
from django.db.models import Avg, Count, Max, Q
from django.db.models.functions import TruncDate
from django.utils import timezone

from engram.access.auth_services import PROJECT_ADMIN_CAPABILITIES
from engram.access.services import EffectiveScope
from engram.core.models import (
    AgentSession,
    AuditEvent,
    ContextBundle,
    Memory,
    MemoryCandidate,
    MemoryStatus,
    Organization,
    RawEventEnvelope,
)

logger = structlog.get_logger(__name__)

ACTIVE_SESSION_THRESHOLD_MINUTES = 15
CONNECTED_AGENTS_WINDOW_HOURS = 24
SESSIONS_DEFAULT_LIMIT = 50
ACTIVITY_DEFAULT_LIMIT = 50
OVERVIEW_METRICS_CACHE_TTL_SECONDS = 30


def _project_scope_filter(organization: Organization, scope: EffectiveScope) -> Q:
    return Q(organization=organization, project_id__in=scope.project_ids)


def _narrow_scope(
    scope: EffectiveScope,
    project_id: uuid.UUID | None,
    team_id: uuid.UUID | None,
) -> tuple[tuple[uuid.UUID, ...], uuid.UUID | None, bool]:
    project_ids = scope.project_ids
    out_of_scope = False

    if project_id is not None:
        if project_id in scope.project_ids:
            project_ids = (project_id,)
        else:
            out_of_scope = True

    resolved_team_id: uuid.UUID | None = None

    if team_id is not None:
        if team_id in scope.team_ids:
            resolved_team_id = team_id
        else:
            out_of_scope = True

    return project_ids, resolved_team_id, out_of_scope


def _scoped_base(
    organization: Organization,
    project_ids: tuple[uuid.UUID, ...],
    team_id: uuid.UUID | None,
) -> Q:
    base = Q(organization=organization, project_id__in=project_ids)

    if team_id is not None:
        base &= Q(team_id=team_id)

    return base


def _is_full_org_admin(scope: EffectiveScope) -> bool:
    return bool(PROJECT_ADMIN_CAPABILITIES & set(scope.capabilities))


def _overview_metrics_cache_key(
    organization: Organization,
    project_ids: tuple[uuid.UUID, ...],
    team_id: uuid.UUID | None,
) -> str:
    projects = ','.join(sorted(str(project_id) for project_id in project_ids))
    team = str(team_id) if team_id is not None else ''
    return f'console:overview_metrics:{organization.id}:{projects}:{team}'


def _empty_overview_metrics() -> dict[str, Any]:
    return {
        'memories_indexed': 0,
        'memories_indexed_delta': 0,
        'context_bundles_7d': 0,
        'context_bundles_7d_delta': 0,
        'connected_agents': 0,
        'avg_retrieval_latency_ms': None,
        'avg_retrieval_latency_measured': False,
    }


def get_overview_metrics(
    organization: Organization,
    scope: EffectiveScope,
    *,
    project_id: uuid.UUID | None = None,
    team_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    project_ids, resolved_team_id, out_of_scope = _narrow_scope(scope, project_id, team_id)

    if out_of_scope:
        return _empty_overview_metrics()

    cache_key = _overview_metrics_cache_key(organization, project_ids, resolved_team_id)

    try:
        cached = cache.get(cache_key)
    except Exception as e:
        logger.warning(
            'overview_metrics_cache_read_failed',
            cache_key=cache_key,
            error=e,
        )
        cached = None

    if cached is not None:
        return cached

    metrics = _compute_overview_metrics(organization, _scoped_base(organization, project_ids, resolved_team_id))

    try:
        cache.set(cache_key, metrics, OVERVIEW_METRICS_CACHE_TTL_SECONDS)
    except Exception as e:
        logger.warning(
            'overview_metrics_cache_write_failed',
            cache_key=cache_key,
            error=e,
        )

    return metrics


def _compute_overview_metrics(
    organization: Organization,
    base: Q,
) -> dict[str, Any]:
    now = timezone.now()
    seven_days_ago = now - timedelta(days=7)
    fourteen_days_ago = now - timedelta(days=14)
    twenty_four_hours_ago = now - timedelta(hours=CONNECTED_AGENTS_WINDOW_HOURS)

    memories_total = Memory.objects.filter(base, status=MemoryStatus.APPROVED).count()
    memories_current_7d = Memory.objects.filter(
        base,
        status=MemoryStatus.APPROVED,
        created_at__gte=seven_days_ago,
    ).count()
    memories_prior_7d = Memory.objects.filter(
        base,
        status=MemoryStatus.APPROVED,
        created_at__gte=fourteen_days_ago,
        created_at__lt=seven_days_ago,
    ).count()

    bundles_current_7d = ContextBundle.objects.filter(base, created_at__gte=seven_days_ago).count()
    bundles_prior_7d = ContextBundle.objects.filter(
        base,
        created_at__gte=fourteen_days_ago,
        created_at__lt=seven_days_ago,
    ).count()

    connected_agents = (
        AgentSession.objects.filter(base, updated_at__gte=twenty_four_hours_ago).values('agent_id').distinct().count()
    )

    avg_retrieval_latency_ms = ContextBundle.objects.filter(
        base,
        created_at__gte=seven_days_ago,
        retrieval_latency_ms__isnull=False,
    ).aggregate(avg_latency=Avg('retrieval_latency_ms'))['avg_latency']

    return {
        'memories_indexed': memories_total,
        'memories_indexed_delta': memories_current_7d - memories_prior_7d,
        'context_bundles_7d': bundles_current_7d,
        'context_bundles_7d_delta': bundles_current_7d - bundles_prior_7d,
        'connected_agents': connected_agents,
        'avg_retrieval_latency_ms': avg_retrieval_latency_ms,
        'avg_retrieval_latency_measured': avg_retrieval_latency_ms is not None,
    }


def get_memory_ingest_daily(
    organization: Organization,
    scope: EffectiveScope,
    *,
    project_id: uuid.UUID | None = None,
    team_id: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    project_ids, resolved_team_id, out_of_scope = _narrow_scope(scope, project_id, team_id)

    if out_of_scope:
        return []

    now = timezone.now()
    fourteen_days_ago = now - timedelta(days=14)

    base = _scoped_base(organization, project_ids, resolved_team_id)

    rows = (
        MemoryCandidate.objects.filter(base, created_at__gte=fourteen_days_ago)
        .annotate(date=TruncDate('created_at'))
        .values('date')
        .annotate(count=Count('id'))
        .order_by('date')
    )

    return [{'date': str(row['date']), 'count': row['count']} for row in rows]


def get_sessions(
    organization: Organization,
    scope: EffectiveScope,
    limit: int = SESSIONS_DEFAULT_LIMIT,
    *,
    project_id: uuid.UUID | None = None,
    team_id: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    project_ids, resolved_team_id, out_of_scope = _narrow_scope(scope, project_id, team_id)

    if out_of_scope:
        return []

    now = timezone.now()
    base = _scoped_base(organization, project_ids, resolved_team_id)
    active_cutoff = now - timedelta(minutes=ACTIVE_SESSION_THRESHOLD_MINUTES)

    sessions = list(AgentSession.objects.filter(base).select_related('agent').order_by('-updated_at')[:limit])

    session_ids = [s.id for s in sessions]

    latest_events = (
        RawEventEnvelope.objects.filter(
            organization=organization,
            session_id__in=session_ids,
        )
        .values('session_id')
        .annotate(last_seen=Max('received_at'))
    )
    last_seen_map = {row['session_id']: row['last_seen'] for row in latest_events}

    results = []
    for session in sessions:
        last_seen = last_seen_map.get(session.id) or session.updated_at
        status = 'active' if last_seen >= active_cutoff else 'idle'
        agent = session.agent
        agent_name = ''
        if agent is not None:
            agent_name = agent.display_name or agent.external_id

        results.append(
            {
                'session_id': str(session.id),
                'agent_name': agent_name,
                'model_id': session.model_id,
                'status': status,
                'last_seen': last_seen.isoformat(),
            }
        )

    return results


def get_activity(
    organization: Organization,
    scope: EffectiveScope,
    limit: int = ACTIVITY_DEFAULT_LIMIT,
    *,
    project_id: uuid.UUID | None = None,
    team_id: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    project_ids, resolved_team_id, out_of_scope = _narrow_scope(scope, project_id, team_id)

    if out_of_scope:
        return []

    if project_id is None and team_id is None:
        if _is_full_org_admin(scope):
            base_filter = Q(organization=organization)
        else:
            base_filter = Q(organization=organization, project_id__in=scope.project_ids)
    else:
        base_filter = _scoped_base(organization, project_ids, resolved_team_id)

    events = AuditEvent.objects.filter(base_filter).order_by('-created_at')[:limit]

    return [
        {
            'event_type': event.event_type,
            'actor_type': event.actor_type,
            'actor_id': str(event.actor_id),
            'target_type': event.target_type,
            'target_id': str(event.target_id),
            'result': event.result,
            'created_at': event.created_at.isoformat(),
        }
        for event in events
    ]
