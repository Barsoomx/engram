'use client';

import { useQuery } from '@tanstack/react-query';
import { AlertTriangle, ScrollText } from 'lucide-react';
import * as React from 'react';

import { EmptyState } from '@/components/ui/empty-state';
import { PageHeader } from '@/components/ui/page-header';
import { apiClient } from '@/lib/auth';
import { formatRelativeTime } from '@/lib/design';
import { useProjectStore } from '@/lib/project-store';
import { useTeamStore } from '@/lib/team-store';

type AuditEventItem = {
  id: string;
  project_id: string;
  team_id: string | null;
  event_type: string;
  actor_type: string;
  actor_id: string | null;
  target_type: string | null;
  target_id: string | null;
  capability: string | null;
  result: string;
  request_id: string | null;
  created_at: string | null;
};

type AuditEventsResponse = {
  count: number;
  items: AuditEventItem[];
};

type AuditAction =
  | 'create'
  | 'delete'
  | 'change'
  | 'promote'
  | 'login'
  | 'secret'
  | 'neutral';

const ACTION_STYLES: Record<AuditAction, string> = {
  create: 'text-success bg-success/10',
  delete: 'text-danger bg-danger/10',
  change: 'text-info bg-info/10',
  promote: 'text-primary-300 bg-primary-soft',
  login: 'text-default-500 bg-content3',
  secret: 'text-warning bg-warning/10',
  neutral: 'text-default-500 bg-content3',
};

const RESULT_COLORS: Record<string, string> = {
  success: '#3DD9AC',
  denied: '#FB6E72',
};

const GRID =
  'grid grid-cols-[minmax(150px,0.9fr)_minmax(0,1.4fr)_minmax(0,1fr)_auto] items-center gap-4';

function resolveAction(eventType: string): AuditAction {
  const v = eventType.toLowerCase();

  if (v.includes('secret')) {
    return 'secret';
  }

  if (v.includes('delete') || v.includes('archive') || v.includes('revoke')) {
    return 'delete';
  }

  if (v.includes('create') || v.includes('issue')) {
    return 'create';
  }

  if (v.includes('promote')) {
    return 'promote';
  }

  if (v.includes('login')) {
    return 'login';
  }

  if (v.includes('change') || v.includes('update')) {
    return 'change';
  }

  return 'neutral';
}

function shortenId(value: string): string {
  if (value.length <= 12) {
    return value;
  }

  return `${value.slice(0, 8)}…`;
}

function AuditRow({ event }: { event: AuditEventItem }) {
  const action = resolveAction(event.event_type);
  const resultColor = RESULT_COLORS[event.result] ?? '#666C77';

  return (
    <div
      className={`${GRID} border-b border-divider px-5 py-3.5 transition-colors last:border-b-0 hover:bg-content2/60`}
    >
      <div className='min-w-0'>
        <span
          className={`inline-flex max-w-full items-center truncate rounded-[7px] px-2 py-0.5 font-mono text-[11px] font-medium ${ACTION_STYLES[action]}`}
        >
          {event.event_type}
        </span>
      </div>

      <div className='min-w-0'>
        {event.target_type || event.target_id ? (
          <div className='truncate text-[13px] text-default-700'>
            {event.target_type ?? 'target'}
            {event.target_id && (
              <span className='ml-1.5 font-mono text-[11.5px] text-default-400'>
                {shortenId(event.target_id)}
              </span>
            )}
          </div>
        ) : (
          <span className='text-[13px] text-default-400'>—</span>
        )}
        {event.capability && (
          <div className='truncate font-mono text-[11px] text-default-400'>
            {event.capability}
          </div>
        )}
      </div>

      <div className='min-w-0 truncate text-[13px]'>
        <span className='text-default-400'>by </span>
        <span className='text-default-500'>{event.actor_type}</span>
        {event.actor_id && (
          <span className='ml-1 font-mono text-[11.5px] text-default-400'>
            {shortenId(event.actor_id)}
          </span>
        )}
      </div>

      <div className='flex items-center justify-end gap-2.5'>
        <span className='inline-flex items-center gap-1.5'>
          <span
            className='inline-block h-1.5 w-1.5 rounded-full'
            style={{ backgroundColor: resultColor }}
          />
          <span
            className='whitespace-nowrap text-[11.5px] font-medium capitalize'
            style={{ color: resultColor }}
          >
            {event.result}
          </span>
        </span>
        <span className='tnum whitespace-nowrap font-mono text-[11.5px] text-default-400'>
          {formatRelativeTime(event.created_at)}
        </span>
      </div>
    </div>
  );
}

