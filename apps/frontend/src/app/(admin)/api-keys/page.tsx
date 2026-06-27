'use client';

import { useQuery } from '@tanstack/react-query';
import { ExternalLink, KeyRound } from 'lucide-react';
import * as React from 'react';

import { fetchMe, type MeResponse } from '@/lib/auth';

const DJANGO_ADMIN_URL = process.env.NEXT_PUBLIC_ENGRAM_DJANGO_ADMIN_URL ?? '';

export default function ApiKeysPage() {
  const meQuery = useQuery<MeResponse>({
    queryKey: ['auth', 'me'],
    queryFn: fetchMe,
  });

  const profile = meQuery.data;

  return (
    <section className='space-y-6'>
      <div>
        <h1 className='text-2xl font-semibold text-foreground'>API Key Management</h1>
        <p className='text-sm text-default-500 mt-1'>
          API keys are provisioned and rotated via the Django admin or the{' '}
          <code className='font-mono text-xs'>engram connect</code> CLI.
        </p>
      </div>

      <div className='surface-card p-5 space-y-4'>
        <div className='flex items-start gap-3'>
          <div className='w-10 h-10 rounded-lg bg-content2 flex items-center justify-center shrink-0'>
            <KeyRound className='w-5 h-5 text-foreground' />
          </div>
          <div className='min-w-0'>
            <h2 className='text-base font-semibold text-foreground'>How keys are managed</h2>
            <p className='text-sm text-default-500 mt-1'>
              There is no dedicated key-management API surface in the Engram
              admin yet. Keys live in the Django admin and the{' '}
              <code className='font-mono text-xs'>engram connect</code> CLI.
            </p>
          </div>
        </div>

        {DJANGO_ADMIN_URL && (
          <a
            className='inline-flex items-center gap-2 text-sm font-medium text-primary hover:underline'
            href={DJANGO_ADMIN_URL}
            rel='noreferrer'
            target='_blank'
          >
            Open Django admin
            <ExternalLink className='w-4 h-4' />
          </a>
        )}

        {!DJANGO_ADMIN_URL && (
          <pre className='text-xs text-default-500 bg-content2/50 rounded-medium p-3'>
            NEXT_PUBLIC_ENGRAM_DJANGO_ADMIN_URL is not set.
          </pre>
        )}
      </div>

      <div className='surface-card p-5'>
        <h2 className='text-base font-semibold text-foreground mb-3'>Your capabilities</h2>

        {meQuery.isLoading && <p className='text-default-500'>Loading profile...</p>}

        {meQuery.isError && (
          <pre className='text-sm text-danger-500 bg-danger-50 dark:bg-danger-500/10 rounded-medium p-3'>
            {meQuery.error instanceof Error ? meQuery.error.message : 'Failed to load profile.'}
          </pre>
        )}

        {profile && (
          <dl className='grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-2 text-sm'>
            <div>
              <dt className='text-default-500'>Signed in user</dt>
              <dd className='font-mono text-foreground break-all'>{profile.username}</dd>
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
        )}
      </div>
    </section>
  );
}
