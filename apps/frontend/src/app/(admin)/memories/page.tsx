'use client';

import { addToast, Button, Checkbox, Input, Select, SelectItem } from '@heroui/react';
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { Database, Download } from 'lucide-react';
import Link from 'next/link';
import * as React from 'react';

import { ConfidenceTrack } from '@/components/ui/confidence-track';
import { EmptyState } from '@/components/ui/empty-state';
import { ErrorState } from '@/components/ui/error-state';
import { KindBadge } from '@/components/ui/kind-badge';
import { PageHeader } from '@/components/ui/page-header';
import { PaginationFooter } from '@/components/ui/pagination-footer';
import { StatusPill } from '@/components/ui/status-pill';
import { TimeStamp } from '@/components/ui/time-stamp';
import { useDebouncedValue } from '@/hooks/use-debounced-value';
import { useUrlFilters } from '@/hooks/use-url-filters';
import { fetchMe, hasCapability, type MeResponse } from '@/lib/auth';
import {
  downloadMemoryExport,
  listInspectionMemories,
  type InspectionMemory,
  type InspectionMemoryOrdering,
} from '@/lib/console-api';
import { resolveKind } from '@/lib/design';
import { useProjectStore } from '@/lib/project-store';
import { useTeamStore } from '@/lib/team-store';

const PAGE_SIZE = 25;

const KIND_OPTIONS: { key: string; label: string }[] = [
  { key: 'all', label: 'All kinds' },
  { key: 'decision', label: 'Decisions' },
  { key: 'convention', label: 'Conventions' },
  { key: 'gotcha', label: 'Gotchas' },
  { key: 'architecture', label: 'Architecture' },
  { key: 'incident', label: 'Incidents' },
  { key: 'digest', label: 'Digests' },
];

const STATUS_OPTIONS: { key: string; label: string }[] = [
  { key: 'approved', label: 'Approved' },
  { key: 'archived', label: 'Archived' },
  { key: 'refuted', label: 'Refuted' },
];

const ORDERING_OPTIONS: { key: InspectionMemoryOrdering; label: string }[] = [
  { key: '-created_at', label: 'Newest first' },
  { key: 'created_at', label: 'Oldest first' },
];

const MEMORIES_FILTER_DEFAULTS = {
  search: '',
  kind: 'all',
  status: 'approved',
  ordering: '-created_at' as InspectionMemoryOrdering,
  page: 1,
};

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

function MemoryCard({ memory }: { memory: InspectionMemory }) {
  const kind = resolveKind(memory.kind ?? (memory.metadata?.kind as string | undefined));
  const source =
    memory.file_paths?.[0] ??
    (typeof memory.metadata?.source === 'string' ? memory.metadata.source : null) ??
    '—';
  const project = memory.project_name ?? memory.project_slug ?? memory.project_id;
  const pct = memory.confidence_percent ?? confidencePct(memory.confidence);
  const showRefuted = memory.refuted && memory.status !== 'refuted';

  return (
    <Link
      href={`/memories/${memory.id}`}
      className='surface-card block px-[22px] py-[19px] transition-all duration-150 hover:-translate-y-px hover:border-divider-strong hover:bg-content2'
    >
      <div className='flex items-center justify-between gap-3'>
        <div className='flex min-w-0 items-center gap-2.5'>
          <KindBadge kind={kind} />
          <span
            title={source}
            className='truncate font-mono text-[12px] text-default-400'
          >
            {source}
          </span>
        </div>
        <TimeStamp
          value={memory.updated_at ?? memory.created_at}
          className='shrink-0 text-[12px] text-default-400'
        />
      </div>

      <h3
        title={memory.title || undefined}
        className='mt-3 line-clamp-2 text-[16px] font-semibold leading-[1.3] tracking-[-0.01em] text-foreground'
      >
        {memory.title || '(untitled)'}
      </h3>

      {memory.body && (
        <p
          title={memory.body}
          className='mt-1.5 line-clamp-2 max-w-[74ch] text-[13.5px] leading-relaxed text-default-500'
        >
          {memory.body}
        </p>
      )}

      <div className='mt-4 flex items-center justify-between gap-3'>
        <div className='flex min-w-0 flex-wrap items-center gap-2 text-[12px] text-default-500'>
          <StatusPill status={memory.status} />
          {memory.stale && <StatusPill tone='warning' status='stale' label='Stale' />}
          {showRefuted && <StatusPill tone='danger' status='refuted' label='Refuted' />}
          <span className='min-w-0 truncate' title={project}>
            {project}
          </span>
        </div>
        {pct !== null && (
          <div className='flex shrink-0 items-center gap-2.5'>
            <span className='tnum font-mono text-[12px] text-default-400'>
              {pct}% conf
            </span>
            <ConfidenceTrack value={pct} />
          </div>
        )}
      </div>
    </Link>
  );
}

