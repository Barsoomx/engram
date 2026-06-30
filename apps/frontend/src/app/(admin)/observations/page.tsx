'use client';

import { useQuery } from '@tanstack/react-query';
import { Eye } from 'lucide-react';
import * as React from 'react';

import { EmptyState } from '@/components/ui/empty-state';
import { PageHeader } from '@/components/ui/page-header';
import { TableRowSkeleton } from '@/components/ui/table-row-skeleton';
import { apiClient } from '@/lib/auth';
import { useProjectStore } from '@/lib/project-store';
import { useTeamStore } from '@/lib/team-store';

type ObservationItem = {
  observation_id: string;
  session_id: string | null;
  observation_type: string;
  title: string;
  body: string;
  files_read: string[] | null;
  files_modified: string[] | null;
  observed_at: string | null;
};

type ObservationsResponse = {
  request_id: string;
  items: ObservationItem[];
};

function ObservationsTable({ items }: { items: ObservationItem[] }) {
  return (
    <div className='overflow-x-auto'>
      <table className='w-full border-collapse text-left text-sm'>
        <thead>
          <tr className='border-b border-divider'>
            <th className='py-2 px-3 text-default-500 font-medium'>Type</th>
            <th className='py-2 px-3 text-default-500 font-medium'>Title</th>
            <th className='py-2 px-3 text-default-500 font-medium'>Body</th>
            <th className='py-2 px-3 text-default-500 font-medium'>Observed at</th>
          </tr>
        </thead>
        <tbody>
          {items.map((observation) => (
            <tr key={observation.observation_id} className='border-b border-divider/50'>
              <td className='py-2 px-3 font-mono text-xs text-default-700'>{observation.observation_type}</td>
              <td className='py-2 px-3 text-foreground'>{observation.title || '(untitled)'}</td>
              <td className='py-2 px-3 text-default-700 max-w-md truncate'>{observation.body}</td>
              <td className='py-2 px-3 text-default-700 whitespace-nowrap'>
                {observation.observed_at ?? '—'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function ObservationsPage() {
  const activeProjectId = useProjectStore((s) => s.activeProjectId);
  const activeTeamId = useTeamStore((s) => s.activeTeamId);

  const query = useQuery<ObservationsResponse>({
    queryKey: ['observations', activeProjectId, activeTeamId],
    enabled: Boolean(activeProjectId),
    queryFn: async () => {
      const client = apiClient();
      const params: Record<string, string> = { project_id: activeProjectId ?? '' };

      if (activeTeamId) {
        params.team_id = activeTeamId;
      }

      const response = await client.get<ObservationsResponse>('/v1/observations/', { params });

      return response.data;
    },
  });

  if (!activeProjectId) {
    return (
      <section className='space-y-6'>
        <PageHeader
          title='Observations'
          subtitle='Raw agent observations captured for the active project.'
        />
        <EmptyState
          title='No project selected'
          description='Select a project to view its observations.'
          icon={<Eye className='w-6 h-6' />}
        />
      </section>
    );
  }

  const items = query.data?.items ?? [];

  return (
    <section className='space-y-6'>
      <PageHeader
        title='Observations'
        subtitle='Raw agent observations captured for the active project.'
      />

      <div className='surface-card p-2'>
        {query.isLoading ? (
          <table className='w-full border-collapse text-left text-sm'>
            <thead>
              <tr className='border-b border-divider'>
                {Array.from({ length: 4 }).map((_, index) => (
                  <th
                    key={index}
                    className='py-2 px-3 text-default-500 font-medium'
                  >
                    <span className='inline-block w-16 h-3 rounded-medium bg-content2/60' />
                  </th>
                ))}
              </tr>
            </thead>
            <TableRowSkeleton columns={4} />
          </table>
        ) : items.length === 0 ? (
          <EmptyState
            title='No observations'
            description='No observations have been recorded for this project yet.'
            icon={<Eye className='w-6 h-6' />}
          />
        ) : (
          <ObservationsTable items={items} />
        )}
      </div>

      {items.length > 0 && (
        <div className='flex items-center justify-between text-xs text-default-500'>
          <p>
            Showing {items.length} observation{items.length === 1 ? '' : 's'}.
          </p>
        </div>
      )}

      {query.isError && (
        <pre className='text-sm text-danger-500 bg-danger-50 dark:bg-danger-500/10 rounded-medium p-3'>
          {query.error instanceof Error
            ? query.error.message
            : 'Failed to load observations.'}
        </pre>
      )}
    </section>
  );
}
