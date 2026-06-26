'use client';

import { useQuery } from '@tanstack/react-query';
import { Activity, ShieldCheck, User } from 'lucide-react';
import * as React from 'react';

import { apiClient, fetchMe, type MeResponse } from '@/lib/auth';

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

function StatCard({
  icon: Icon,
  label,
  value,
  tone = 'default',
}: {
  icon: typeof Activity;
  label: string;
  value: string;
  tone?: 'default' | 'success' | 'danger';
}) {
  const toneClass =
    tone === 'success'
      ? 'text-success-500'
      : tone === 'danger'
        ? 'text-danger-500'
        : 'text-foreground';

  return (
    <div className='surface-card p-5 flex items-start gap-4'>
      <div className='w-10 h-10 rounded-lg bg-content2 flex items-center justify-center shrink-0'>
        <Icon className={toneClass + ' w-5 h-5'} />
      </div>
      <div className='min-w-0'>
        <p className='text-xs uppercase tracking-wider text-default-500'>{label}</p>
        <p className={'text-lg font-semibold mt-1 break-words ' + toneClass}>{value}</p>
      </div>
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

  const health = healthQuery.data;
  const profile = meQuery.data;

  return (
    <section className='space-y-6'>
      <div>
        <h1 className='text-2xl font-semibold text-foreground'>Dashboard</h1>
        <p className='text-sm text-default-500 mt-1'>
          Overview of the Engram backend and your access scope.
        </p>
      </div>

      <div className='grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4'>
        <StatCard
          icon={Activity}
          label='Backend status'
          tone={health ? (health.ok ? 'success' : 'danger') : 'default'}
          value={healthQuery.isLoading ? 'Checking...' : health?.ok ? 'Reachable' : 'Unreachable'}
        />
        <StatCard
          icon={User}
          label='Signed in user'
          value={profile ? profile.username : '—'}
        />
        <StatCard
          icon={ShieldCheck}
          label='Capabilities'
          value={profile ? String(profile.capabilities.length) : '—'}
        />
      </div>

      {profile && (
        <div className='surface-card p-5'>
          <h2 className='text-base font-semibold text-foreground mb-3'>Access scope</h2>
          <dl className='grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-2 text-sm'>
            <div>
              <dt className='text-default-500'>Organization ID</dt>
              <dd className='font-mono text-foreground break-all'>{profile.organization_id}</dd>
            </div>
            <div>
              <dt className='text-default-500'>Identity ID</dt>
              <dd className='font-mono text-foreground break-all'>{profile.identity_id}</dd>
            </div>
            <div className='sm:col-span-2'>
              <dt className='text-default-500'>Capabilities</dt>
              <dd>
                {profile.capabilities.length > 0 ? (
                  <ul className='flex flex-wrap gap-2 mt-2'>
                    {profile.capabilities.map((capability) => (
                      <li
                        key={capability}
                        className='text-xs px-2 py-1 rounded-medium bg-content2 text-foreground'
                      >
                        {capability}
                      </li>
                    ))}
                  </ul>
                ) : (
                  <span className='text-default-500'>No capabilities assigned.</span>
                )}
              </dd>
            </div>
          </dl>
        </div>
      )}

      <div className='surface-card p-5'>
        <h2 className='text-base font-semibold text-foreground mb-3'>Backend health detail</h2>
        <p className='text-xs text-default-500 mb-2 font-mono'>
          {API_URL}/-/healthz/
        </p>
        <pre className='text-xs font-mono text-default-700 bg-content2/50 rounded-medium p-3 overflow-auto'>
          {healthQuery.isLoading ? 'loading...' : health?.detail || '(empty)'}
        </pre>
      </div>
    </section>
  );
}