export default function MemoriesPage() {
  const activeProjectId = useProjectStore((s) => s.activeProjectId);
  const activeTeamId = useTeamStore((s) => s.activeTeamId);

  const meQuery = useQuery<MeResponse>({
    queryKey: ['auth', 'me'],
    queryFn: fetchMe,
  });
  const canExportAllStatuses = hasCapability(
    meQuery.data?.capabilities ?? [],
    'memories:admin',
  );

  const [exportAllStatuses, setExportAllStatuses] = React.useState(false);
  const [isExporting, setIsExporting] = React.useState(false);

  const handleExport = React.useCallback(async () => {
    if (!activeProjectId) {
      return;
    }

    setIsExporting(true);

    try {
      await downloadMemoryExport({
        projectId: activeProjectId,
        teamId: activeTeamId,
        allStatuses: canExportAllStatuses && exportAllStatuses,
      });
      addToast({ title: 'Export started', color: 'success' });
    } catch {
      addToast({ title: 'Export failed', color: 'danger' });
    } finally {
      setIsExporting(false);
    }
  }, [activeProjectId, activeTeamId, canExportAllStatuses, exportAllStatuses]);

  const exportActions = (
    <div className='flex items-center gap-3'>
      {canExportAllStatuses && (
        <Checkbox
          size='sm'
          isSelected={exportAllStatuses}
          onValueChange={setExportAllStatuses}
        >
          All statuses
        </Checkbox>
      )}
      <Button
        size='sm'
        variant='bordered'
        startContent={<Download className='h-4 w-4' />}
        isLoading={isExporting}
        onPress={handleExport}
      >
        Export
      </Button>
    </div>
  );

  const [filters, setFilters] = useUrlFilters(MEMORIES_FILTER_DEFAULTS);
  const [searchInput, setSearchInput] = React.useState(filters.search);
  const debouncedSearch = useDebouncedValue(searchInput, 300);

  React.useEffect(() => {
    setSearchInput(filters.search);
  }, [filters.search]);

  React.useEffect(() => {
    if (debouncedSearch !== filters.search) {
      setFilters({ search: debouncedSearch, page: 1 });
    }
  }, [debouncedSearch, filters.search, setFilters]);

  const page = Math.max(1, filters.page);

  const query = useQuery({
    queryKey: [
      'inspection',
      'memories',
      'list',
      activeProjectId,
      activeTeamId,
      filters.search,
      filters.kind,
      filters.status,
      filters.ordering,
      page,
    ],
    enabled: Boolean(activeProjectId),
    placeholderData: keepPreviousData,
    queryFn: () =>
      listInspectionMemories({
        projectId: activeProjectId as string,
        teamId: activeTeamId,
        search: filters.search || undefined,
        status: filters.status,
        kind: filters.kind === 'all' ? undefined : filters.kind,
        ordering: filters.ordering,
        limit: PAGE_SIZE,
        offset: (page - 1) * PAGE_SIZE,
      }),
  });

  const items = query.data?.items ?? [];
  const total = query.data?.count ?? 0;
  const hasFilters =
    Boolean(filters.search) ||
    filters.kind !== MEMORIES_FILTER_DEFAULTS.kind ||
    filters.status !== MEMORIES_FILTER_DEFAULTS.status;

  if (!activeProjectId) {
    return (
      <section className='space-y-6'>
        <PageHeader
          title='Memories'
          subtitle='Engineering knowledge captured by your agents, ready to inject.'
        />
        <EmptyState
          title='Select a project'
          description='Choose a project from the switcher above to view its captured memories.'
          icon={<Database className='h-6 w-6' />}
        />
      </section>
    );
  }

  return (
    <section className='space-y-6'>
      <PageHeader
        title='Memories'
        subtitle='Engineering knowledge captured by your agents, ready to inject.'
        actions={exportActions}
      />

      <div className='surface-card p-4'>
        <div className='grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-4'>
          <Input
            label='Search'
            labelPlacement='outside'
            placeholder='Title or body…'
            value={searchInput}
            onValueChange={setSearchInput}
            variant='bordered'
            size='sm'
            isClearable
            onClear={() => setSearchInput('')}
          />
          <Select
            label='Kind'
            labelPlacement='outside'
            variant='bordered'
            size='sm'
            disallowEmptySelection
            selectedKeys={new Set([filters.kind])}
            onSelectionChange={(keys) => {
              const next = Array.from(keys)[0];

              if (typeof next === 'string') {
                setFilters({ kind: next, page: 1 });
              }
            }}
          >
            {KIND_OPTIONS.map((option) => (
              <SelectItem key={option.key}>{option.label}</SelectItem>
            ))}
          </Select>
          <Select
            label='Status'
            labelPlacement='outside'
            variant='bordered'
            size='sm'
            disallowEmptySelection
            selectedKeys={new Set([filters.status])}
            onSelectionChange={(keys) => {
              const next = Array.from(keys)[0];

              if (typeof next === 'string') {
                setFilters({ status: next, page: 1 });
              }
            }}
          >
            {STATUS_OPTIONS.map((option) => (
              <SelectItem key={option.key}>{option.label}</SelectItem>
            ))}
          </Select>
          <Select
            label='Sort'
            labelPlacement='outside'
            variant='bordered'
            size='sm'
            disallowEmptySelection
            selectedKeys={new Set([filters.ordering])}
            onSelectionChange={(keys) => {
              const next = Array.from(keys)[0];

              if (typeof next === 'string') {
                setFilters({ ordering: next as InspectionMemoryOrdering, page: 1 });
              }
            }}
          >
            {ORDERING_OPTIONS.map((option) => (
              <SelectItem key={option.key}>{option.label}</SelectItem>
            ))}
          </Select>
        </div>
      </div>

      {query.isLoading ? (
        <div className='space-y-3'>
          {Array.from({ length: 4 }).map((_, index) => (
            <div
              key={index}
              className='surface-card h-[150px] animate-pulse bg-content1'
            />
          ))}
        </div>
      ) : query.isError && !query.data ? (
        <ErrorState
          message={
            query.error instanceof Error
              ? query.error.message
              : 'Failed to load memories.'
          }
          onRetry={() => query.refetch()}
        />
      ) : items.length === 0 ? (
        <EmptyState
          title={hasFilters ? 'No matching memories' : 'No memories yet'}
          description={
            hasFilters
              ? 'No memories match the current filters. Try a different search term, kind, or status.'
              : 'Memories captured by your agents for this project will appear here.'
          }
          icon={<Database className='h-6 w-6' />}
        />
      ) : (
        <div className='space-y-3'>
          <div className='space-y-3'>
            {items.map((memory) => (
              <MemoryCard key={memory.id} memory={memory} />
            ))}
          </div>
          <PaginationFooter
            page={page}
            pageSize={PAGE_SIZE}
            total={total}
            noun='result'
            onPageChange={(next) => setFilters({ page: next })}
            isDisabled={query.isFetching}
          />
        </div>
      )}
    </section>
  );
}
