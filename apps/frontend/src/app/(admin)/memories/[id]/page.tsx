'use client';

import { useQuery } from '@tanstack/react-query';
import { Check, ChevronLeft } from 'lucide-react';
import Link from 'next/link';
import { useParams } from 'next/navigation';

import { ConfidenceTrack } from '@/components/ui/confidence-track';
import { KindBadge, KindDot } from '@/components/ui/kind-badge';
import { PrimaryButton } from '@/components/ui/primary-button';
import { apiClient } from '@/lib/auth';
import { KIND_STYLES, formatRelativeTime, resolveKind } from '@/lib/design';
import { useProjectStore } from '@/lib/project-store';
import { useTeamStore } from '@/lib/team-store';

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

type RelatedItem = {
  key: string;
  title: string;
  mono: boolean;
};

function metaString(meta: Record<string, unknown> | null, key: string): string | null {
  const value = meta?.[key];

  return typeof value === 'string' && value.trim() ? value.trim() : null;
}

function parseConfidence(value: string | null): number | null {
  if (!value) {
    return null;
  }

  const parsed = Number.parseFloat(value);

  if (!Number.isFinite(parsed)) {
    return null;
  }

  const scaled = parsed <= 1 ? parsed * 100 : parsed;

  return Math.max(0, Math.min(100, Math.round(scaled)));
}

function buildRelated(data: MemoryDetail): RelatedItem[] {
  if (data.retrieval_documents.length > 0) {
    return data.retrieval_documents.map((doc) => ({
      key: doc.id,
      title: doc.content_hash ? doc.content_hash.slice(0, 16) : doc.id.slice(0, 8),
      mono: true,
    }));
  }

  return data.versions.map((version) => ({
    key: `v${version.version}`,
    title: `Version ${version.version}`,
    mono: false,
  }));
}

function BackLink() {
  return (
    <Link
      href='/memories'
      className='inline-flex items-center gap-1 text-[13px] font-medium text-default-500 transition-colors hover:text-foreground'
    >
      <ChevronLeft size={16} strokeWidth={2} />
      All memories
    </Link>
  );
}

function ProvenanceRow({
  label,
  value,
  mono,
  accent,
}: {
  label: string;
  value: string;
  mono?: boolean;
  accent?: boolean;
}) {
  return (
    <div className='flex items-baseline justify-between gap-4'>
      <span className='shrink-0 text-[12px] text-default-500'>{label}</span>
      <span
        className={`min-w-0 truncate text-right text-[12.5px] font-semibold ${
          accent ? 'text-primary-300' : 'text-foreground'
        }${mono ? ' font-mono text-[11.5px]' : ''}`}
      >
        {value}
      </span>
    </div>
  );
}

