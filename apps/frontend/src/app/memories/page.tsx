import { fetchAdminJson } from '../api';

export const dynamic = 'force-dynamic';

type MemoryItem = {
  id: string;
  project_id: string;
  team_id: string | null;
  title: string;
  body: string;
  status: string;
  visibility_scope: string;
  current_version: number;
  confidence: string | null;
  stale: boolean;
  refuted: boolean;
  created_at: string | null;
  updated_at: string | null;
};

type MemoriesResponse = {
  count: number;
  items: MemoryItem[];
};

const TABLE_STYLE = {
  width: '100%',
  borderCollapse: 'collapse',
  textAlign: 'left',
} as const;

const CELL_STYLE = {
  border: '1px solid #e5e5e5',
  padding: '0.5rem 0.75rem',
  verticalAlign: 'top',
} as const;

function StatusBadge({ status }: { status: string }) {
  const color = status === 'active' ? 'green' : status === 'stale' ? '#b8860b' : '#555';

  return <strong style={{ color }}>{status}</strong>;
}

function MemoriesTable({ items }: { items: MemoryItem[] }) {
  return (
    <table style={TABLE_STYLE}>
      <thead>
        <tr>
          <th style={CELL_STYLE}>ID</th>
          <th style={CELL_STYLE}>Title</th>
          <th style={CELL_STYLE}>Status</th>
          <th style={CELL_STYLE}>Visibility</th>
        </tr>
      </thead>
      <tbody>
        {items.map((memory) => (
          <tr key={memory.id}>
            <td style={{ ...CELL_STYLE, fontFamily: 'monospace', fontSize: '0.85em' }}>
              {memory.id}
            </td>
            <td style={CELL_STYLE}>{memory.title || '(untitled)'}</td>
            <td style={CELL_STYLE}>
              <StatusBadge status={memory.status} />
            </td>
            <td style={CELL_STYLE}>{memory.visibility_scope}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export default async function MemoriesPage() {
  const result = await fetchAdminJson<MemoriesResponse>('/v1/inspection/memories/');

  return (
    <section>
      <h1>Memories</h1>
      <p>
        Endpoint:{' '}
        <code>
          {process.env.NEXT_PUBLIC_ENGRAM_API_URL ?? 'http://localhost:8000'}
          /v1/inspection/memories/
        </code>
      </p>

      {result.ok ? (
        <>
          <p>Total: {result.data.count}</p>
          {result.data.items.length > 0 ? (
            <MemoriesTable items={result.data.items} />
          ) : (
            <p style={{ color: '#555' }}>No memories found for this project.</p>
          )}
        </>
      ) : (
        <pre
          style={{
            background: '#f6f6f6',
            padding: '0.75rem',
            borderRadius: '4px',
            color: result.reason === 'missing-config' ? '#555' : 'crimson',
          }}
        >
          {result.detail}
        </pre>
      )}
    </section>
  );
}
