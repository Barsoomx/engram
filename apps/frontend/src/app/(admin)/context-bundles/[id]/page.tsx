'use client';

import { useQuery } from '@tanstack/react-query';
import { AlertTriangle, ChevronLeft } from 'lucide-react';
import Link from 'next/link';
import { useParams } from 'next/navigation';
import * as React from 'react';

import { CapabilityGate } from '@/components/ui/capability-gate';
import { ConfidenceTrack } from '@/components/ui/confidence-track';
import { EmptyState } from '@/components/ui/empty-state';
import { ErrorState } from '@/components/ui/error-state';
import { KindBadge } from '@/components/ui/kind-badge';
import { StatusPill } from '@/components/ui/status-pill';
import { TimeStamp } from '@/components/ui/time-stamp';
import { fetchMe, type MeResponse } from '@/lib/auth';
import {
  getContextBundle,
  type ContextBundleDetail,
  type ContextBundleEntry,
  type ContextBundleWarning,
} from '@/lib/console-api';
import { useProjectStore } from '@/lib/project-store';
import { useTeamStore } from '@/lib/team-store';

function confidencePct(value: string | null): number | null {
  if (value === null) {
    return null;
  }

  const parsed = Number(value);

  if (!Number.isFinite(parsed)) {
    return null;
  }

  const pct = parsed <= 1 ? parsed * 100 : parsed;

  return Math.max(0, Math.min(100, Math.round(pct)));
}

function hasKeys(value: Record<string, unknown> | null | undefined): boolean {
  return Boolean(value && Object.keys(value).length > 0);
}

function BackLink() {
  return (
    <Link
      href='/context-bundles'
      className='inline-flex items-center gap-1 text-[13px] font-medium text-default-500 transition-colors hover:text-foreground'
    >
      <ChevronLeft size={16} strokeWidth={2} />
      All bundles
    </Link>
  );
}

function MetaRow({ label, value, mono, accent }: { label: string; value: React.ReactNode; mono?: boolean; accent?: boolean }) {
  return (
    <div className='flex items-baseline justify-between gap-4'>
      <span className='shrink-0 text-[12px] text-default-500'>{label}</span>
      <span
        className={`min-w-0 truncate text-right text-[12.5px] font-semibold ${accent ? 'text-primary-300' : 'text-foreground'}${
          mono ? ' font-mono text-[11.5px]' : ''
        }`}
      >
        {value}
      </span>
    </div>
  );
}

function BundleItem({ item }: { item: ContextBundleEntry }) {
  const pct = confidencePct(item.confidence);

  return (
    <div className='surface-card flex items-start gap-3 p-4'>
      <span className='tnum inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-[8px] bg-content3 font-mono text-[12px] font-semibold text-primary-300'>
        {item.rank}
      </span>
      <div className='min-w-0 flex-1 space-y-1.5'>
        <div className='flex flex-wrap items-center gap-2'>
          <KindBadge kind={item.kind} />
          <p className='min-w-0 truncate text-[13.5px] font-medium leading-snug text-foreground'>
            {item.citation || '(no citation)'}
          </p>
        </div>
        {item.memory_id ? (
          <Link
            href={`/memories/${item.memory_id}`}
            className='block truncate font-mono text-[11.5px] text-primary-300 hover:underline'
            title={item.memory_id}
          >
            {item.memory_id}
          </Link>
        ) : (
          <p className='font-mono text-[11.5px] text-default-400'>—</p>
        )}
        <p className='text-[12.5px] leading-relaxed text-default-500'>
          {item.inclusion_reason || 'No inclusion reason recorded.'}
        </p>
        {hasKeys(item.scope_evidence) ? (
          <p
            className='truncate font-mono text-[11px] text-default-400'
            title={JSON.stringify(item.scope_evidence, null, 2)}
          >
            scope: {JSON.stringify(item.scope_evidence)}
          </p>
        ) : null}
        {pct !== null && (
          <div className='flex items-center gap-2.5 pt-0.5'>
            <span className='tnum font-mono text-[12px] text-default-400'>{pct}% conf</span>
            <ConfidenceTrack value={pct} />
          </div>
        )}
      </div>
    </div>
  );
}

