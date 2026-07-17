'use client';

import * as Sentry from '@sentry/nextjs';
import * as React from 'react';

export default function GlobalError({
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
    <html lang='en'>
      <body
        style={{
          margin: 0,
          minHeight: '100vh',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          backgroundColor: '#0A0C11',
          color: '#ECEDEE',
          fontFamily: 'ui-sans-serif, system-ui, sans-serif',
          padding: '24px',
        }}
      >
        <div style={{ textAlign: 'center', maxWidth: 420 }}>
          <h1 style={{ fontSize: 18, fontWeight: 600, margin: 0 }}>
            Something went wrong
          </h1>
          <p
            style={{
              marginTop: 8,
              fontSize: 14,
              lineHeight: 1.6,
              color: '#8B8D93',
            }}
          >
            The application hit an unexpected error. Please reload the page.
          </p>
          <button
            type='button'
            onClick={reset}
            style={{
              marginTop: 20,
              cursor: 'pointer',
              borderRadius: 10,
              border: '1px solid rgba(99,102,241,0.3)',
              backgroundColor: 'rgba(99,102,241,0.1)',
              color: '#A5B4FC',
              padding: '8px 16px',
              fontSize: 13,
              fontWeight: 600,
            }}
          >
            Reload
          </button>
        </div>
      </body>
    </html>
  );
}
