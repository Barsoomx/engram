'use client';

import { useQuery } from '@tanstack/react-query';
import { ArrowRight, Plus, Shield, Sparkles } from 'lucide-react';
import Link from 'next/link';
import * as React from 'react';

import { PageHeader } from '@/components/ui/page-header';
import { PrimaryButton } from '@/components/ui/primary-button';
import { PulseDot } from '@/components/ui/pulse-dot';
import { Sparkline } from '@/components/ui/sparkline';
import { apiClient, fetchMe, type MeResponse } from '@/lib/auth';

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

interface StatItem {
  label: string;
  value: string;
  delta: string;
  tone: 'success' | 'neutral';
  color: string;
  data: number[];
}

const STATS: StatItem[] = [
  {
    label: 'Memories indexed',
    value: '2,481',
    delta: '+12.4%',
    tone: 'success',
    color: '#7C5CFF',
    data: [12, 18, 15, 22, 19, 27, 24, 31, 28, 36, 33, 41, 44, 48],
  },
  {
    label: 'Context bundles · 7d',
    value: '18.2k',
    delta: '+8.1%',
    tone: 'success',
    color: '#6BA6FF',
    data: [8, 10, 9, 13, 12, 16, 15, 14, 18, 17, 21, 20, 24, 26],
  },
  {
    label: 'Avg retrieval',
    value: '142ms',
    delta: '−11ms',
    tone: 'success',
    color: '#3DD9AC',
    data: [170, 168, 161, 158, 150, 152, 148, 145, 147, 142, 140, 143, 139, 142],
  },
  {
    label: 'Connected agents',
    value: '3',
    delta: 'all live',
    tone: 'neutral',
    color: '#666C77',
    data: [3, 3, 3, 2, 3, 3, 3, 3, 2, 3, 3, 3, 3, 3],
  },
];

const INGEST_BARS = [38, 52, 46, 61, 49, 67, 58, 72, 55, 80, 69, 88, 76, 94];
const INGEST_MAX = Math.max(...INGEST_BARS);
const INGEST_LABELS = ['Jun 17', 'Jun 21', 'Jun 25', 'Jun 30'];

interface AgentItem {
  name: string;
  model: string;
  color: string;
  status: string;
  last: string;
  live: boolean;
}

const AGENTS: AgentItem[] = [
  { name: 'Claude Code', model: 'claude-sonnet-4', color: '#7C5CFF', status: 'Active', last: 'now', live: true },
  { name: 'Codex', model: 'gpt-5-codex', color: '#3DD9AC', status: 'Active', last: '2m', live: true },
  { name: 'Cursor', model: 'cursor-fast', color: '#6BA6FF', status: 'Idle', last: '1h', live: false },
];

interface ActivityItem {
  tone: string;
  text: string;
  meta: string;
  time: string;
  actor?: string;
}

