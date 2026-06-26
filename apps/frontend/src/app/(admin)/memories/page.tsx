'use client';

import { useQuery } from '@tanstack/react-query';
import * as React from 'react';

import { apiClient } from '@/lib/auth';

type MemoryItem = {
  id: string;
  project_id: string;
  team_id: string | null;
  title: string;
  body: string;
  status: string;
  visibility_scope: string;
  current_version: number;
  confidence: string | null;
  stale: boolean;
  refuted: boolean;
  created_at: string | null;
  updated_at: string | null;
};

type MemoriesResponse = {
  count: number;
  items: MemoryItem[];
};

const PROJECT_ID = process.env.NEXT_PUBLIC_ENGRAM_PROJECT_ID ?? '';
const TEAM_ID = process.env.NEXT_PUBLIC_ENGRAM_TEAM_ID ?? '';

async function fetchMemories(): Promise<MemoriesResponse> {
  const client = apiClient();
  const params: Record<string, string> = { project_id: PROJECT_ID };

  if (TEAM_ID) {
    params.team_id = TEAM_ID;
  }

  const response = await client.get<MemoriesResponse>('/v1/inspection/memories/', { params });

  return response.data;
}

function StatusBadge({ status }: { status: string }) {
  const tone =
    status === 'active' ? 'text-success-500' : status === 'stale' ? 'text-warning-500' : 'text-default-500';

  return <strong className={tone}>{status}</strong>;
}

function MemoriesTable({ items }: { items: MemoryItem[] }) {
  return (
    <div className='overflow-x-auto'>
      <table className='w-full border-collapse text-left text-sm'>
        <thead>
          <tr className='border-b border-divider'>
            <th className='py-2 px-3 text-default-500 font-medium'>ID</th>
            <th className='py-2 px-3 text-default-500 font-medium'>Title</th>
            <th className='py-2 px-3 text-default-500 font-medium'>Status</th>
            <th className='py-2 px-3 text-default-500 font-medium'>Visibility</th>
          </tr>
        </thead>
        <tbody>
          {items.map((memory) => (
            <tr key={memory.id} className='border-b border-divider/50'>
              <td className='py-2 px-3 font-mono text-xs text-default-700 break-all'>
                {memory.id}
              </td>
              <td className='py-2 px-3 text-foreground'>{memory.title || '(untitled)'}</td>
              <td className='py-2 px-3'>
                <StatusBadge status={memory.status} />
              </td>
              <td className='py-2 px-3 text-default-700'>{memory.visibility_scope}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function MemoriesPage() {
  const query = useQuery<MemoriesResponse>({
    queryKey: ['inspection', 'memories', PROJECT_ID, TEAM_ID],
    enabled: Boolean(PROJECT_ID),
    queryFn: fetchMemories,
  });

  if (!PROJECT_ID) {
    return (
      <section>
        <h1 className='text-2xl font-semibold text-foreground'>Memories</h1>
        <pre className='mt-4 text-sm text-default-500 bg-content2/50 rounded-medium p-3'>
          NEXT_PUBLIC_ENGRAM_PROJECT_ID is not set.
        </pre>
      </section>
    );
  }

  return (
    <section className='space-y-4'>
      <div>
        <h1 className='text-2xl font-semibold text-foreground'>Memories</h1>
        <p className='text-xs text-default-500 mt-1 font-mono'>
          {process.env.NEXT_PUBLIC_ENGRAM_API_URL ?? 'http://localhost:8000'}
          /v1/inspection/memories/
        </p>
      </div>

      {query.isLoading && <p className='text-default-500'>Loading memories...</p>}

      {query.isError && (
        <pre className='text-sm text-danger-500 bg-danger-50 dark:bg-danger-500/10 rounded-medium p-3'>
          {query.error instanceof Error ? query.error.message : 'Failed to load memories.'}
        </pre>
      )}

      {query.data && (
        <>
          <p className='text-sm text-default-500'>Total: {query.data.count}</p>
          {query.data.items.length > 0 ? (
            <div className='surface-card p-2'>
              <MemoriesTable items={query.data.items} />
            </div>
          ) : (
            <p className='text-default-500'>No memories found for this project.</p>
          )}
        </>
      )}
    </section>
  );
}
