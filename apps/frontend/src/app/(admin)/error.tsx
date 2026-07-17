'use client';

import * as Sentry from '@sentry/nextjs';
import { AlertTriangle } from 'lucide-react';
import Link from 'next/link';
import * as React from 'react';

export default function AdminSegmentError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  React.useEffect(() => {
    // No-op when Sentry was never initialised (no DSN configured).
    Sentry.captureException(error);
    console.error(error);
  }, [error]);

  return (
    <div className='flex min-h-[60vh] flex-col items-center justify-center px-6 text-center'>
      <div className='flex h-12 w-12 items-center justify-center rounded-[14px] border border-danger/30 bg-danger/10 text-danger'>
        <AlertTriangle className='h-6 w-6' strokeWidth={1.8} />
      </div>
      <h1 className='mt-4 text-lg font-semibold text-foreground'>
        Something went wrong
      </h1>
      <p className='mt-2 max-w-md text-sm leading-relaxed text-default-500'>
        This page hit an unexpected error and could not finish loading. You can
        retry, or head back to the dashboard.
      </p>
      <div className='mt-5 flex items-center gap-3'>
        <button
          type='button'
          onClick={reset}
          className='inline-flex items-center gap-1.5 rounded-[10px] border border-primary/30 bg-primary/10 px-4 py-2 text-[13px] font-semibold text-primary-300 transition-colors hover:bg-primary/20'
        >
          Try again
        </button>
        <Link
          href='/'
          className='inline-flex items-center gap-1.5 rounded-[10px] border border-divider bg-content1 px-4 py-2 text-[13px] font-medium text-default-600 transition-colors hover:text-foreground'
        >
          Go to dashboard
        </Link>
      </div>
    </div>
  );
}
