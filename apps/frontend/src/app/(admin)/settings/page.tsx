'use client';

import { useQuery } from '@tanstack/react-query';
import { useRouter } from 'next/navigation';
import * as React from 'react';

import { apiClient, clearToken, fetchMe, logout, type MeResponse } from '@/lib/auth';

const API_URL = process.env.NEXT_PUBLIC_ENGRAM_API_URL ?? 'http://localhost:8000';
const PROJECT_ID = process.env.NEXT_PUBLIC_ENGRAM_PROJECT_ID ?? '';
const TEAM_ID = process.env.NEXT_PUBLIC_ENGRAM_TEAM_ID ?? '';

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

function DetailRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className='grid grid-cols-1 md:grid-cols-[200px_1fr] gap-1 md:gap-4 py-2 border-b border-divider/40 last:border-b-0'>
      <dt className='text-xs uppercase tracking-wide text-default-500 font-medium'>{label}</dt>
      <dd className='text-sm text-foreground break-all'>{children}</dd>
    </div>
  );
}

export default function SettingsPage() {
  const router = useRouter();

  const meQuery = useQuery<MeResponse>({
    queryKey: ['auth', 'me'],
    queryFn: fetchMe,
    retry: false,
  });

  const healthQuery = useQuery<HealthStatus>({
    queryKey: ['health', 'healthz'],
    queryFn: fetchHealth,
    refetchInterval: 30000,
  });

  const [loggingOut, setLoggingOut] = React.useState(false);

  const handleLogout = React.useCallback(async () => {
    setLoggingOut(true);

    try {
      await logout();
    } finally {
      clearToken();
      setLoggingOut(false);
      router.replace('/login');
    }
  }, [router]);

  const profile = meQuery.data;
  const health = healthQuery.data;

  return (
    <section className='space-y-4'>
      <div>
        <h1 className='text-2xl font-semibold text-foreground'>Settings</h1>
        <p className='text-xs text-default-500 mt-1 font-mono'>
          {API_URL}
        </p>
      </div>

      <div className='surface-card p-5'>
        <h2 className='text-base font-semibold text-foreground mb-3'>Current user</h2>
        {meQuery.isLoading && <p className='text-default-500'>Loading user...</p>}
        {meQuery.isError && (
          <pre className='text-sm text-danger-500 bg-danger-50 dark:bg-danger-500/10 rounded-medium p-3'>
            {meQuery.error instanceof Error ? meQuery.error.message : 'Failed to load user.'}
          </pre>
        )}
        {profile && (
          <dl className='flex flex-col'>
            <DetailRow label='User ID'>{profile.user_id}</DetailRow>
            <DetailRow label='Username'>{profile.username}</DetailRow>
            <DetailRow label='Identity'>
              <span className='font-mono text-xs'>{profile.identity_id}</span>
            </DetailRow>
            <DetailRow label='Organization'>
              <span className='font-mono text-xs'>{profile.organization_id}</span>
            </DetailRow>
            <DetailRow label='Capabilities'>
              {profile.capabilities.length > 0 ? (
                <span className='font-mono text-xs'>{profile.capabilities.join(', ')}</span>
              ) : (
                '—'
              )}
            </DetailRow>
          </dl>
        )}
      </div>

      <div className='surface-card p-5'>
        <h2 className='text-base font-semibold text-foreground mb-3'>Backend health</h2>
        <p className='text-sm'>
          Status:{' '}
          <strong className={health?.ok ? 'text-success-500' : 'text-danger-500'}>
            {healthQuery.isLoading ? 'checking...' : health?.ok ? 'healthy' : 'unhealthy'}
          </strong>
        </p>
        <pre className='mt-3 text-xs font-mono text-default-700 bg-content2/50 rounded-medium p-3 overflow-auto'>
          {healthQuery.isLoading ? 'loading...' : health?.detail || '(empty)'}
        </pre>
      </div>

      <div className='surface-card p-5'>
        <h2 className='text-base font-semibold text-foreground mb-3'>Environment</h2>
        <dl className='flex flex-col'>
          <DetailRow label='API URL'>
            <span className='font-mono text-xs'>{API_URL}</span>
          </DetailRow>
          <DetailRow label='Project ID'>
            <span className='font-mono text-xs'>{PROJECT_ID || '(not set)'}</span>
          </DetailRow>
          <DetailRow label='Team ID'>
            <span className='font-mono text-xs'>{TEAM_ID || '(not set)'}</span>
          </DetailRow>
        </dl>
      </div>

      <div className='surface-card p-5'>
        <h2 className='text-base font-semibold text-foreground mb-3'>Session</h2>
        <button
          className='inline-flex items-center justify-center rounded-medium bg-danger-500 px-4 py-2 text-sm font-medium text-white hover:bg-danger-600 disabled:opacity-60'
          disabled={loggingOut}
          onClick={handleLogout}
          type='button'
        >
          {loggingOut ? 'Signing out...' : 'Sign out'}
        </button>
      </div>
    </section>
  );
}
