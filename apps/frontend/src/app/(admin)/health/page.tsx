'use client';

import { useQuery } from '@tanstack/react-query';
import { RefreshCw } from 'lucide-react';
import * as React from 'react';

import { OpsStrip } from '@/components/ui/ops-strip';
import { PageHeader } from '@/components/ui/page-header';
import { StatusPill } from '@/components/ui/status-pill';
import { TimeStamp } from '@/components/ui/time-stamp';
import { useOpsOverview } from '@/hooks/use-metrics';
import { apiClient } from '@/lib/auth';
import { useOrgStore } from '@/lib/org-store';

const API_URL = process.env.NEXT_PUBLIC_ENGRAM_API_URL ?? 'http://localhost:8000';

type ReadyzResult = {
  ok: boolean;
  status: string;
  checks: Record<string, string>;
  raw: string;
};

function parseBody(raw: string): { status?: string; checks?: Record<string, string> } | null {
  try {
    const parsed = JSON.parse(raw);

    if (parsed && typeof parsed === 'object') {
      return parsed as { status?: string; checks?: Record<string, string> };
    }

    return null;
  } catch {
    return null;
  }
}

function errorResponse(error: unknown): { status: number; data: unknown } | null {
  if (error && typeof error === 'object' && 'response' in error) {
    const response = (error as { response?: { status?: number; data?: unknown } }).response;

    if (response && typeof response.status === 'number') {
      return { status: response.status, data: response.data };
    }
  }

  return null;
}

async function fetchReadyz(): Promise<ReadyzResult> {
  const client = apiClient();

  try {
    const response = await client.get('/-/readyz/', {
      headers: { Accept: 'application/json' },
      transformResponse: (data) => data,
    });

    const raw = typeof response.data === 'string' ? response.data : JSON.stringify(response.data);
    const parsed = parseBody(raw);

    return {
      ok: response.status >= 200 && response.status < 300,
      status: parsed?.status ?? 'ok',
      checks: parsed?.checks ?? {},
      raw,
    };
  } catch (error) {
    const response = errorResponse(error);

    if (response) {
      const raw =
        typeof response.data === 'string' ? response.data : JSON.stringify(response.data);
      const parsed = parseBody(raw);

      return {
        ok: false,
        status: parsed?.status ?? 'unavailable',
        checks: parsed?.checks ?? {},
        raw,
      };
    }

    const message = error instanceof Error ? error.message : String(error);

    return {
      ok: false,
      status: 'unreachable',
      checks: {},
      raw: `Unreachable: ${message}`,
    };
  }
}

export default function HealthPage() {
  const activeOrgId = useOrgStore((state) => state.activeOrgId);

  const query = useQuery<ReadyzResult>({
    queryKey: ['health', 'readyz'],
    queryFn: fetchReadyz,
    refetchInterval: 30000,
  });

  const opsQuery = useOpsOverview(activeOrgId);

  const result = query.data;
  const checks = Object.entries(result?.checks ?? {});
  const lastChecked = query.dataUpdatedAt ? new Date(query.dataUpdatedAt).toISOString() : null;

  return (
    <section className='space-y-6'>
      <PageHeader
        title='Backend health'
        subtitle='Readiness probe and pipeline counters, refreshed every 30 seconds.'
        actions={
          <button
            type='button'
            onClick={() => query.refetch()}
            className='inline-flex h-10 items-center gap-2 rounded-[11px] border border-divider bg-content1 px-3.5 text-[12px] font-medium text-default-500 transition-colors hover:text-foreground'
          >
            <RefreshCw size={14} strokeWidth={2.2} className={query.isFetching ? 'animate-spin' : ''} />
            Refresh
          </button>
        }
      />

      <div className='surface-card space-y-4 p-5'>
        <div className='flex items-center justify-between gap-4'>
          <div className='flex items-center gap-2.5'>
            <span className='text-[13.5px] font-semibold text-foreground'>Readiness</span>
            <StatusPill
              status={result?.status}
              tone={query.isLoading ? 'neutral' : result?.ok ? 'success' : 'danger'}
              label={query.isLoading ? 'Checking…' : result?.ok ? 'Ready' : (result?.status ?? 'Unavailable')}
            />
          </div>
          <span className='text-[11.5px] text-default-400'>
            {lastChecked ? (
              <>
                Last checked <TimeStamp value={lastChecked} />
              </>
            ) : (
              '—'
            )}
          </span>
        </div>

        <div className='space-y-2'>
          <p className='text-[10.5px] font-semibold uppercase tracking-[0.12em] text-default-400'>
            Component checks
          </p>
          {checks.length === 0 ? (
            <p className='text-[12.5px] text-default-400'>
              {query.isLoading ? 'Loading…' : 'No component checks reported.'}
            </p>
          ) : (
            <div className='divide-y divide-divider overflow-hidden rounded-[10px] border border-divider'>
              {checks.map(([name, value]) => (
                <div key={name} className='flex items-center justify-between gap-3 bg-content1 px-3.5 py-2.5'>
                  <span className='font-mono text-[12.5px] text-default-700'>{name}</span>
                  <StatusPill status={value} tone={value === 'ok' ? 'success' : 'danger'} />
                </div>
              ))}
            </div>
          )}
        </div>

        <p className='break-all font-mono text-[11.5px] text-default-400'>{API_URL}/-/readyz/</p>

        <div className='space-y-2'>
          <p className='text-[10.5px] font-semibold uppercase tracking-[0.12em] text-default-400'>
            Response
          </p>
          <pre className='max-w-full overflow-x-auto whitespace-pre-wrap break-words rounded-[10px] border border-divider bg-content2/50 p-3 font-mono text-xs text-default-700'>
            {query.isLoading ? 'loading…' : result?.raw || '(empty)'}
          </pre>
        </div>
      </div>

      <div className='space-y-2.5'>
        <h2 className='text-[12px] font-semibold uppercase tracking-[0.1em] text-default-400'>
          Pipeline health
        </h2>
        <OpsStrip
          data={opsQuery.data}
          isLoading={opsQuery.isLoading}
          isError={opsQuery.isError}
          onRetry={() => opsQuery.refetch()}
        />
      </div>
    </section>
  );
}
