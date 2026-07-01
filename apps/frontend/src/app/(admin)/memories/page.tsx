'use client';

import { useInfiniteQuery } from '@tanstack/react-query';
import { AlertTriangle, ChevronDown, Database, Search } from 'lucide-react';
import Link from 'next/link';
import * as React from 'react';

import { ConfidenceTrack } from '@/components/ui/confidence-track';
import { EmptyState } from '@/components/ui/empty-state';
import { KindBadge, KindDot } from '@/components/ui/kind-badge';
import { PageHeader } from '@/components/ui/page-header';
import { useProjects } from '@/hooks/use-projects';
import { apiClient } from '@/lib/auth';
import { formatRelativeTime, resolveKind, type MemoryKind } from '@/lib/design';
import { useOrgStore } from '@/lib/org-store';
import { useProjectStore } from '@/lib/project-store';
import { useTeamStore } from '@/lib/team-store';

const LIMIT = 50;

type MemoryMetadata = {
  kind?: string | null;
  source?: string | null;
  agent?: string | null;
  project?: string | null;
};

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
  confidence_percent?: number | null;
  stale: boolean;
  refuted: boolean;
  created_at: string | null;
  updated_at: string | null;
  metadata?: MemoryMetadata | null;
  kind?: string | null;
  tags?: string[];
  file_paths?: string[];
  captured_by?: unknown;
  project_name?: string;
  project_slug?: string;
  authorized_for_injection?: boolean;
};

type MemoriesResponse = {
  count: number;
  items: MemoryItem[];
};

type KindFilter = MemoryKind | 'all';

const KIND_FILTERS: { key: KindFilter; label: string }[] = [
  { key: 'all', label: 'All' },
  { key: 'decision', label: 'Decisions' },
  { key: 'convention', label: 'Conventions' },
  { key: 'gotcha', label: 'Gotchas' },
  { key: 'architecture', label: 'Architecture' },
];

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

