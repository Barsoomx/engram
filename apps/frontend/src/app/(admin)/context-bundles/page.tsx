'use client';

import { Input, Select, SelectItem } from '@heroui/react';
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { Layers } from 'lucide-react';
import Link from 'next/link';
import * as React from 'react';

import { CapabilityGate } from '@/components/ui/capability-gate';
import { DateTimeInput } from '@/components/ui/datetime-input';
import { EmptyState } from '@/components/ui/empty-state';
import { ErrorState } from '@/components/ui/error-state';
import { PageHeader } from '@/components/ui/page-header';
import { StatusPill } from '@/components/ui/status-pill';
import { TimeStamp } from '@/components/ui/time-stamp';
import { useDebouncedValue } from '@/hooks/use-debounced-value';
import { useUrlFilters } from '@/hooks/use-url-filters';
import { fetchMe, type MeResponse } from '@/lib/auth';
import {
  listContextBundles,
  type ContextBundleListItem,
  type ContextBundleOrdering,
} from '@/lib/console-api';
import { useProjectStore } from '@/lib/project-store';
import { useTeamStore } from '@/lib/team-store';

const GRID_COLUMNS =
  'minmax(0,1.7fr) minmax(0,0.9fr) minmax(0,0.6fr) minmax(0,0.7fr) minmax(0,1.3fr) minmax(0,0.7fr)';

const STATUS_OPTIONS = ['created', 'injected', 'skipped'] as const;

const ORDERING_OPTIONS: { key: ContextBundleOrdering; label: string }[] = [
  { key: '-created_at', label: 'Newest first' },
  { key: 'created_at', label: 'Oldest first' },
];

const PAGE_SIZE = 50;

type BundleFilters = {
  status: string;
  session_id: string;
  since: string;
  until: string;
  ordering: ContextBundleOrdering;
  page: number;
};

const DEFAULT_FILTERS: BundleFilters = {
  status: '',
  session_id: '',
  since: '',
  until: '',
  ordering: '-created_at',
  page: 0,
};

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
      <span className='truncate text-[13.5px] font-medium text-foreground' title={bundle.purpose || undefined}>
        {bundle.purpose || '(no purpose)'}
      </span>
      <span className='min-w-0'>
        <StatusPill status={bundle.status} />
      </span>
      <span className='tnum font-mono text-[12px] text-default-500'>{bundle.selected_count}</span>
      <span className='tnum font-mono text-[12px] text-default-500'>{bundle.token_budget?.toLocaleString() ?? '—'}</span>
      <span className='min-w-0'>
        <span className='block truncate font-mono text-[11.5px] text-default-500' title={bundle.session_id || undefined}>
          {bundle.session_id || '—'}
        </span>
        {bundle.agent_id && (
          <span className='block truncate font-mono text-[11px] text-default-400' title={bundle.agent_id}>
            {bundle.agent_id}
          </span>
        )}
      </span>
      <span className='whitespace-nowrap text-[12px] text-default-400'>
        <TimeStamp value={bundle.created_at} />
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

function BundleFilterBar({
  filters,
  sessionInput,
  onSessionInput,
  onChange,
  onReset,
}: {
  filters: BundleFilters;
  sessionInput: string;
  onSessionInput: (value: string) => void;
  onChange: (next: Partial<BundleFilters>) => void;
  onReset: () => void;
}) {
  return (
    <div className='surface-card grid grid-cols-1 gap-4 p-4 sm:grid-cols-2 lg:grid-cols-6'>
      <Select
        label='Status'
        labelPlacement='outside'
        placeholder='Any'
        selectedKeys={filters.status ? new Set([filters.status]) : new Set<string>()}
        onSelectionChange={(keys) => {
          const next = Array.from(keys)[0];
          onChange({ status: typeof next === 'string' ? next : '', page: 0 });
        }}
      >
        {STATUS_OPTIONS.map((status) => (
          <SelectItem key={status}>{status}</SelectItem>
        ))}
      </Select>
      <Input
        label='Session ID'
        labelPlacement='outside'
        placeholder='uuid'
        value={sessionInput}
        onValueChange={onSessionInput}
        isClearable
        onClear={() => onSessionInput('')}
      />
      <DateTimeInput
        label='Since'
        value={filters.since}
        onValueChange={(value) => onChange({ since: value, page: 0 })}
      />
      <DateTimeInput
        label='Until'
        value={filters.until}
        onValueChange={(value) => onChange({ until: value, page: 0 })}
      />
      <Select
        label='Sort'
        labelPlacement='outside'
        disallowEmptySelection
        selectedKeys={new Set([filters.ordering])}
        onSelectionChange={(keys) => {
          const next = Array.from(keys)[0];

          if (typeof next === 'string') {
            onChange({ ordering: next as ContextBundleOrdering, page: 0 });
          }
        }}
      >
        {ORDERING_OPTIONS.map((option) => (
          <SelectItem key={option.key}>{option.label}</SelectItem>
        ))}
      </Select>
      <div className='flex items-end'>
        <button
          type='button'
          onClick={onReset}
          className='h-10 rounded-[10px] border border-divider bg-content1 px-3.5 text-[12.5px] font-medium text-default-500 transition-colors hover:text-foreground'
        >
          Reset
        </button>
      </div>
    </div>
  );
}