function MemoryDetailContent({ data }: { data: MemoryDetail }) {
  const meta = data.metadata;
  const kind = resolveKind(metaString(meta, 'kind'));
  const source = metaString(meta, 'source') ?? '—';
  const capturedBy =
    metaString(meta, 'captured_by') ??
    metaString(meta, 'agent') ??
    metaString(meta, 'author') ??
    '—';
  const projectName =
    metaString(meta, 'project') ?? metaString(meta, 'project_slug') ?? data.project_id;
  const confidencePct = parseConfidence(data.confidence);
  const authorized = data.status === 'active' && !data.refuted && !data.stale;
  const chip = authorized
    ? { text: 'Authorized for injection', color: 'text-success', bg: 'rgba(61,217,172,0.13)', icon: true }
    : data.refuted
      ? { text: 'Refuted', color: 'text-danger', bg: 'rgba(251,110,114,0.13)', icon: false }
      : data.stale
        ? { text: 'Stale', color: 'text-warning', bg: 'rgba(242,183,101,0.14)', icon: false }
        : { text: 'Not authorized', color: 'text-default-500', bg: 'rgba(255,255,255,0.05)', icon: false };
  const versionBody =
    data.versions.find((v) => v.version === data.current_version)?.body ??
    data.versions[0]?.body ??
    null;
  const showVersionBody = Boolean(
    versionBody && versionBody.trim() && versionBody.trim() !== data.body.trim(),
  );
  const related = buildRelated(data);

  return (
    <div className='grid grid-cols-1 gap-6 lg:grid-cols-[1.7fr_1fr]'>
      <div className='space-y-5'>
        <div className='space-y-3'>
          <div className='flex items-center gap-3'>
            <KindBadge kind={kind} />
            <span className='min-w-0 truncate font-mono text-[12px] text-default-500'>{source}</span>
          </div>
          <h1 className='text-[26px] font-semibold leading-[1.25] tracking-[-0.02em] text-foreground'>
            {data.title || '(untitled)'}
          </h1>
        </div>

        <div className='surface-card space-y-4 p-[22px]'>
          {data.body ? (
            <p className='whitespace-pre-wrap text-[15px] leading-[1.7] text-default-700'>{data.body}</p>
          ) : (
            <p className='text-[15px] leading-[1.7] text-default-500'>No body recorded.</p>
          )}
          {showVersionBody && versionBody && (
            <p className='whitespace-pre-wrap text-[14px] leading-[1.7] text-default-500'>{versionBody}</p>
          )}
        </div>

        {related.length > 0 && (
          <div className='space-y-3'>
            <h2 className='text-[14.5px] font-semibold text-foreground'>
              Related memories
              <span className='tnum ml-2 text-[12px] font-normal text-default-400'>{related.length}</span>
            </h2>
            <div className='space-y-2'>
              {related.map((item) => (
                <div
                  key={item.key}
                  className='surface-card flex items-center gap-3 px-4 py-3 transition-colors hover:bg-content2'
                >
                  <KindDot kind={kind} size={9} />
                  <span
                    className={`min-w-0 flex-1 truncate text-[13.5px] text-default-700${item.mono ? ' font-mono text-[12px]' : ''}`}
                  >
                    {item.title}
                  </span>
                  <span className='shrink-0 text-[11.5px] text-default-400'>{KIND_STYLES[kind].label}</span>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>

      <div className='space-y-4'>
        <div className='surface-card space-y-3 p-[22px]'>
          <span className='block text-[10.5px] font-semibold uppercase tracking-[0.12em] text-default-400'>
            Provenance
          </span>
          <div className='space-y-3'>
            <ProvenanceRow label='Project' value={projectName} />
            <ProvenanceRow label='Captured by' value={capturedBy} />
            <ProvenanceRow label='Source' value={source} mono accent />
            <ProvenanceRow label='Scope' value={data.visibility_scope} />
            <ProvenanceRow label='Version' value={`v${data.current_version}`} mono />
            {data.team_id && <ProvenanceRow label='Team' value={data.team_id} mono />}
            <ProvenanceRow label='Updated' value={formatRelativeTime(data.updated_at)} />
          </div>
        </div>

        <div className='surface-card space-y-4 p-[22px]'>
          <div className='flex items-center justify-between'>
            <span className='text-[10.5px] font-semibold uppercase tracking-[0.12em] text-default-400'>
              Confidence
            </span>
            <span className='tnum text-[22px] font-semibold tracking-[-0.02em] text-foreground'>
              {confidencePct != null ? `${confidencePct}%` : '—'}
            </span>
          </div>
          <ConfidenceTrack value={confidencePct ?? 0} height={7} className='!w-full' />
          <span
            className={`inline-flex items-center gap-1.5 rounded-[7px] px-2.5 py-1 text-[12px] font-medium ${chip.color}`}
            style={{ backgroundColor: chip.bg }}
          >
            {chip.icon && <Check size={14} strokeWidth={2.5} />}
            {chip.text}
          </span>
        </div>

        <PrimaryButton className='w-full'>Add to context bundle</PrimaryButton>
      </div>
    </div>
  );
}

export default function MemoryDetailPage() {
  const params = useParams<{ id: string }>();
  const id = params?.id ?? '';

  const activeProjectId = useProjectStore((s) => s.activeProjectId);
  const activeTeamId = useTeamStore((s) => s.activeTeamId);

  const query = useQuery<MemoryDetail>({
    queryKey: ['inspection', 'memories', id, activeProjectId, activeTeamId],
    enabled: Boolean(activeProjectId) && Boolean(id),
    queryFn: async () => {
      const client = apiClient();
      const queryParams: Record<string, string> = { project_id: activeProjectId ?? '' };

      if (activeTeamId) {
        queryParams.team_id = activeTeamId;
      }

      const response = await client.get<MemoryDetail>(`/v1/inspection/memories/${id}/`, { params: queryParams });

      return response.data;
    },
  });

  if (!activeProjectId) {
    return (
      <section className='animate-fade-up space-y-6'>
        <BackLink />
        <div className='surface-card p-8'>
          <h1 className='text-[18px] font-semibold text-foreground'>Memory detail</h1>
          <p className='mt-2 text-[13.5px] text-default-500'>
            Select a project to view memory details.
          </p>
        </div>
      </section>
    );
  }

  return (
    <section className='animate-fade-up space-y-6'>
      <BackLink />

      {query.isLoading && <p className='text-[13.5px] text-default-500'>Loading memory…</p>}

      {query.isError && (
        <div className='surface-card p-5 text-[13.5px] text-danger'>
          {query.error instanceof Error ? query.error.message : 'Failed to load memory.'}
        </div>
      )}

      {query.data && <MemoryDetailContent data={query.data} />}
    </section>
  );
}
