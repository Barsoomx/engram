from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.db.models import Count, Max, Q
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

ACTIVE_SESSION_THRESHOLD_MINUTES = 15
CONNECTED_AGENTS_WINDOW_HOURS = 24
SESSIONS_DEFAULT_LIMIT = 50
ACTIVITY_DEFAULT_LIMIT = 50


def _project_scope_filter(organization: Organization, scope: EffectiveScope) -> Q:
    return Q(organization=organization, project_id__in=scope.project_ids)


def _is_full_org_admin(scope: EffectiveScope) -> bool:
    return bool(PROJECT_ADMIN_CAPABILITIES & set(scope.capabilities))


def get_overview_metrics(
    organization: Organization,
    scope: EffectiveScope,
) -> dict[str, Any]:
    now = timezone.now()
    seven_days_ago = now - timedelta(days=7)
    fourteen_days_ago = now - timedelta(days=14)
    twenty_four_hours_ago = now - timedelta(hours=CONNECTED_AGENTS_WINDOW_HOURS)

    base = _project_scope_filter(organization, scope)

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

    return {
        'memories_indexed': memories_total,
        'memories_indexed_delta': memories_current_7d - memories_prior_7d,
        'context_bundles_7d': bundles_current_7d,
        'context_bundles_7d_delta': bundles_current_7d - bundles_prior_7d,
        'connected_agents': connected_agents,
        'avg_retrieval_latency_ms': None,
        'avg_retrieval_latency_measured': False,
    }


def get_memory_ingest_daily(
    organization: Organization,
    scope: EffectiveScope,
) -> list[dict[str, Any]]:
    now = timezone.now()
    fourteen_days_ago = now - timedelta(days=14)

    base = _project_scope_filter(organization, scope)

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
) -> list[dict[str, Any]]:
    now = timezone.now()
    base = _project_scope_filter(organization, scope)
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
                'model_id': '',
                'status': status,
                'last_seen': last_seen.isoformat(),
            }
        )

    return results


def get_activity(
    organization: Organization,
    scope: EffectiveScope,
    limit: int = ACTIVITY_DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    if _is_full_org_admin(scope):
        base_filter = Q(organization=organization)
    else:
        base_filter = Q(organization=organization, project_id__in=scope.project_ids)

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