function toIso(value: string): string | undefined {
  if (!value) {
    return undefined;
  }

  const date = new Date(value);

  return Number.isNaN(date.getTime()) ? undefined : date.toISOString();
}

export default function ContextBundlesPage() {
  const meQuery = useQuery<MeResponse>({ queryKey: ['auth', 'me'], queryFn: fetchMe });
  const capabilities = meQuery.data?.capabilities ?? [];

  const activeProjectId = useProjectStore((s) => s.activeProjectId);
  const activeTeamId = useTeamStore((s) => s.activeTeamId);

  const [filters, setFilters, resetFilters] = useUrlFilters<BundleFilters>(DEFAULT_FILTERS);
  const [sessionInput, setSessionInput] = React.useState(filters.session_id);
  const debouncedSession = useDebouncedValue(sessionInput, 300);

  React.useEffect(() => {
    if (debouncedSession !== filters.session_id) {
      setFilters({ session_id: debouncedSession, page: 0 });
    }
  }, [debouncedSession, filters.session_id, setFilters]);

  const query = useQuery({
    queryKey: [
      'inspection',
      'context-bundles',
      activeProjectId,
      activeTeamId,
      filters.status,
      filters.session_id,
      filters.since,
      filters.until,
      filters.ordering,
      filters.page,
    ],
    enabled: Boolean(activeProjectId),
    placeholderData: keepPreviousData,
    queryFn: () =>
      listContextBundles({
        projectId: activeProjectId ?? '',
        teamId: activeTeamId,
        limit: PAGE_SIZE,
        offset: filters.page * PAGE_SIZE,
        status: filters.status || undefined,
        session_id: filters.session_id || undefined,
        since: toIso(filters.since),
        until: toIso(filters.until),
        ordering: filters.ordering,
      }),
  });

  const items = query.data?.items ?? [];
  const total = query.data?.count ?? 0;

  return (
    <CapabilityGate capabilities={capabilities} required='context:read'>
      <section className='space-y-6'>
        <PageHeader title='Context Bundles' subtitle='Assembled context delivered to agents.' />

        {!activeProjectId ? (
          <EmptyState
            title='Select a project'
            description='Choose a project from the switcher above to inspect its assembled context bundles.'
            icon={<Layers className='h-6 w-6' />}
          />
        ) : (
          <>
            <BundleFilterBar
              filters={filters}
              sessionInput={sessionInput}
              onSessionInput={setSessionInput}
              onChange={setFilters}
              onReset={() => {
                setSessionInput('');
                resetFilters();
              }}
            />

            {query.isLoading ? (
              <BundlesTableSkeleton />
            ) : query.isError ? (
              <ErrorState
                title='Failed to load context bundles'
                message={query.error instanceof Error ? query.error.message : 'The context bundles could not be loaded.'}
                onRetry={() => query.refetch()}
              />
            ) : items.length === 0 ? (
              <EmptyState
                title='No context bundles'
                description='No bundles match the current filters for this project.'
                icon={<Layers className='h-6 w-6' />}
              />
            ) : (
              <>
                <BundlesTable items={items} />
                <div className='flex items-center justify-between gap-3'>
                  <p className='text-[12px] text-default-400'>
                    {total > 0
                      ? `Showing ${filters.page * PAGE_SIZE + 1}-${filters.page * PAGE_SIZE + items.length} of ${total}`
                      : `Showing ${items.length} bundle${items.length === 1 ? '' : 's'}`}
                  </p>
                  <div className='flex items-center gap-2'>
                    <button
                      type='button'
                      onClick={() => setFilters({ page: Math.max(0, filters.page - 1) })}
                      disabled={filters.page === 0}
                      className='rounded-[9px] border border-divider bg-content1 px-3 py-1.5 text-[12.5px] font-medium text-default-600 transition-colors hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50'
                    >
                      Previous
                    </button>
                    <button
                      type='button'
                      onClick={() => setFilters({ page: filters.page + 1 })}
                      disabled={(filters.page + 1) * PAGE_SIZE >= total}
                      className='rounded-[9px] border border-divider bg-content1 px-3 py-1.5 text-[12.5px] font-medium text-default-600 transition-colors hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50'
                    >
                      Next
                    </button>
                  </div>
                </div>
              </>
            )}
          </>
        )}
      </section>
    </CapabilityGate>
  );
}
