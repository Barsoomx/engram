'use client';

import { useQuery } from '@tanstack/react-query';
import { Building2 } from 'lucide-react';
import * as React from 'react';

import { fetchMe, type MeResponse } from '@/lib/auth';

export default function ProjectsPage() {
  const meQuery = useQuery<MeResponse>({
    queryKey: ['auth', 'me'],
    queryFn: fetchMe,
  });

  const profile = meQuery.data;

  return (
    <section className='space-y-6'>
      <div>
        <h1 className='text-2xl font-semibold text-foreground'>Projects &amp; Teams</h1>
        <p className='text-sm text-default-500 mt-1'>
          Read-only view of your current project scope and what operations your
          account can perform. Management is done via the Django admin or the{' '}
          <code className='font-mono text-xs'>engram connect</code> CLI.
        </p>
      </div>

      <div className='surface-card p-5'>
        <div className='flex items-start gap-3 mb-4'>
          <div className='w-10 h-10 rounded-lg bg-content2 flex items-center justify-center shrink-0'>
            <Building2 className='w-5 h-5 text-foreground' />
          </div>
          <div className='min-w-0'>
            <h2 className='text-base font-semibold text-foreground'>Access scope</h2>
            <p className='text-sm text-default-500 mt-1'>
              Derived from the <code className='font-mono text-xs'>/v1/auth/me</code> profile.
            </p>
          </div>
        </div>

        {meQuery.isLoading && <p className='text-default-500'>Loading profile...</p>}

        {meQuery.isError && (
          <pre className='text-sm text-danger-500 bg-danger-50 dark:bg-danger-500/10 rounded-medium p-3'>
            {meQuery.error instanceof Error ? meQuery.error.message : 'Failed to load profile.'}
          </pre>
        )}

        {profile && (
          <dl className='grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-2 text-sm'>
            <div>
              <dt className='text-default-500'>Organization ID</dt>
              <dd className='font-mono text-foreground break-all'>{profile.organization_id}</dd>
            </div>
            <div>
              <dt className='text-default-500'>Identity ID</dt>
              <dd className='font-mono text-foreground break-all'>{profile.identity_id}</dd>
            </div>
            <div>
              <dt className='text-default-500'>Signed in user</dt>
              <dd className='font-mono text-foreground break-all'>{profile.username}</dd>
            </div>
            <div>
              <dt className='text-default-500'>User ID</dt>
              <dd className='font-mono text-foreground break-all'>{String(profile.user_id)}</dd>
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
        )}
      </div>
    </section>
  );
}
