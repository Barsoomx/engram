'use client';

import { useQuery } from '@tanstack/react-query';
import Link from 'next/link';
import { useParams } from 'next/navigation';
import * as React from 'react';

import { apiClient } from '@/lib/auth';

type MemoryVersion = {
  version: number;
  body: string | null;
  created_at: string | null;
};

type RetrievalDocument = {
  id: string;
  memory_id: string;
  content_hash: string | null;
  token_count: number | null;
  created_at: string | null;
};

type MemoryDetail = {
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
  metadata: Record<string, unknown> | null;
  created_at: string | null;
  updated_at: string | null;
  versions: MemoryVersion[];
  retrieval_documents: RetrievalDocument[];
};

const PROJECT_ID = process.env.NEXT_PUBLIC_ENGRAM_PROJECT_ID ?? '';
const TEAM_ID = process.env.NEXT_PUBLIC_ENGRAM_TEAM_ID ?? '';

async function fetchMemoryDetail(id: string): Promise<MemoryDetail> {
  const client = apiClient();
  const params: Record<string, string> = { project_id: PROJECT_ID };

  if (TEAM_ID) {
    params.team_id = TEAM_ID;
  }

  const response = await client.get<MemoryDetail>(`/v1/inspection/memories/${id}/`, { params });

  return response.data;
}

function StatusBadge({ status }: { status: string }) {
  const tone =
    status === 'active'
      ? 'text-success-500'
      : status === 'stale'
        ? 'text-warning-500'
        : 'text-default-500';

  return <strong className={tone}>{status}</strong>;
}

function DetailRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className='grid grid-cols-1 md:grid-cols-[200px_1fr] gap-1 md:gap-4 py-2 border-b border-divider/40 last:border-b-0'>
      <dt className='text-xs uppercase tracking-wide text-default-500 font-medium'>{label}</dt>
      <dd className='text-sm text-foreground break-words'>{children}</dd>
    </div>
  );
}

