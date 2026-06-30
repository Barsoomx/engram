'use client';

import { useQuery } from '@tanstack/react-query';
import { ArrowRight, Plus, Shield, Sparkles } from 'lucide-react';
import Link from 'next/link';
import * as React from 'react';

import { PageHeader } from '@/components/ui/page-header';
import { PrimaryButton } from '@/components/ui/primary-button';
import { PulseDot } from '@/components/ui/pulse-dot';
import {
  useActivity,
  useMemoryIngest,
  useMetricsOverview,
  useSessions,
} from '@/hooks/use-metrics';
import { apiClient } from '@/lib/auth';
import { avatarColor, formatRelativeTime } from '@/lib/design';
import type {
  ActivityEvent,
  MemoryIngestPoint,
  MetricsSession,
} from '@/lib/metrics-api';
import { useOrgStore } from '@/lib/org-store';

type HealthStatus = {
  ok: boolean;
  detail: string;
};

async function fetchHealth(): Promise<HealthStatus> {
  const client = apiClient();

  try {
    const response = await client.get('/-/healthz/', {
      headers: { Accept: 'text/plain, application/json' },
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
      const date = new Date(points[index].date);

      return Number.isNaN(date.getTime())
        ? points[index].date
        : date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
    });
}

function MemoryIngest({
  points,
  loading,
}: {
  points: MemoryIngestPoint[];
  loading: boolean;
}) {
  const max = Math.max(1, ...points.map((point) => point.count));
  const labels = ingestLabels(points);

  return (
    <div className='rounded-[18px] border border-divider bg-content1 p-5'>
      <PanelHeading
        title='Memory ingest'
        sub='Memories captured per day · last 14 days'
        right={
          <span className='flex items-center gap-1.5 text-[11px] text-default-500'>
            <span className='h-2 w-2 rounded-full bg-primary' />
            Ingested
          </span>
        }
      />
      {points.length === 0 ? (
        <div className='flex h-[150px] items-center justify-center text-[12.5px] text-default-400'>
          {loading ? 'Loading…' : 'No ingest activity yet'}
        </div>
      ) : (
        <>
          <div className='flex h-[150px] items-end gap-[7px]'>
            {points.map((point, index) => (
              <div
                key={point.date}
                title={`${point.date} · ${point.count}`}
                className='animate-bar-grow flex-1 transition-[filter] duration-150 hover:brightness-110'
                style={{
                  height: `${(point.count / max) * 100}%`,
                  borderRadius: '5px 5px 2px 2px',
                  backgroundImage: 'linear-gradient(180deg,#8B6BFF,#5A3DF2)',
                  animationDelay: `${index * 38}ms`,
                }}
              />
            ))}
          </div>
          <div className='mt-3 flex justify-between font-mono text-[11px] text-default-400'>
            {labels.map((label, index) => (
              <span key={`${label}-${index}`}>{label}</span>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

function ConnectedAgents({
  sessions,
  loading,
}: {
  sessions: MetricsSession[];
  loading: boolean;
}) {
  return (
    <div className='rounded-[18px] border border-divider bg-content1 p-5'>
      <PanelHeading title='Connected agents' sub='Live agent sessions' />
      {sessions.length === 0 ? (
        <div className='flex h-[120px] items-center justify-center text-[12.5px] text-default-400'>
          {loading ? 'Loading…' : 'No active agent sessions'}
        </div>
      ) : (
        <div className='space-y-2.5'>
          {sessions.map((session) => {
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
                  <span className='text-default-400'>· {formatRelativeTime(session.last_seen)}</span>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function RecentActivity({
  events,
  loading,
}: {
  events: ActivityEvent[];
  loading: boolean;
}) {
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
            <div
              key={`${event.created_at}-${index}`}
              className='flex items-center gap-3 rounded-[10px] px-3 py-2.5 transition-colors hover:bg-content2'
            >
              <span
                className='h-2 w-2 shrink-0 rounded-full'
                style={{ backgroundColor: event.result === 'success' ? '#3DD9AC' : '#FB6E72' }}
              />
              <span className='truncate text-[13px] text-default-700'>{humanizeEvent(event.event_type)}</span>
              <span className='shrink-0 rounded-[6px] bg-content2 px-2 py-0.5 font-mono text-[11px] text-default-400'>
                {event.target_type}
                {event.target_id ? ` · ${shortId(event.target_id)}` : ''}
              </span>
              <span className='ml-auto shrink-0 text-[11.5px] text-default-400'>
                {formatRelativeTime(event.created_at)}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function WeeklyDigest() {
  return (
    <div className='relative overflow-hidden rounded-[18px] border border-primary/25 bg-primary-soft p-5'>
      <div className='flex items-center gap-3'>
        <span className='flex h-9 w-9 items-center justify-center rounded-[10px] bg-primary-gradient text-white shadow-primary-glow'>
          <Sparkles size={17} strokeWidth={2} />
        </span>
        <div>
          <div className='text-[14.5px] font-semibold text-foreground'>Weekly digest</div>
          <div className='mt-0.5 text-[12px] text-primary-300'>Coming soon</div>
        </div>
      </div>
      <p className='mt-3.5 text-[13px] leading-relaxed text-default-500'>
        A weekly summary of memories merged and retired across your organization will appear here once
        digest reporting is enabled on the backend.
      </p>
      <button
        type='button'
        disabled
        className='mt-4 inline-flex cursor-not-allowed items-center gap-1.5 rounded-[10px] border border-primary/20 bg-primary/5 px-3.5 py-2 text-[12.5px] font-semibold text-primary-300/60'
      >
        Review digest
        <ArrowRight size={14} strokeWidth={2.2} />
      </button>
      <span className='mt-2 block text-[11px] text-default-400'>Preview · not yet available</span>
    </div>
  );
}

export default function DashboardPage() {
  const activeOrgId = useOrgStore((state) => state.activeOrgId);

  const healthQuery = useQuery<HealthStatus>({
    queryKey: ['health', 'livez'],
    queryFn: fetchHealth,
    refetchInterval: 30000,
  });

  const overviewQuery = useMetricsOverview(activeOrgId);
  const ingestQuery = useMemoryIngest(activeOrgId);
  const sessionsQuery = useSessions(activeOrgId);
  const activityQuery = useActivity(activeOrgId);

  const health = healthQuery.data;
  const healthOk = health?.ok ?? false;
  const healthLabel = healthQuery.isLoading ? 'Checking' : healthOk ? 'Operational' : 'Degraded';
  const healthColor = healthQuery.isLoading ? '#666C77' : healthOk ? '#3DD9AC' : '#FB6E72';

  const overview = overviewQuery.data;
  const sessions = sessionsQuery.data ?? [];
  const liveCount = sessions.filter((session) => session.status === 'active').length;

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
      label: 'Connected agents',
      value: formatCount(overview?.connected_agents),
      delta: liveCount > 0 ? `${liveCount} live` : null,
      tone: liveCount > 0 ? 'success' : 'neutral',
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
            <PrimaryButton type='button' startContent={<Plus size={16} strokeWidth={2.2} />}>
              Connect agent
            </PrimaryButton>
          </>
        }
      />

      <div className='space-y-[14px]'>
        <div className='grid grid-cols-2 gap-[14px] lg:grid-cols-4'>
          {stats.map((item) => (
            <StatCard key={item.label} item={item} />
          ))}
        </div>

        <div className='grid gap-[14px] lg:grid-cols-[1.55fr_1fr]'>
          <MemoryIngest points={ingestQuery.data ?? []} loading={ingestQuery.isLoading} />
          <ConnectedAgents sessions={sessions} loading={sessionsQuery.isLoading} />
        </div>

        <div className='grid gap-[14px] lg:grid-cols-[1.55fr_1fr]'>
          <RecentActivity events={activityQuery.data ?? []} loading={activityQuery.isLoading} />
          <WeeklyDigest />
        </div>
      </div>
    </div>
  );
}
