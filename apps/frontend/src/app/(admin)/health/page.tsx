'use client';

import { useQuery } from '@tanstack/react-query';
import * as React from 'react';

import { apiClient } from '@/lib/auth';

const API_URL = process.env.NEXT_PUBLIC_ENGRAM_API_URL ?? 'http://localhost:8000';

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

export default function HealthPage() {
  const query = useQuery<HealthStatus>({
    queryKey: ['health', 'healthz'],
    queryFn: fetchHealth,
    refetchInterval: 30000,
  });

  const health = query.data;

  return (
    <section className='space-y-4'>
      <div>
        <h1 className='text-2xl font-semibold text-foreground'>Backend Health</h1>
        <p className='text-xs text-default-500 mt-1 font-mono'>
          {API_URL}/-/healthz/
        </p>
      </div>

      <div className='surface-card p-5'>
        <p className='text-sm'>
          Status:{' '}
          <strong className={health?.ok ? 'text-success-500' : 'text-danger-500'}>
            {query.isLoading ? 'checking...' : health?.ok ? 'healthy' : 'unhealthy'}
          </strong>
        </p>
        <h2 className='text-base font-semibold text-foreground mt-4 mb-2'>Response</h2>
        <pre className='text-xs font-mono text-default-700 bg-content2/50 rounded-medium p-3 overflow-auto'>
          {query.isLoading ? 'loading...' : health?.detail || '(empty)'}
        </pre>
      </div>
    </section>
  );
}
