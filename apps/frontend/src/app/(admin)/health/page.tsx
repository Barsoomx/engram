'use client';

import { useQuery } from '@tanstack/react-query';
import * as React from 'react';

import { PageHeader } from '@/components/ui/page-header';
import { PulseDot } from '@/components/ui/pulse-dot';
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

  const statusColor = query.isLoading
    ? '#666C77'
    : health?.ok
      ? '#3DD9AC'
      : '#FB6E72';

  return (
    <section className='space-y-6'>
      <PageHeader
        title='Backend Health'
        subtitle='Liveness probe for the Engram API, refreshed every 30 seconds.'
      />

      <div className='surface-card space-y-4 p-5'>
        <div className='flex items-center justify-between gap-4'>
          <div className='flex items-center gap-2.5'>
            <PulseDot color={statusColor} pulse={Boolean(health?.ok)} />
            <span className='text-[13.5px] text-default-700'>Status</span>
          </div>
          <span
            className={`text-[13.5px] font-semibold ${
              health?.ok
                ? 'text-success'
                : query.isLoading
                  ? 'text-default-400'
                  : 'text-danger'
            }`}
          >
            {query.isLoading ? 'checking…' : health?.ok ? 'Healthy' : 'Unhealthy'}
          </span>
        </div>

        <p className='font-mono text-[12px] text-default-400'>
          {API_URL}/-/healthz/
        </p>

        <div className='space-y-2'>
          <p className='text-[10.5px] font-semibold uppercase tracking-[0.12em] text-default-400'>
            Response
          </p>
          <pre className='overflow-auto rounded-[10px] border border-divider bg-content2/50 p-3 font-mono text-xs text-default-700'>
            {query.isLoading ? 'loading…' : health?.detail || '(empty)'}
          </pre>
        </div>
      </div>
    </section>
  );
}
