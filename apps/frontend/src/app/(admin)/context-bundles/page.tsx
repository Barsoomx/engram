'use client';

import { useQuery } from '@tanstack/react-query';
import { AlertTriangle, Layers } from 'lucide-react';
import Link from 'next/link';

import { CapabilityGate } from '@/components/ui/capability-gate';
import { EmptyState } from '@/components/ui/empty-state';
import { PageHeader } from '@/components/ui/page-header';
import { fetchMe, type MeResponse } from '@/lib/auth';
import {
  listContextBundles,
  type ContextBundleListItem,
} from '@/lib/console-api';
import { formatRelativeTime } from '@/lib/design';
import { useProjectStore } from '@/lib/project-store';
import { useTeamStore } from '@/lib/team-store';

const GRID_COLUMNS =
  'minmax(0,1.7fr) minmax(0,0.9fr) minmax(0,0.6fr) minmax(0,0.7fr) minmax(0,1.3fr) minmax(0,0.7fr)';

function statusTone(status: string): { text: string; bg: string } {
  const value = status.toLowerCase();

  if (
    ['rendered', 'ready', 'authorized', 'active', 'completed', 'complete', 'ok'].includes(
      value,
    )
  ) {
    return { text: 'text-success', bg: 'rgba(61,217,172,0.13)' };
  }

  if (['pending', 'partial', 'rendering', 'queued', 'building'].includes(value)) {
    return { text: 'text-warning', bg: 'rgba(242,183,101,0.14)' };
  }

  if (['failed', 'error', 'denied', 'rejected', 'empty'].includes(value)) {
    return { text: 'text-danger', bg: 'rgba(251,110,114,0.13)' };
  }

  return { text: 'text-default-500', bg: 'rgba(255,255,255,0.05)' };
}

function StatusPill({ status }: { status: string }) {
  const tone = statusTone(status);

  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-[7px] px-2.5 py-1 text-[11.5px] font-medium ${tone.text}`}
      style={{ backgroundColor: tone.bg }}
    >
      <span className='h-1.5 w-1.5 rounded-full bg-current' />
      {status || 'unknown'}
    </span>
  );
}

function ColumnHeader() {
  return (
    <div
      className='grid items-center gap-4 border-b border-divider px-5 py-3 text-[10.5px] font-semibold uppercase tracking-[0.1em] text-default-400'
      style={{ gridTemplateColumns: GRID_COLUMNS }}
    >
      <span>Purpose</span>
      <span>Status</span>
      <span>Selected</span>
      <span>Budget</span>
      <span>Session</span>
      <span>Created</span>
    </div>
  );
}

function BundleRow({ bundle }: { bundle: ContextBundleListItem }) {
  return (
    <Link
      href={`/context-bundles/${bundle.id}`}
      className='grid items-center gap-4 border-b border-divider px-5 py-3.5 transition-colors last:border-b-0 hover:bg-content2/60'
      style={{ gridTemplateColumns: GRID_COLUMNS }}
    >
      <span className='truncate text-[13.5px] font-medium text-foreground'>
        {bundle.purpose || '(no purpose)'}
      </span>
      <span className='min-w-0'>
        <StatusPill status={bundle.status} />
      </span>
      <span className='tnum font-mono text-[12px] text-default-500'>
        {bundle.selected_count}
      </span>
      <span className='tnum font-mono text-[12px] text-default-500'>
        {bundle.token_budget.toLocaleString()}
      </span>
      <span className='min-w-0'>
        <span className='block truncate font-mono text-[11.5px] text-default-500'>
          {bundle.session_id || '—'}
        </span>
        {bundle.agent_id && (
          <span className='block truncate font-mono text-[11px] text-default-400'>
            {bundle.agent_id}
          </span>
        )}
      </span>
      <span className='whitespace-nowrap text-[12px] text-default-400'>
        {formatRelativeTime(bundle.created_at)}
      </span>
    </Link>
  );
}

function BundlesTable({ items }: { items: ContextBundleListItem[] }) {
  return (
    <div className='surface-card overflow-hidden'>
      <div className='overflow-x-auto'>
        <div className='min-w-[760px]'>
          <ColumnHeader />
          {items.map((bundle) => (
            <BundleRow key={bundle.id} bundle={bundle} />
          ))}
        </div>
      </div>
    </div>
  );
}

function BundlesTableSkeleton() {
  return (
    <div className='surface-card overflow-hidden'>
      <div className='overflow-x-auto'>
        <div className='min-w-[760px]'>
          <ColumnHeader />
          {Array.from({ length: 6 }).map((_, index) => (
            <div
              key={index}
              className='grid items-center gap-4 border-b border-divider px-5 py-3.5 last:border-b-0'
              style={{ gridTemplateColumns: GRID_COLUMNS }}
            >
              <span className='h-3.5 w-40 rounded-medium bg-content2' />
              <span className='h-5 w-16 rounded-[7px] bg-content2' />
              <span className='h-3 w-8 rounded-medium bg-content2' />
              <span className='h-3 w-12 rounded-medium bg-content2' />
              <span className='h-3 w-28 rounded-medium bg-content2' />
              <span className='h-3 w-12 rounded-medium bg-content2' />
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

export default function ContextBundlesPage() {
  const meQuery = useQuery<MeResponse>({
    queryKey: ['auth', 'me'],
    queryFn: fetchMe,
  });
  const capabilities = meQuery.data?.capabilities ?? [];

  const activeProjectId = useProjectStore((s) => s.activeProjectId);
  const activeTeamId = useTeamStore((s) => s.activeTeamId);

  const query = useQuery({
    queryKey: ['inspection', 'context-bundles', activeProjectId, activeTeamId],
    enabled: Boolean(activeProjectId),
    queryFn: () =>
      listContextBundles({
        projectId: activeProjectId ?? '',
        teamId: activeTeamId,
      }),
  });

  const items = query.data?.items ?? [];

  return (
    <CapabilityGate capabilities={capabilities} required='memories:admin'>
      <section className='space-y-6'>
        <PageHeader
          title='Context Bundles'
          subtitle='Assembled context delivered to agents.'
        />

        {!activeProjectId ? (
          <EmptyState
            title='Select a project'
            description='Choose a project from the switcher above to inspect its assembled context bundles.'
            icon={<Layers className='h-6 w-6' />}
          />
        ) : query.isLoading ? (
          <BundlesTableSkeleton />
        ) : query.isError ? (
          <div className='flex items-start gap-3 rounded-[16px] border border-danger/30 bg-danger/5 px-5 py-4'>
            <AlertTriangle className='mt-0.5 h-5 w-5 shrink-0 text-danger' />
            <p className='text-[13px] leading-relaxed text-danger'>
              {query.error instanceof Error
                ? query.error.message
                : 'Failed to load context bundles.'}
            </p>
          </div>
        ) : items.length === 0 ? (
          <EmptyState
            title='No context bundles yet'
            description='Context bundles assembled and delivered to your agents for this project will appear here.'
            icon={<Layers className='h-6 w-6' />}
          />
        ) : (
          <>
            <BundlesTable items={items} />
            <p className='text-[12px] text-default-400'>
              Showing {items.length} bundle{items.length === 1 ? '' : 's'}.
            </p>
          </>
        )}
      </section>
    </CapabilityGate>
  );
}