function StatCard({ item }: { item: StatItem }) {
  const deltaClass = item.tone === 'success' ? 'text-success' : 'text-default-400';

  return (
    <div className='surface-card flex flex-col gap-3 p-[18px]'>
      <div className='flex items-start justify-between gap-2'>
        <span className='text-[11.5px] leading-tight text-default-500'>{item.label}</span>
        <span className={`shrink-0 text-[11px] font-medium ${deltaClass}`}>{item.delta}</span>
      </div>
      <span className='tnum text-[27px] font-semibold leading-none tracking-[-0.02em] text-foreground'>
        {item.value}
      </span>
      <Sparkline data={item.data} color={item.color} height={26} />
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

function MemoryIngest() {
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
      <div className='flex h-[150px] items-end gap-[7px]'>
        {INGEST_BARS.map((value, index) => (
          <div
            key={index}
            className='animate-bar-grow flex-1 transition-[filter] duration-150 hover:brightness-110'
            style={{
              height: `${(value / INGEST_MAX) * 100}%`,
              borderRadius: '5px 5px 2px 2px',
              backgroundImage: 'linear-gradient(180deg,#8B6BFF,#5A3DF2)',
              animationDelay: `${index * 38}ms`,
            }}
          />
        ))}
      </div>
      <div className='mt-3 flex justify-between font-mono text-[11px] text-default-400'>
        {INGEST_LABELS.map((label) => (
          <span key={label}>{label}</span>
        ))}
      </div>
    </div>
  );
}

function ConnectedAgents() {
  return (
    <div className='rounded-[18px] border border-divider bg-content1 p-5'>
      <PanelHeading title='Connected agents' sub='Live agent sessions' />
      <div className='space-y-2.5'>
        {AGENTS.map((agent) => (
          <div
            key={agent.name}
            className='flex items-center gap-3 rounded-[12px] border border-divider bg-content2 px-3.5 py-3'
          >
            <span
              className='flex h-8 w-8 shrink-0 items-center justify-center rounded-[9px]'
              style={{ backgroundColor: `${agent.color}1f`, color: agent.color }}
            >
              <Shield size={16} strokeWidth={2} />
            </span>
            <div className='min-w-0 flex-1'>
              <div className='truncate text-[13px] font-semibold text-foreground'>{agent.name}</div>
              <div className='truncate font-mono text-[11.5px] text-default-400'>{agent.model}</div>
            </div>
            <div className='flex shrink-0 items-center gap-1.5 text-[11.5px]'>
              <PulseDot color={agent.live ? '#3DD9AC' : '#666C77'} pulse={agent.live} size={6} />
              <span className={agent.live ? 'text-default-700' : 'text-default-500'}>{agent.status}</span>
              <span className='text-default-400'>· {agent.last}</span>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function RecentActivity({ items }: { items: ActivityItem[] }) {
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
      <div className='space-y-0.5'>
        {items.map((item, index) => (
          <div
            key={index}
            className='flex items-center gap-3 rounded-[10px] px-3 py-2.5 transition-colors hover:bg-content2'
          >
            <span className='h-2 w-2 shrink-0 rounded-full' style={{ backgroundColor: item.tone }} />
            <span className='truncate text-[13px] text-default-700'>{item.text}</span>
            <span className='shrink-0 rounded-[6px] bg-content2 px-2 py-0.5 font-mono text-[11px] text-default-400'>
              {item.meta}
            </span>
            <span className='ml-auto shrink-0 text-[11.5px] text-default-400'>
              {item.actor && <span className='mr-2 text-default-500'>by {item.actor}</span>}
              {item.time}
            </span>
          </div>
        ))}
      </div>
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
          <div className='text-[14.5px] font-semibold text-foreground'>Weekly digest is ready</div>
          <div className='mt-0.5 text-[12px] text-primary-300'>Updated just now</div>
        </div>
      </div>
      <p className='mt-3.5 text-[13px] leading-relaxed text-default-500'>
        34 memories merged and 6 retired across your organization this week. Review what changed before
        your agents inject them.
      </p>
      <button
        type='button'
        className='mt-4 inline-flex items-center gap-1.5 rounded-[10px] border border-primary/30 bg-primary/10 px-3.5 py-2 text-[12.5px] font-semibold text-primary-300 transition-colors hover:bg-primary/20'
      >
        Review digest
        <ArrowRight size={14} strokeWidth={2.2} />
      </button>
    </div>
  );
}

export default function DashboardPage() {
  const meQuery = useQuery<MeResponse>({
    queryKey: ['auth', 'me'],
    queryFn: fetchMe,
  });
  const healthQuery = useQuery<HealthStatus>({
    queryKey: ['health', 'livez'],
    queryFn: fetchHealth,
    refetchInterval: 30000,
  });

  const profile = meQuery.data;
  const health = healthQuery.data;
  const healthOk = health?.ok ?? false;
  const healthLabel = healthQuery.isLoading ? 'Checking' : healthOk ? 'Operational' : 'Degraded';
  const healthColor = healthQuery.isLoading ? '#666C77' : healthOk ? '#3DD9AC' : '#FB6E72';

  const activity: ActivityItem[] = [
    {
      tone: '#3DD9AC',
      text: 'Memory promoted to authoritative',
      meta: 'auth/login.py',
      time: '4m ago',
      actor: profile?.username,
    },
    { tone: '#A78BFF', text: 'New convention captured', meta: 'api/serializers.py', time: '22m ago' },
    { tone: '#6BA6FF', text: 'Context bundle assembled', meta: 'bundle · 14 memories', time: '1h ago' },
    { tone: '#F2B765', text: 'Memory flagged stale', meta: 'db/migrations', time: '3h ago' },
    { tone: '#FB6E72', text: 'Memory retired', meta: 'legacy/auth.py', time: '5h ago' },
  ];

  return (
    <div className='space-y-6'>
      <PageHeader
        title='Overview'
        subtitle='Memory health across your organization · updated just now'
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
          {STATS.map((item) => (
            <StatCard key={item.label} item={item} />
          ))}
        </div>

        <div className='grid gap-[14px] lg:grid-cols-[1.55fr_1fr]'>
          <MemoryIngest />
          <ConnectedAgents />
        </div>

        <div className='grid gap-[14px] lg:grid-cols-[1.55fr_1fr]'>
          <RecentActivity items={activity} />
          <WeeklyDigest />
        </div>
      </div>
    </div>
  );
}
