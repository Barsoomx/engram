'use client';

import { useQuery } from '@tanstack/react-query';
import { ArrowRight, ArrowUpRight, Plus, Shield, Sparkles } from 'lucide-react';
import Link from 'next/link';
import * as React from 'react';

import { ConnectAgentModal } from '@/components/connect/connect-agent-modal';
import { ErrorState } from '@/components/ui/error-state';
import { OpsStrip } from '@/components/ui/ops-strip';
import { PageHeader } from '@/components/ui/page-header';
import { PrimaryButton } from '@/components/ui/primary-button';
import { PulseDot } from '@/components/ui/pulse-dot';
import { TimeStamp } from '@/components/ui/time-stamp';
import {
  useActivity,
  useMemoryIngest,
  useMetricsOverview,
  useOpsOverview,
  useSessions,
} from '@/hooks/use-metrics';
import { apiClient } from '@/lib/auth';
import { getWeeklyDigest, type WeeklyDigest } from '@/lib/console-api';
import { auditResultColor, avatarColor } from '@/lib/design';
import type {
  ActivityEvent,
  MemoryIngestPoint,
  MetricsScopeParams,
  MetricsSession,
} from '@/lib/metrics-api';
import { useOrgStore } from '@/lib/org-store';
import { useProjectStore } from '@/lib/project-store';
import { useTeamStore } from '@/lib/team-store';

type HealthStatus = {
  ok: boolean;
  detail: string;
};

async function fetchHealth(): Promise<HealthStatus> {
  const client = apiClient();

  try {
    const response = await client.get('/-/readyz/', {
      headers: { Accept: 'application/json' },
      transformResponse: (data) => data,
    });

    return {
      ok: response.status >= 200 && response.status < 300,
      detail: typeof response.data === 'string' ? response.data : JSON.stringify(response.data),
    };
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);

    return {
      ok: false,
      detail: `Unreachable: ${message}`,
    };
  }
}

function formatCount(value: number | undefined): string {
  if (value === undefined || value === null) {
    return '—';
  }

  return value.toLocaleString();
}

function formatDelta(value: number | undefined): string | null {
  if (value === undefined || value === null || value === 0) {
    return null;
  }

  const sign = value > 0 ? '+' : '−';

  return `${sign}${Math.abs(value).toLocaleString()}`;
}