function AuditTable({ items }: { items: AuditEventItem[] }) {
  return (
    <div className='surface-card overflow-hidden'>
      <div
        className={`${GRID} border-b border-divider px-5 py-3 text-[10.5px] font-semibold uppercase tracking-[0.1em] text-default-400`}
      >
        <span>Event</span>
        <span>Target</span>
        <span>Actor</span>
        <span className='text-right'>When</span>
      </div>
      {items.map((event) => (
        <AuditRow key={event.id} event={event} />
      ))}
    </div>
  );
}

function AuditSkeleton() {
  return (
    <div className='surface-card overflow-hidden'>
      <div
        className={`${GRID} border-b border-divider px-5 py-3 text-[10.5px] font-semibold uppercase tracking-[0.1em] text-default-400`}
      >
        <span>Event</span>
        <span>Target</span>
        <span>Actor</span>
        <span className='text-right'>When</span>
      </div>
      {Array.from({ length: 6 }).map((_, index) => (
        <div
          key={index}
          className={`${GRID} border-b border-divider px-5 py-3.5 last:border-b-0`}
        >
          <span className='h-5 w-24 rounded-[7px] bg-content2 animate-pulse' />
          <span className='h-3.5 w-40 rounded-medium bg-content2 animate-pulse' />
          <span className='h-3.5 w-28 rounded-medium bg-content2 animate-pulse' />
          <span className='ml-auto h-3.5 w-16 rounded-medium bg-content2 animate-pulse' />
        </div>
      ))}
    </div>
  );
}

export default function AuditPage() {
  const activeProjectId = useProjectStore((s) => s.activeProjectId);
  const activeTeamId = useTeamStore((s) => s.activeTeamId);

  const query = useQuery<AuditEventsResponse>({
    queryKey: ['inspection', 'audit-events', activeProjectId, activeTeamId],
    enabled: Boolean(activeProjectId),
    queryFn: async () => {
      const client = apiClient();
      const params: Record<string, string> = { project_id: activeProjectId ?? '' };

      if (activeTeamId) {
        params.team_id = activeTeamId;
      }

      const response = await client.get<AuditEventsResponse>('/v1/inspection/audit-events', { params });

      return response.data;
    },
  });

  if (!activeProjectId) {
    return (
      <section className='space-y-6'>
        <PageHeader
          title='Audit log'
          subtitle='Every privileged action across this project, with actor and outcome.'
        />
        <EmptyState
          title='No project selected'
          description='Select a project to view its audit events.'
          icon={<ScrollText className='h-6 w-6' />}
        />
      </section>
    );
  }

  return (
    <section className='space-y-6'>
      <PageHeader
        title='Audit log'
        subtitle='Every privileged action across this project, with actor and outcome.'
      />

      {query.isLoading && <AuditSkeleton />}

      {query.isError && (
        <div className='flex items-start gap-3 rounded-[16px] border border-danger/30 bg-danger/5 px-5 py-4'>
          <AlertTriangle className='mt-0.5 h-5 w-5 shrink-0 text-danger' />
          <p className='text-[13px] leading-relaxed text-danger'>
            {query.error instanceof Error ? query.error.message : 'Failed to load audit events.'}
          </p>
        </div>
      )}

      {query.data &&
        (query.data.items.length > 0 ? (
          <div className='space-y-3'>
            <AuditTable items={query.data.items} />
            <p className='tnum text-[12px] text-default-400'>
              Total {query.data.count} {query.data.count === 1 ? 'event' : 'events'}
            </p>
          </div>
        ) : (
          <EmptyState
            title='No audit events yet'
            description='Privileged actions in this project will appear here as they happen.'
            icon={<ScrollText className='h-6 w-6' />}
          />
        ))}
    </section>
  );
}