function MemoryCard({
  memory,
  projectLabel,
}: {
  memory: MemoryItem;
  projectLabel: string;
}) {
  const kind = resolveKind(memory.kind ?? memory.metadata?.kind);
  const source = memory.file_paths?.[0] ?? memory.metadata?.source ?? '—';
  const agent = memory.metadata?.agent ?? null;
  const project = memory.project_name ?? memory.project_slug ?? memory.metadata?.project ?? projectLabel;
  const pct = memory.confidence_percent ?? confidencePct(memory.confidence);

  return (
    <Link
      href={`/memories/${memory.id}`}
      className='surface-card block px-[22px] py-[19px] transition-all duration-150 hover:-translate-y-px hover:border-divider-strong hover:bg-content2'
    >
      <div className='flex items-center justify-between gap-3'>
        <div className='flex min-w-0 items-center gap-2.5'>
          <KindBadge kind={kind} />
          <span className='truncate font-mono text-[12px] text-default-400'>
            {source}
          </span>
        </div>
        <span className='shrink-0 text-[12px] text-default-400'>
          {formatRelativeTime(memory.updated_at ?? memory.created_at)}
        </span>
      </div>

      <h3 className='mt-3 text-[16px] font-semibold leading-[1.3] tracking-[-0.01em] text-foreground'>
        {memory.title || '(untitled)'}
      </h3>

      {memory.body && (
        <p className='mt-1.5 line-clamp-2 max-w-[74ch] text-[13.5px] leading-relaxed text-default-500'>
          {memory.body}
        </p>
      )}

      <div className='mt-4 flex items-center justify-between gap-3'>
        <div className='flex min-w-0 items-center gap-2 text-[12px] text-default-500'>
          <KindDot kind={kind} size={8} />
          <span className='truncate'>{project}</span>
          {agent && (
            <>
              <span className='text-default-400'>·</span>
              <span className='truncate'>{agent}</span>
            </>
          )}
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
  const activeOrgId = useOrgStore((s) => s.activeOrgId);
  const activeProjectId = useProjectStore((s) => s.activeProjectId);
  const activeTeamId = useTeamStore((s) => s.activeTeamId);

  const [search, setSearch] = React.useState('');
  const [kindFilter, setKindFilter] = React.useState<KindFilter>('all');

  const projectsQuery = useProjects(activeOrgId, { pageSize: 100 });

  const query = useInfiniteQuery({
    queryKey: ['inspection', 'memories', activeProjectId, activeTeamId],
    enabled: Boolean(activeProjectId),
    initialPageParam: 0,
    queryFn: async ({ pageParam }) => {
      const params: Record<string, string> = {
        project_id: activeProjectId ?? '',
        limit: String(LIMIT),
        offset: String(pageParam),
      };

      if (activeTeamId) {
        params.team_id = activeTeamId;
      }

      const response = await apiClient().get<MemoriesResponse>(
        '/v1/inspection/memories',
        { params },
      );

      return response.data;
    },
    getNextPageParam: (lastPage, allPages) => {
      const loaded = allPages.reduce((sum, page) => sum + page.items.length, 0);

      return loaded < lastPage.count ? loaded : undefined;
    },
  });

  const allItems = React.useMemo(
    () => query.data?.pages.flatMap((page) => page.items) ?? [],
    [query.data],
  );

  const totalCount = query.data?.pages[0]?.count ?? 0;
  const hasMore = Boolean(query.hasNextPage);
  const isLoadingMore = query.isFetchingNextPage;
  const loadMoreError =
    query.isError && query.data
      ? query.error instanceof Error
        ? query.error.message
        : 'Failed to load more memories.'
      : null;

  const projectLabel = React.useMemo(() => {
    const project = projectsQuery.data?.results.find(
      (p) => p.id === activeProjectId,
    );

    return project?.slug ?? '—';
  }, [projectsQuery.data, activeProjectId]);

  const filtered = React.useMemo(() => {
    const q = search.trim().toLowerCase();

    return allItems.filter((memory) => {
      if (kindFilter !== 'all' && resolveKind(memory.kind ?? memory.metadata?.kind) !== kindFilter) {
        return false;
      }

      if (!q) {
        return true;
      }

      const haystack = [
        memory.title,
        memory.body,
        memory.metadata?.source ?? '',
        memory.metadata?.agent ?? '',
      ]
        .join(' ')
        .toLowerCase();

      return haystack.includes(q);
    });
  }, [allItems, search, kindFilter]);

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
      />

      <div className='space-y-3'>
        <div className='flex items-center gap-2.5 rounded-[12px] border border-divider-strong bg-content1 px-3.5 transition-colors focus-within:border-primary'>
          <Search size={17} strokeWidth={1.8} className='shrink-0 text-default-400' />
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder='Search memories, tags, files…'
            className='h-12 w-full bg-transparent text-[14px] text-foreground outline-none placeholder:text-default-400'
          />
        </div>
        <div className='flex flex-wrap gap-2'>
          {KIND_FILTERS.map((filter) => {
            const active = kindFilter === filter.key;

            return (
              <button
                key={filter.key}
                type='button'
                onClick={() => setKindFilter(filter.key)}
                className={
                  active
                    ? 'rounded-[9px] bg-foreground px-3.5 py-2 text-[13px] font-medium text-background transition-colors'
                    : 'rounded-[9px] border border-divider-strong px-3.5 py-2 text-[13px] font-medium text-default-500 transition-colors hover:text-foreground'
                }
              >
                {filter.label}
              </button>
            );
          })}
        </div>
      </div>

      {query.isLoading && (
        <div className='space-y-3'>
          {Array.from({ length: 4 }).map((_, index) => (
            <div
              key={index}
              className='surface-card h-[150px] animate-pulse bg-content1'
            />
          ))}
        </div>
      )}

      {query.isError && !query.data && (
        <div className='flex items-start gap-3 rounded-[16px] border border-danger/30 bg-danger/5 px-5 py-4'>
          <AlertTriangle className='mt-0.5 h-5 w-5 shrink-0 text-danger' />
          <p className='text-[13px] leading-relaxed text-danger'>
            {query.error instanceof Error ? query.error.message : 'Failed to load memories.'}
          </p>
        </div>
      )}

      {query.data &&
        (filtered.length > 0 ? (
          <div className='space-y-3'>
            {filtered.map((memory) => (
              <MemoryCard
                key={memory.id}
                memory={memory}
                projectLabel={projectLabel}
              />
            ))}

            {loadMoreError && (
              <div className='flex items-start gap-3 rounded-[16px] border border-danger/30 bg-danger/5 px-5 py-4'>
                <AlertTriangle className='mt-0.5 h-5 w-5 shrink-0 text-danger' />
                <p className='text-[13px] leading-relaxed text-danger'>{loadMoreError}</p>
              </div>
            )}

            {hasMore && (
              <button
                type='button'
                onClick={() => void query.fetchNextPage()}
                disabled={isLoadingMore}
                className='flex w-full items-center justify-center gap-2 rounded-[12px] border border-divider-strong bg-content1 py-3 text-[13.5px] font-medium text-default-600 transition-colors hover:bg-content2 disabled:cursor-wait disabled:opacity-60'
              >
                {isLoadingMore ? (
                  <span className='animate-pulse'>Loading…</span>
                ) : (
                  <>
                    <ChevronDown size={16} strokeWidth={1.8} />
                    Load more
                    <span className='ml-1 text-default-400'>
                      ({totalCount - allItems.length} remaining)
                    </span>
                  </>
                )}
              </button>
            )}

            {!hasMore && allItems.length > LIMIT && (
              <p className='tnum text-center text-[12px] text-default-400'>
                All {totalCount} memories loaded
              </p>
            )}
          </div>
        ) : (
          <EmptyState
            title={allItems.length === 0 ? 'No memories yet' : 'No matching memories'}
            description={
              allItems.length === 0
                ? 'Memories captured by your agents for this project will appear here.'
                : 'Try a different search term or kind filter.'
            }
            icon={<Database className='h-6 w-6' />}
          />
        ))}
    </section>
  );
}
