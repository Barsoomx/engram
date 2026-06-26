import { checkBackendHealth } from './api';

export default async function HomePage() {
  const health = await checkBackendHealth('/-/healthz/');

  return (
    <section>
      <h1>Engram Admin</h1>
      <p>
        Backend status:{' '}
        <strong style={{ color: health.ok ? 'green' : 'crimson' }}>
          {health.ok ? 'reachable' : 'unreachable'}
        </strong>
      </p>
      {health.detail && (
        <pre
          style={{
            background: '#f6f6f6',
            padding: '0.75rem',
            borderRadius: '4px',
          }}
        >
          {health.detail}
        </pre>
      )}
      <p>
        <a href='/health'>View dedicated health page</a>
      </p>
    </section>
  );
}