function humanizeEvent(value: string): string {
  const spaced = value
    .replace(/[_.]/g, ' ')
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .trim()
    .toLowerCase();

  if (!spaced) {
    return value;
  }

  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

function shortId(value: string): string {
  if (!value) {
    return '';
  }

  return value.length > 8 ? value.slice(0, 8) : value;
}

function activityHref(event: ActivityEvent): string | null {
  const id = event.target_id;

  switch (event.target_type) {
    case 'memory':
      return id ? `/memories/${id}` : '/memories';
    case 'memory_candidate':
      return '/memory-review';
    case 'workflow_run':
      return id ? `/workflow-runs/${id}` : '/workflow-runs';
    case 'context_bundle':
      return id ? `/context-bundles/${id}` : '/context-bundles';
    case 'identity':
      return '/members';
    case 'project':
      return '/projects';
    case 'team':
      return '/teams';
    case 'api_key':
      return '/api-keys';
    case 'organization':
      return '/organizations';
    default:
      return null;
  }
}

function isoUtcDate(date: Date): string {
  const year = date.getUTCFullYear();
  const month = String(date.getUTCMonth() + 1).padStart(2, '0');
  const day = String(date.getUTCDate()).padStart(2, '0');

  return `${year}-${month}-${day}`;
}

function build14DaySeries(points: MemoryIngestPoint[]): MemoryIngestPoint[] {
  const byDate = new Map(points.map((point) => [point.date, point.count]));

  let anchorIso = isoUtcDate(new Date());

  for (const point of points) {
    if (point.date > anchorIso) {
      anchorIso = point.date;
    }
  }

  const anchor = new Date(`${anchorIso}T00:00:00Z`);
  const series: MemoryIngestPoint[] = [];

  for (let offset = 13; offset >= 0; offset -= 1) {
    const day = new Date(anchor);
    day.setUTCDate(anchor.getUTCDate() - offset);
    const iso = isoUtcDate(day);

    series.push({ date: iso, count: byDate.get(iso) ?? 0 });
  }

  return series;
}

interface StatItem {
  label: string;
  value: string;
  delta: string | null;
  tone: 'success' | 'neutral';
}

function StatCard({ item }: { item: StatItem }) {
  const deltaClass = item.tone === 'success' ? 'text-success' : 'text-default-400';

  return (
    <div className='surface-card flex flex-col gap-3 p-[18px]'>
      <div className='flex items-start justify-between gap-2'>
        <span className='text-[11.5px] leading-tight text-default-500'>{item.label}</span>
        {item.delta && (
          <span className={`shrink-0 text-[11px] font-medium ${deltaClass}`}>{item.delta}</span>
        )}
      </div>
      <span className='tnum text-[27px] font-semibold leading-none tracking-[-0.02em] text-foreground'>
        {item.value}
      </span>
    </div>
  );
}

function PanelHeading({
  title,
  sub,
  right,
}: {
  title: string;
  sub?: string;
  right?: React.ReactNode;
}) {
  return (
    <div className='mb-4 flex items-start justify-between gap-3'>
      <div className='min-w-0'>
        <h3 className='text-[14.5px] font-semibold text-foreground'>{title}</h3>
        {sub && <p className='mt-0.5 text-[12px] text-default-500'>{sub}</p>}
      </div>
      {right && <div className='shrink-0'>{right}</div>}
    </div>
  );
}

function ingestLabels(points: MemoryIngestPoint[]): string[] {
  if (points.length === 0) {
    return [];
  }

  const picks = [0, Math.floor(points.length / 3), Math.floor((points.length * 2) / 3), points.length - 1];
  const seen = new Set<number>();

  return picks
    .filter((index) => {
      if (seen.has(index)) {
        return false;
      }

      seen.add(index);

      return true;
    })
    .map((index) => {
      const date = new Date(`${points[index].date}T00:00:00Z`);

      return Number.isNaN(date.getTime())
        ? points[index].date
        : date.toLocaleDateString('en-US', { month: 'short', day: 'numeric', timeZone: 'UTC' });
    });
}

function MemoryIngest({
  points,
  loading,
  isError,
  onRetry,
}: {
  points: MemoryIngestPoint[];
  loading: boolean;
  isError: boolean;
  onRetry: () => void;
}) {
  const series = React.useMemo(() => build14DaySeries(points), [points]);
  const max = Math.max(1, ...series.map((point) => point.count));
  const labels = ingestLabels(series);

  if (isError) {
    return (
      <ErrorState
        title='Memory ingest unavailable'
        message='Could not load ingest history.'
        onRetry={onRetry}
      />
    );
  }

  return (
    <div className='rounded-[18px] border border-divider bg-content1 p-5'>
      <PanelHeading
        title='Memory ingest'
        sub='Memories captured per day · last 14 days'
        right={
          <span className='rounded-[7px] bg-content2 px-2 py-0.5 font-mono text-[11px] text-default-500'>
            {loading ? '…' : `peak ${max}/day`}
          </span>
        }
      />
      <div className='flex h-[132px] items-end gap-[6px]'>
        {series.map((point, index) => {
          const pct = point.count > 0 ? Math.max(6, (point.count / max) * 100) : 0;

          return (
            <div
              key={point.date}
              title={`${point.date} · ${point.count}`}
              className='relative flex flex-1 items-end overflow-hidden rounded-[4px] bg-content2'
            >
              {pct > 0 && (
                <div
                  className='animate-bar-grow w-full transition-[filter] duration-150 hover:brightness-110'
                  style={{
                    height: `${pct}%`,
                    borderRadius: '4px 4px 3px 3px',
                    backgroundImage: 'linear-gradient(180deg,#8B6BFF,#5A3DF2)',
                    animationDelay: `${index * 30}ms`,
                  }}
                />
              )}
            </div>
          );
        })}
      </div>
      <div className='mt-3 flex justify-between font-mono text-[11px] text-default-400'>
        {labels.map((label, index) => (
          <span key={`${label}-${index}`}>{label}</span>
        ))}
      </div>
    </div>
  );
}

function AgentSessions({
  sessions,
  loading,
  isError,
  onRetry,
}: {
  sessions: MetricsSession[];
  loading: boolean;
  isError: boolean;
  onRetry: () => void;
}) {
  const [activeOnly, setActiveOnly] = React.useState(false);

  const activeCount = sessions.filter((session) => session.status === 'active').length;
  const visible = activeOnly ? sessions.filter((session) => session.status === 'active') : sessions;

  if (isError) {
    return (
      <ErrorState
        title='Agent sessions unavailable'
        message='Could not load agent sessions.'
        onRetry={onRetry}
      />
    );
  }

  return (
    <div className='rounded-[18px] border border-divider bg-content1 p-5'>
      <PanelHeading
        title='Agent sessions'
        sub={`${sessions.length} total · ${activeCount} active`}
        right={
          <button
            type='button'
            onClick={() => setActiveOnly((value) => !value)}
            className={`rounded-[8px] border px-2.5 py-1 text-[11.5px] font-medium transition-colors ${
              activeOnly
                ? 'border-primary/40 bg-primary/10 text-primary-300'
                : 'border-divider bg-content2 text-default-500 hover:text-foreground'
            }`}
          >
            Active only
          </button>
        }
      />
      {visible.length === 0 ? (
        <div className='flex h-[120px] items-center justify-center text-[12.5px] text-default-400'>
          {loading ? 'Loading…' : activeOnly ? 'No active sessions' : 'No agent sessions yet'}
        </div>
      ) : (
        <div className='max-h-[300px] space-y-2.5 overflow-y-auto pr-1'>
          {visible.map((session) => {
            const live = session.status === 'active';
            const color = avatarColor(session.agent_name || session.session_id);

            return (
              <div
                key={session.session_id}
                className='flex items-center gap-3 rounded-[12px] border border-divider bg-content2 px-3.5 py-3'
              >
                <span
                  className='flex h-8 w-8 shrink-0 items-center justify-center rounded-[9px]'
                  style={{ backgroundColor: `${color}1f`, color }}
                >
                  <Shield size={16} strokeWidth={2} />
                </span>
                <div className='min-w-0 flex-1'>
                  <div className='truncate text-[13px] font-semibold text-foreground'>
                    {session.agent_name || 'Unknown agent'}
                  </div>
                  <div className='truncate font-mono text-[11.5px] text-default-400'>
                    {session.model_id || '—'}
                  </div>
                </div>
                <div className='flex shrink-0 items-center gap-1.5 text-[11.5px]'>
                  <PulseDot color={live ? '#3DD9AC' : '#666C77'} pulse={live} size={6} />
                  <span className={live ? 'text-default-700' : 'text-default-500'}>
                    {live ? 'Active' : 'Idle'}
                  </span>
                  <span className='text-default-400'>
                    · <TimeStamp value={session.last_seen} />
                  </span>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function ActivityRow({ event }: { event: ActivityEvent }) {
  const href = activityHref(event);

  const body = (
    <>
      <span
        className='mt-1.5 h-2 w-2 shrink-0 rounded-full'
        style={{ backgroundColor: auditResultColor(event.result) }}
      />
      <div className='min-w-0 flex-1'>
        <div className='text-[13px] leading-snug text-default-700'>{humanizeEvent(event.event_type)}</div>
        <div className='mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-default-400'>
          <span className='rounded-[6px] bg-content2 px-2 py-0.5 font-mono'>
            {event.target_type}
            {event.target_id ? ` · ${shortId(event.target_id)}` : ''}
          </span>
          <TimeStamp value={event.created_at} />
        </div>
      </div>
      {href && <ArrowUpRight size={14} strokeWidth={2.2} className='mt-1 shrink-0 text-default-400' />}
    </>
  );

  if (href) {
    return (
      <Link
        href={href}
        className='flex items-start gap-3 rounded-[10px] px-3 py-2.5 transition-colors hover:bg-content2'
      >
        {body}
      </Link>
    );
  }

  return <div className='flex items-start gap-3 rounded-[10px] px-3 py-2.5'>{body}</div>;
}

function RecentActivity({
  events,
  loading,
  isError,
  onRetry,
}: {
  events: ActivityEvent[];
  loading: boolean;
  isError: boolean;
  onRetry: () => void;
}) {
  if (isError) {
    return (
      <ErrorState
        title='Activity unavailable'
        message='Could not load recent activity.'
        onRetry={onRetry}
      />
    );
  }

  return (
    <div className='rounded-[18px] border border-divider bg-content1 p-5'>
      <PanelHeading
        title='Recent activity'
        right={
          <Link
            href='/audit'
            className='inline-flex items-center gap-1 text-[12px] font-medium text-default-500 transition-colors hover:text-foreground'
          >
            View all
            <ArrowRight size={13} strokeWidth={2.2} />
          </Link>
        }
      />
      {events.length === 0 ? (
        <div className='flex h-[120px] items-center justify-center text-[12.5px] text-default-400'>
          {loading ? 'Loading…' : 'No recent activity'}
        </div>
      ) : (
        <div className='space-y-0.5'>
          {events.map((event, index) => (
            <ActivityRow key={`${event.created_at}-${index}`} event={event} />
          ))}
        </div>
      )}
    </div>
  );
}

function WeeklyDigestCard() {
  const activeProjectId = useProjectStore((s) => s.activeProjectId);
  const activeTeamId = useTeamStore((s) => s.activeTeamId);

  const digestQuery = useQuery<WeeklyDigest>({
    queryKey: ['digests', 'weekly', activeProjectId, activeTeamId],
    queryFn: () => getWeeklyDigest({ projectId: activeProjectId!, teamId: activeTeamId }),
    enabled: !!activeProjectId,
  });

  const digest = digestQuery.data;
  const generating = Boolean(digest && !digest.ready);

  return (
    <div className='relative overflow-hidden rounded-[18px] border border-primary/25 bg-primary-soft p-5'>
      <div className='flex items-center gap-3'>
        <span className='flex h-9 w-9 items-center justify-center rounded-[10px] bg-primary-gradient text-white shadow-primary-glow'>
          <Sparkles size={17} strokeWidth={2} />
        </span>
        <div>
          <div className='text-[14.5px] font-semibold text-foreground'>Weekly digest</div>
          {digest && (
            <div className='mt-0.5 flex items-center gap-1.5 text-[12px] text-primary-300'>
              {generating && <PulseDot color='#F2B765' pulse size={6} />}
              {digest.ready ? 'Ready' : 'Generating…'}
            </div>
          )}
        </div>
      </div>

      {!activeProjectId ? (
        <p className='mt-3 text-[12.5px] text-default-400'>
          Select a project to see the weekly digest.
        </p>
      ) : digestQuery.isError ? (
        <p className='mt-3 text-[12.5px] text-danger'>
          Could not load the weekly digest.
        </p>
      ) : digestQuery.isLoading ? (
        <div className='mt-3 h-[52px] animate-pulse rounded-[10px] bg-primary/10' />
      ) : !digest ? (
        <p className='mt-3 text-[12.5px] text-default-400'>
          No digest available for this project yet.
        </p>
      ) : generating ? (
        <p className='mt-3 text-[12.5px] text-default-500'>
          This week&apos;s digest is being generated. Counts and changelog appear once it is ready.
        </p>
      ) : (
        <>
          <p className='mt-3 text-[13px] text-default-500'>
            {digest.counts.added} added · {digest.counts.retired} retired this week
          </p>
          {digest.changelog.slice(0, 3).length > 0 && (
            <div className='mt-3 space-y-1.5'>
              {digest.changelog.slice(0, 3).map((item) => (
                <div
                  key={item.id}
                  className='flex items-center gap-2 rounded-[8px] bg-primary/5 px-3 py-2'
                >
                  <span className='min-w-0 flex-1 truncate text-[12px] font-medium text-foreground'>
                    {item.title || '(untitled)'}
                  </span>
                  <span className='shrink-0 text-[11px] text-default-400'>
                    <TimeStamp value={item.at} />
                  </span>
                </div>
              ))}
            </div>
          )}
          <Link
            href='/digests'
            className='mt-4 inline-flex items-center gap-1.5 rounded-[10px] border border-primary/20 bg-primary/5 px-3.5 py-2 text-[12.5px] font-semibold text-primary-300 transition-colors hover:bg-primary/10'
          >
            View digest
            <ArrowRight size={14} strokeWidth={2.2} />
          </Link>
        </>
      )}
    </div>
  );
}

export default function DashboardPage() {
  const activeOrgId = useOrgStore((state) => state.activeOrgId);
  const activeProjectId = useProjectStore((state) => state.activeProjectId);
  const activeTeamId = useTeamStore((state) => state.activeTeamId);

  const scope = React.useMemo<MetricsScopeParams | undefined>(() => {
    if (!activeProjectId && !activeTeamId) {
      return undefined;
    }

    const next: MetricsScopeParams = {};

    if (activeProjectId) {
      next.project_id = activeProjectId;
    }

    if (activeTeamId) {
      next.team_id = activeTeamId;
    }

    return next;
  }, [activeProjectId, activeTeamId]);

  const healthQuery = useQuery<HealthStatus>({
    queryKey: ['health', 'readyz'],
    queryFn: fetchHealth,
    refetchInterval: 30000,
  });

  const overviewQuery = useMetricsOverview(activeOrgId, scope);
  const ingestQuery = useMemoryIngest(activeOrgId, scope);
  const sessionsQuery = useSessions(activeOrgId, scope);
  const activityQuery = useActivity(activeOrgId, scope);
  const opsQuery = useOpsOverview(activeOrgId);

  const health = healthQuery.data;
  const healthOk = health?.ok ?? false;
  const healthLabel = healthQuery.isLoading ? 'Checking' : healthOk ? 'Operational' : 'Degraded';
  const healthColor = healthQuery.isLoading ? '#666C77' : healthOk ? '#3DD9AC' : '#FB6E72';

  const [connectOpen, setConnectOpen] = React.useState(false);
  const overview = overviewQuery.data;
  const sessions = sessionsQuery.data ?? [];

  const latencyMeasured =
    overview?.avg_retrieval_latency_measured && overview.avg_retrieval_latency_ms !== null;

  const stats: StatItem[] = [
    {
      label: 'Memories indexed',
      value: formatCount(overview?.memories_indexed),
      delta: formatDelta(overview?.memories_indexed_delta),
      tone: (overview?.memories_indexed_delta ?? 0) > 0 ? 'success' : 'neutral',
    },
    {
      label: 'Context bundles · 7d',
      value: formatCount(overview?.context_bundles_7d),
      delta: formatDelta(overview?.context_bundles_7d_delta),
      tone: (overview?.context_bundles_7d_delta ?? 0) > 0 ? 'success' : 'neutral',
    },
    {
      label: 'Avg retrieval',
      value: latencyMeasured ? `${Math.round(overview!.avg_retrieval_latency_ms!)}ms` : '—',
      delta: latencyMeasured ? null : 'Not measured',
      tone: 'neutral',
    },
    {
      label: 'Connected agents · 24h',
      value: formatCount(overview?.connected_agents),
      delta: null,
      tone: 'neutral',
    },
  ];

  return (
    <div className='space-y-6'>
      <PageHeader
        title='Overview'
        subtitle='Memory health across your organization'
        actions={
          <>
            <span className='hidden h-10 items-center gap-2 rounded-[11px] border border-divider bg-content1 px-3.5 text-[12px] font-medium text-default-500 sm:inline-flex'>
              <PulseDot color={healthColor} pulse={healthOk} size={7} />
              {healthLabel}
            </span>
            <PrimaryButton
              type='button'
              onPress={() => setConnectOpen(true)}
              startContent={<Plus size={16} strokeWidth={2.2} />}
            >
              Connect agent
            </PrimaryButton>
          </>
        }
      />

      <ConnectAgentModal
        isOpen={connectOpen}
        onClose={() => setConnectOpen(false)}
      />

      <div className='space-y-[14px]'>
        <div className='grid grid-cols-2 gap-[14px] lg:grid-cols-4'>
          {stats.map((item) => (
            <StatCard key={item.label} item={item} />
          ))}
        </div>

        <div>
          <h2 className='mb-2.5 text-[12px] font-semibold uppercase tracking-[0.1em] text-default-400'>
            Pipeline health
          </h2>
          <OpsStrip
            data={opsQuery.data}
            isLoading={opsQuery.isLoading}
            isError={opsQuery.isError}
            onRetry={() => opsQuery.refetch()}
          />
        </div>

        <div className='grid gap-[14px] lg:grid-cols-[1.55fr_1fr]'>
          <MemoryIngest
            points={ingestQuery.data ?? []}
            loading={ingestQuery.isLoading}
            isError={ingestQuery.isError}
            onRetry={() => ingestQuery.refetch()}
          />
          <AgentSessions
            sessions={sessions}
            loading={sessionsQuery.isLoading}
            isError={sessionsQuery.isError}
            onRetry={() => sessionsQuery.refetch()}
          />
        </div>

        <div className='grid gap-[14px] lg:grid-cols-[1.55fr_1fr]'>
          <RecentActivity
            events={activityQuery.data ?? []}
            loading={activityQuery.isLoading}
            isError={activityQuery.isError}
            onRetry={() => activityQuery.refetch()}
          />
          <WeeklyDigestCard />
        </div>
      </div>
    </div>
  );
}