function WarningsBanner({ warnings }: { warnings: ContextBundleWarning[] }) {
  if (warnings.length === 0) {
    return null;
  }

  return (
    <div className='space-y-2 rounded-[16px] border border-warning/30 bg-warning/[0.06] px-5 py-4'>
      <div className='flex items-center gap-2'>
        <AlertTriangle className='h-4 w-4 shrink-0 text-warning' strokeWidth={1.8} />
        <span className='text-[13px] font-semibold text-warning'>
          Warnings
          <span className='tnum ml-2 font-normal text-warning/70'>{warnings.length}</span>
        </span>
      </div>
      <ul className='space-y-1.5'>
        {warnings.map((warning, index) => (
          <li key={`${warning.code}-${warning.memory_id ?? index}`} className='flex items-start gap-2.5'>
            <span className='tnum shrink-0 rounded-[6px] bg-warning/15 px-1.5 py-0.5 font-mono text-[10.5px] text-warning'>
              {warning.code}
            </span>
            <span className='text-[12.5px] leading-relaxed text-warning/90'>{warning.message}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function BundleDetailContent({ data }: { data: ContextBundleDetail }) {
  const items = React.useMemo(() => [...data.items].sort((a, b) => a.rank - b.rank), [data.items]);
  const hasRendered = Boolean(data.rendered_text && data.rendered_text.trim());

  return (
    <div className='space-y-6'>
      <div className='space-y-3'>
        <div className='flex flex-wrap items-center gap-3'>
          <h1 className='text-[26px] font-semibold leading-[1.25] tracking-[-0.02em] text-foreground'>
            {data.purpose || '(no purpose)'}
          </h1>
          <StatusPill status={data.status} />
        </div>
      </div>

      <div className='grid grid-cols-1 gap-6 lg:grid-cols-[1.7fr_1fr]'>
        <div className='space-y-6'>
          <div className='surface-card space-y-3 p-[22px]'>
            <h2 className='text-[14.5px] font-semibold text-foreground'>Rendered context</h2>
            {hasRendered ? (
              <pre className='max-h-[480px] overflow-auto whitespace-pre-wrap break-words rounded-[12px] bg-content2/60 p-4 font-mono text-[12px] leading-relaxed text-default-700'>
                {data.rendered_text}
              </pre>
            ) : (
              <p className='text-[13.5px] leading-relaxed text-default-500'>No rendered context recorded for this bundle.</p>
            )}
          </div>

          <WarningsBanner warnings={data.warnings ?? []} />

          <div className='space-y-3'>
            <h2 className='text-[14.5px] font-semibold text-foreground'>
              Items
              <span className='tnum ml-2 text-[12px] font-normal text-default-400'>{items.length}</span>
            </h2>
            {items.length > 0 ? (
              <div className='space-y-2'>
                {items.map((item) => (
                  <BundleItem key={item.id} item={item} />
                ))}
              </div>
            ) : (
              <div className='surface-card px-4 py-5 text-[13px] text-default-500'>No items were selected for this bundle.</div>
            )}
          </div>
        </div>

        <div className='space-y-4'>
          <div className='surface-card space-y-4 p-[22px]'>
            <span className='block text-[10.5px] font-semibold uppercase tracking-[0.12em] text-default-400'>Bundle</span>
            <div className='space-y-1.5'>
              <span className='block text-[12px] text-default-500'>Query</span>
              <p className='whitespace-pre-wrap break-words text-[13px] leading-relaxed text-default-700'>{data.query_text || '—'}</p>
            </div>
            <div className='space-y-3 border-t border-divider pt-4'>
              <MetaRow label='Token budget' value={data.token_budget?.toLocaleString() ?? '—'} mono />
              <MetaRow label='Selected' value={String(data.selected_count)} mono />
              <MetaRow
                label='Retrieval latency'
                value={data.retrieval_latency_ms != null ? `${data.retrieval_latency_ms} ms` : 'not recorded'}
                mono
              />
              <MetaRow label='Agent' value={data.agent_id || '—'} mono accent />
              <MetaRow label='Session' value={data.session_id || '—'} mono />
              <MetaRow label='Request' value={data.request_id || '—'} mono />
              <MetaRow label='Created' value={<TimeStamp value={data.created_at} />} />
            </div>
          </div>

          <div className='surface-card space-y-2 p-[22px]'>
            <span className='block text-[10.5px] font-semibold uppercase tracking-[0.12em] text-default-400'>
              Authorization scope
            </span>
            {hasKeys(data.authorization_scope) ? (
              <pre className='max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-[10px] bg-content2/60 p-3 font-mono text-[11.5px] text-default-700'>
                {JSON.stringify(data.authorization_scope, null, 2)}
              </pre>
            ) : (
              <p className='text-[12.5px] text-default-500'>No scope evidence recorded.</p>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

export default function ContextBundleDetailPage() {
  const params = useParams<{ id: string }>();
  const id = params?.id ?? '';

  const meQuery = useQuery<MeResponse>({ queryKey: ['auth', 'me'], queryFn: fetchMe });
  const capabilities = meQuery.data?.capabilities ?? [];

  const activeProjectId = useProjectStore((s) => s.activeProjectId);
  const activeTeamId = useTeamStore((s) => s.activeTeamId);

  const query = useQuery({
    queryKey: ['inspection', 'context-bundles', id, activeProjectId, activeTeamId],
    enabled: Boolean(activeProjectId) && Boolean(id),
    queryFn: () => getContextBundle(id, { projectId: activeProjectId ?? '', teamId: activeTeamId }),
  });

  return (
    <CapabilityGate capabilities={capabilities} required='context:read'>
      <section className='animate-fade-up space-y-6'>
        <BackLink />

        {!activeProjectId ? (
          <EmptyState
            title='Select a project'
            description='Choose a project from the switcher above to inspect this context bundle.'
          />
        ) : query.isLoading ? (
          <p className='text-[13.5px] text-default-500'>Loading bundle…</p>
        ) : query.isError ? (
          <ErrorState
            title='Failed to load context bundle'
            message={query.error instanceof Error ? query.error.message : 'The context bundle could not be loaded.'}
            onRetry={() => query.refetch()}
          />
        ) : query.data ? (
          <BundleDetailContent data={query.data} />
        ) : null}
      </section>
    </CapabilityGate>
  );
}