function VersionsList({ versions }: { versions: MemoryVersion[] }) {
  if (versions.length === 0) {

    return <p className='text-default-500'>No versions recorded.</p>;
  }

  return (
    <div className='overflow-x-auto'>
      <table className='w-full border-collapse text-left text-sm'>
        <thead>
          <tr className='border-b border-divider'>
            <th className='py-2 px-3 text-default-500 font-medium'>Version</th>
            <th className='py-2 px-3 text-default-500 font-medium'>Created at</th>
            <th className='py-2 px-3 text-default-500 font-medium'>Body</th>
          </tr>
        </thead>
        <tbody>
          {versions.map((version) => (
            <tr key={version.version} className='border-b border-divider/50'>
              <td className='py-2 px-3 font-mono text-xs text-default-700'>{version.version}</td>
              <td className='py-2 px-3 text-default-700 whitespace-nowrap'>
                {version.created_at ?? '—'}
              </td>
              <td className='py-2 px-3 text-default-700 max-w-md truncate'>
                {version.body ?? '—'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function RetrievalDocumentsList({ documents }: { documents: RetrievalDocument[] }) {
  if (documents.length === 0) {

    return <p className='text-default-500'>No retrieval documents recorded.</p>;
  }

  return (
    <div className='overflow-x-auto'>
      <table className='w-full border-collapse text-left text-sm'>
        <thead>
          <tr className='border-b border-divider'>
            <th className='py-2 px-3 text-default-500 font-medium'>ID</th>
            <th className='py-2 px-3 text-default-500 font-medium'>Content hash</th>
            <th className='py-2 px-3 text-default-500 font-medium'>Tokens</th>
            <th className='py-2 px-3 text-default-500 font-medium'>Created at</th>
          </tr>
        </thead>
        <tbody>
          {documents.map((document) => (
            <tr key={document.id} className='border-b border-divider/50'>
              <td className='py-2 px-3 font-mono text-xs text-default-700 break-all'>{document.id}</td>
              <td className='py-2 px-3 font-mono text-xs text-default-700 break-all'>
                {document.content_hash ?? '—'}
              </td>
              <td className='py-2 px-3 text-default-700'>
                {document.token_count ?? '—'}
              </td>
              <td className='py-2 px-3 text-default-700 whitespace-nowrap'>
                {document.created_at ?? '—'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function MemoryDetailPage() {
  const params = useParams<{ id: string }>();
  const id = params?.id ?? '';

  const query = useQuery<MemoryDetail>({
    queryKey: ['inspection', 'memories', id, PROJECT_ID, TEAM_ID],
    enabled: Boolean(PROJECT_ID) && Boolean(id),
    queryFn: () => fetchMemoryDetail(id),
  });

  if (!PROJECT_ID) {
    return (
      <section>
        <h1 className='text-2xl font-semibold text-foreground'>Memory detail</h1>
        <pre className='mt-4 text-sm text-default-500 bg-content2/50 rounded-medium p-3'>
          NEXT_PUBLIC_ENGRAM_PROJECT_ID is not set.
        </pre>
      </section>
    );
  }

  return (
    <section className='space-y-4'>
      <div>
        <Link href='/memories' className='text-sm text-primary hover:underline'>
          ← Back to memories
        </Link>
        <h1 className='text-2xl font-semibold text-foreground mt-2'>Memory detail</h1>
        <p className='text-xs text-default-500 mt-1 font-mono break-all'>
          {process.env.NEXT_PUBLIC_ENGRAM_API_URL ?? 'http://localhost:8000'}
          /v1/inspection/memories/{id}/
        </p>
      </div>

      {query.isLoading && <p className='text-default-500'>Loading memory...</p>}

      {query.isError && (
        <pre className='text-sm text-danger-500 bg-danger-50 dark:bg-danger-500/10 rounded-medium p-3'>
          {query.error instanceof Error ? query.error.message : 'Failed to load memory.'}
        </pre>
      )}

      {query.data && (
        <>
          <div className='surface-card p-5'>
            <dl className='flex flex-col'>
              <DetailRow label='ID'>
                <span className='font-mono text-xs break-all'>{query.data.id}</span>
              </DetailRow>
              <DetailRow label='Title'>
                {query.data.title || '(untitled)'}
              </DetailRow>
              <DetailRow label='Body'>
                <span className='whitespace-pre-wrap'>{query.data.body}</span>
              </DetailRow>
              <DetailRow label='Status'>
                <StatusBadge status={query.data.status} />
              </DetailRow>
              <DetailRow label='Visibility scope'>
                {query.data.visibility_scope}
              </DetailRow>
              <DetailRow label='Current version'>
                {query.data.current_version}
              </DetailRow>
              <DetailRow label='Confidence'>
                {query.data.confidence ?? '—'}
              </DetailRow>
              <DetailRow label='Stale'>
                {query.data.stale ? 'yes' : 'no'}
              </DetailRow>
              <DetailRow label='Refuted'>
                {query.data.refuted ? 'yes' : 'no'}
              </DetailRow>
              <DetailRow label='Team'>
                {query.data.team_id ?? '—'}
              </DetailRow>
              <DetailRow label='Created at'>
                {query.data.created_at ?? '—'}
              </DetailRow>
              <DetailRow label='Updated at'>
                {query.data.updated_at ?? '—'}
              </DetailRow>
            </dl>
          </div>

          <div className='surface-card p-5'>
            <h2 className='text-base font-semibold text-foreground mb-3'>Versions</h2>
            <VersionsList versions={query.data.versions} />
          </div>

          <div className='surface-card p-5'>
            <h2 className='text-base font-semibold text-foreground mb-3'>Retrieval documents</h2>
            <RetrievalDocumentsList documents={query.data.retrieval_documents} />
          </div>
        </>
      )}
    </section>
  );
}
