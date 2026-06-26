import { checkBackendHealth } from '../api';

export const dynamic = 'force-dynamic';

export default async function HealthPage() {
  const health = await checkBackendHealth('/-/healthz/');

  return (
    <section>
      <h1>Backend Health</h1>
      <p>
        Endpoint:{' '}
        <code>
          {process.env.NEXT_PUBLIC_ENGRAM_API_URL ?? 'http://localhost:8000'}
          /-/healthz/
        </code>
      </p>
      <p>
        Status:{' '}
        <strong style={{ color: health.ok ? 'green' : 'crimson' }}>
          {health.ok ? 'healthy' : 'unhealthy'}
        </strong>
      </p>
      <h2>Response</h2>
      <pre
        style={{
          background: '#f6f6f6',
          padding: '0.75rem',
          borderRadius: '4px',
        }}
      >
        {health.detail || '(empty)'}
      </pre>
    </section>
  );
}
