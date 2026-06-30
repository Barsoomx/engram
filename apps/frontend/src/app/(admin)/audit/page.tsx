'use client';

import { useQuery } from '@tanstack/react-query';
import * as React from 'react';

import { apiClient } from '@/lib/auth';
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

function ResultBadge({ result }: { result: string }) {
  const tone =
    result === 'success' ? 'text-success-500' : result === 'denied' ? 'text-danger-500' : 'text-default-500';

  return <strong className={tone}>{result}</strong>;
}

function AuditTable({ items }: { items: AuditEventItem[] }) {
  return (
    <div className='overflow-x-auto'>
      <table className='w-full border-collapse text-left text-sm'>
        <thead>
          <tr className='border-b border-divider'>
            <th className='py-2 px-3 text-default-500 font-medium'>Event type</th>
            <th className='py-2 px-3 text-default-500 font-medium'>Actor</th>
            <th className='py-2 px-3 text-default-500 font-medium'>Capability</th>
            <th className='py-2 px-3 text-default-500 font-medium'>Result</th>
            <th className='py-2 px-3 text-default-500 font-medium'>Created at</th>
          </tr>
        </thead>
        <tbody>
          {items.map((event) => (
            <tr key={event.id} className='border-b border-divider/50'>
              <td className='py-2 px-3 font-mono text-xs text-default-700'>{event.event_type}</td>
              <td className='py-2 px-3 text-default-700'>
                <span className='font-mono text-xs'>{event.actor_type}</span>
                {event.actor_id ? <span className='text-default-500'> · {event.actor_id}</span> : null}
              </td>
              <td className='py-2 px-3 text-default-700'>{event.capability ?? '—'}</td>
              <td className='py-2 px-3'>
                <ResultBadge result={event.result} />
              </td>
              <td className='py-2 px-3 text-default-700 whitespace-nowrap'>{event.created_at ?? '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
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

      const response = await client.get<AuditEventsResponse>('/v1/inspection/audit-events/', { params });

      return response.data;
    },
  });

  if (!activeProjectId) {
    return (
      <section>
        <h1 className='text-2xl font-semibold text-foreground'>Audit</h1>
        <p className='mt-4 text-sm text-default-500'>
          Select a project to view audit events.
        </p>
      </section>
    );
  }

  return (
    <section className='space-y-4'>
      <div>
        <h1 className='text-2xl font-semibold text-foreground'>Audit</h1>
        <p className='text-xs text-default-500 mt-1 font-mono'>
          {process.env.NEXT_PUBLIC_ENGRAM_API_URL ?? 'http://localhost:8000'}
          /v1/inspection/audit-events/
        </p>
      </div>

      {query.isLoading && <p className='text-default-500'>Loading audit events...</p>}

      {query.isError && (
        <pre className='text-sm text-danger-500 bg-danger-50 dark:bg-danger-500/10 rounded-medium p-3'>
          {query.error instanceof Error ? query.error.message : 'Failed to load audit events.'}
        </pre>
      )}

      {query.data && (
        <>
          <p className='text-sm text-default-500'>Total: {query.data.count}</p>
          {query.data.items.length > 0 ? (
            <div className='surface-card p-2'>
              <AuditTable items={query.data.items} />
            </div>
          ) : (
            <p className='text-default-500'>No audit events found for this project.</p>
          )}
        </>
      )}
    </section>
  );
}
