'use client';

import {
  Button,
  Input,
  Modal,
  ModalBody,
  ModalContent,
  ModalHeader,
} from '@heroui/react';
import { useQuery } from '@tanstack/react-query';
import { AlertTriangle, Eye, Filter } from 'lucide-react';
import * as React from 'react';

import { CapabilityGate } from '@/components/ui/capability-gate';
import { EmptyState } from '@/components/ui/empty-state';
import { PageHeader } from '@/components/ui/page-header';
import { apiClient, fetchMe, type MeResponse } from '@/lib/auth';
import { formatRelativeTime } from '@/lib/design';
import { useProjectStore } from '@/lib/project-store';
import { useTeamStore } from '@/lib/team-store';

const LIMIT = 20;

type ObservationItem = {
  observation_id: string;
  session_id: string | null;
  observation_type: string;
  title: string;
  body: string;
  files_read: string[] | null;
  files_modified: string[] | null;
  observed_at: string | null;
};

type ObservationsListResponse = {
  items: ObservationItem[];
  warnings: string[];
  request_id: string;
};

type ObservationDetailResponse = ObservationItem & {
  request_id: string;
};

type Filters = {
  observation_type: string;
  session_id: string;
  since: string;
  until: string;
};

const EMPTY_FILTERS: Filters = {
  observation_type: '',
  session_id: '',
  since: '',
  until: '',
};

const GRID =
  'grid grid-cols-[minmax(0,0.7fr)_minmax(0,1.2fr)_minmax(0,2fr)_auto] items-center gap-4';

function TypePill({ type }: { type: string }) {
  return (
    <span className='inline-flex max-w-full items-center truncate rounded-[7px] bg-content3 px-2.5 py-1 font-mono text-[11px] font-medium text-default-500'>
      {type}
    </span>
  );
}

function ObservationRow({
  observation,
  onClick,
}: {
  observation: ObservationItem;
  onClick: () => void;
}) {
  return (
    <div
      role='button'
      tabIndex={0}
      className={`${GRID} cursor-pointer border-b border-divider px-5 py-3.5 transition-colors last:border-b-0 hover:bg-content2/60 focus:bg-content2/60 focus:outline-none`}
      onClick={onClick}
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          onClick();
        }
      }}
    >
      <div className='min-w-0'>
        <TypePill type={observation.observation_type} />
      </div>
      <div className='min-w-0'>
        <div className='truncate text-[13px] text-foreground'>
          {observation.title || '(untitled)'}
        </div>
        {observation.session_id && (
          <div className='mt-0.5 truncate font-mono text-[10.5px] text-default-400'>
            {observation.session_id}
          </div>
        )}
      </div>
      <div className='min-w-0 truncate text-[12.5px] text-default-500'>
        {observation.body || '—'}
      </div>
      <div className='flex items-center justify-end'>
        <span className='tnum whitespace-nowrap font-mono text-[11.5px] text-default-400'>
          {formatRelativeTime(observation.observed_at)}
        </span>
      </div>
    </div>
  );
}

function ObservationsTable({
  items,
  onRowClick,
}: {
  items: ObservationItem[];
  onRowClick: (id: string) => void;
}) {
  return (
    <div className='surface-card overflow-hidden'>
      <div
        className={`${GRID} border-b border-divider px-5 py-3 text-[10.5px] font-semibold uppercase tracking-[0.1em] text-default-400`}
      >
        <span>Type</span>
        <span>Title / Session</span>
        <span>Body</span>
        <span className='text-right'>Observed</span>
      </div>
      {items.map((obs) => (
        <ObservationRow
          key={obs.observation_id}
          observation={obs}
          onClick={() => onRowClick(obs.observation_id)}
        />
      ))}
    </div>
  );
}

function TableSkeleton() {
  return (
    <div className='surface-card overflow-hidden'>
      <div
        className={`${GRID} border-b border-divider px-5 py-3 text-[10.5px] font-semibold uppercase tracking-[0.1em] text-default-400`}
      >
        <span>Type</span>
        <span>Title / Session</span>
        <span>Body</span>
        <span className='text-right'>Observed</span>
      </div>
      {Array.from({ length: 5 }).map((_, index) => (
        <div
          key={index}
          className={`${GRID} border-b border-divider px-5 py-3.5 last:border-b-0`}
        >
          <span className='h-5 w-20 animate-pulse rounded-[7px] bg-content2' />
          <span className='h-3.5 w-32 animate-pulse rounded-medium bg-content2' />
          <span className='h-3.5 w-48 animate-pulse rounded-medium bg-content2' />
          <span className='ml-auto h-3.5 w-14 animate-pulse rounded-medium bg-content2' />
        </div>
      ))}
    </div>
  );
}

function DetailField({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className='min-w-0 space-y-1.5'>
      <p className='text-[10px] font-semibold uppercase tracking-[0.12em] text-default-400'>
        {label}
      </p>
      {children}
    </div>
  );
}

function DetailModal({
  observationId,
  projectId,
  teamId,
  onClose,
}: {
  observationId: string | null;
  projectId: string;
  teamId: string | null;
  onClose: () => void;
}) {
  const detailQuery = useQuery<ObservationDetailResponse>({
    queryKey: ['observations', 'detail', observationId, projectId, teamId],
    enabled: Boolean(observationId),
    queryFn: async () => {
      const client = apiClient();
      const params: Record<string, string> = { project_id: projectId };

      if (teamId) {
        params.team_id = teamId;
      }

      const response = await client.get<ObservationDetailResponse>(
        `/v1/observations/${observationId}`,
        { params },
      );

      return response.data;
    },
  });

  const obs = detailQuery.data;

  return (
    <Modal
      isOpen={Boolean(observationId)}
      onClose={onClose}
      placement='center'
      size='2xl'
    >
      <ModalContent>
        {() => (
          <>
            <ModalHeader className='text-foreground'>Observation detail</ModalHeader>
            <ModalBody className='pb-6'>
              {detailQuery.isLoading && (
                <div className='space-y-3'>
                  {Array.from({ length: 4 }).map((_, i) => (
                    <span
                      key={i}
                      className='block h-3.5 w-full animate-pulse rounded-medium bg-content2'
                    />
                  ))}
                </div>
              )}
              {detailQuery.isError && (
                <div className='flex items-start gap-3 rounded-[14px] border border-danger/30 bg-danger/5 px-4 py-3.5'>
                  <AlertTriangle className='mt-0.5 h-4 w-4 shrink-0 text-danger' />
                  <p className='text-[13px] text-danger'>
                    {detailQuery.error instanceof Error
                      ? detailQuery.error.message
                      : 'Failed to load observation.'}
                  </p>
                </div>
              )}
              {obs && (
                <div className='space-y-5'>
                  <div className='grid grid-cols-2 gap-x-6 gap-y-4 sm:grid-cols-3'>
                    <DetailField label='Type'>
                      <TypePill type={obs.observation_type} />
                    </DetailField>
                    <DetailField label='Observed'>
                      <span className='font-mono text-[12.5px] text-default-700'>
                        {formatRelativeTime(obs.observed_at)}
                      </span>
                    </DetailField>
                    {obs.session_id && (
                      <DetailField label='Session ID'>
                        <span className='break-all font-mono text-[11.5px] text-default-500'>
                          {obs.session_id}
                        </span>
                      </DetailField>
                    )}
                  </div>

                  {obs.title && (
                    <DetailField label='Title'>
                      <p className='text-[13.5px] font-semibold text-foreground'>
                        {obs.title}
                      </p>
                    </DetailField>
                  )}

                  <DetailField label='Body'>
                    <pre className='max-h-48 overflow-y-auto whitespace-pre-wrap rounded-[12px] bg-content2 px-4 py-3.5 font-mono text-[11.5px] leading-relaxed text-default-700'>
                      {obs.body || '(empty)'}
                    </pre>
                  </DetailField>

                  {obs.files_read && obs.files_read.length > 0 && (
                    <DetailField label={`Files read (${obs.files_read.length})`}>
                      <ul className='space-y-1'>
                        {obs.files_read.map((f) => (
                          <li key={f} className='truncate font-mono text-[11.5px] text-default-500'>
                            {f}
                          </li>
                        ))}
                      </ul>
                    </DetailField>
                  )}

                  {obs.files_modified && obs.files_modified.length > 0 && (
                    <DetailField label={`Files modified (${obs.files_modified.length})`}>
                      <ul className='space-y-1'>
                        {obs.files_modified.map((f) => (
                          <li key={f} className='truncate font-mono text-[11.5px] text-warning'>
                            {f}
                          </li>
                        ))}
                      </ul>
                    </DetailField>
                  )}
                </div>
              )}
            </ModalBody>
          </>
        )}
      </ModalContent>
    </Modal>
  );
}

export default function ObservationsPage() {
  const activeProjectId = useProjectStore((s) => s.activeProjectId);
  const activeTeamId = useTeamStore((s) => s.activeTeamId);

  const meQuery = useQuery<MeResponse>({
    queryKey: ['auth', 'me'],
    queryFn: fetchMe,
  });

  const capabilities = React.useMemo(
    () => meQuery.data?.capabilities ?? [],
    [meQuery.data?.capabilities],
  );

  const [draft, setDraft] = React.useState<Filters>(EMPTY_FILTERS);
  const [applied, setApplied] = React.useState<Filters>(EMPTY_FILTERS);
  const [items, setItems] = React.useState<ObservationItem[]>([]);
  const [offset, setOffset] = React.useState(0);
  const [hasMore, setHasMore] = React.useState(false);
  const [isLoading, setIsLoading] = React.useState(false);
  const [isLoadingMore, setIsLoadingMore] = React.useState(false);
  const [fetchError, setFetchError] = React.useState<string | null>(null);
  const [selectedId, setSelectedId] = React.useState<string | null>(null);

  async function fetchPage(
    currentOffset: number,
    filters: Filters,
    append: boolean,
  ) {
    if (!activeProjectId) {

      return;
    }

    if (append) {
      setIsLoadingMore(true);
    } else {
      setIsLoading(true);
    }

    setFetchError(null);

    try {
      const client = apiClient();
      const params: Record<string, string> = {
        project_id: activeProjectId,
        limit: String(LIMIT),
        offset: String(currentOffset),
      };

      if (activeTeamId) params.team_id = activeTeamId;
      if (filters.observation_type) params.observation_type = filters.observation_type;
      if (filters.session_id) params.session_id = filters.session_id;
      if (filters.since) params.since = filters.since;
      if (filters.until) params.until = filters.until;

      const response = await client.get<ObservationsListResponse>(
        '/v1/observations/',
        { params },
      );
      const newItems = response.data.items;

      if (append) {
        setItems((prev) => [...prev, ...newItems]);
      } else {
        setItems(newItems);
      }

      setHasMore(newItems.length === LIMIT);
      setOffset(currentOffset + newItems.length);
    } catch (err) {
      setFetchError(
        err instanceof Error ? err.message : 'Failed to load observations.',
      );
    } finally {
      setIsLoading(false);
      setIsLoadingMore(false);
    }
  }

  React.useEffect(() => {
    if (!activeProjectId) {

      return;
    }

    setItems([]);
    setOffset(0);
    setHasMore(false);
    void fetchPage(0, applied, false);
  }, [activeProjectId, activeTeamId, applied]);

  function handleFilterSubmit(e: React.FormEvent) {
    e.preventDefault();
    setApplied({ ...draft });
  }

  if (!activeProjectId) {
    return (
      <section className='space-y-6'>
        <PageHeader
          title='Observations'
          subtitle='Raw agent observations captured for the active project.'
        />
        <EmptyState
          title='No project selected'
          description='Select a project to view its observations.'
          icon={<Eye className='h-6 w-6' />}
        />
      </section>
    );
  }

  return (
    <CapabilityGate capabilities={capabilities} required='observations:read'>
      <section className='space-y-6'>
        <PageHeader
          title='Observations'
          subtitle='Raw agent observations captured for the active project.'
        />

        <form
          onSubmit={handleFilterSubmit}
          className='surface-card flex flex-wrap items-end gap-3 p-4'
        >
          <div className='flex items-center gap-2 self-start pt-[26px] text-[11px] font-semibold uppercase tracking-[0.1em] text-default-400'>
            <Filter className='h-3.5 w-3.5' />
            Filters
          </div>
          <Input
            label='Type'
            labelPlacement='outside'
            placeholder='e.g. file_read'
            size='sm'
            className='max-w-[180px]'
            value={draft.observation_type}
            onValueChange={(v) => setDraft((p) => ({ ...p, observation_type: v }))}
            classNames={{ input: 'font-mono text-xs' }}
          />
          <Input
            label='Session ID'
            labelPlacement='outside'
            placeholder='session uuid'
            size='sm'
            className='max-w-[220px]'
            value={draft.session_id}
            onValueChange={(v) => setDraft((p) => ({ ...p, session_id: v }))}
            classNames={{ input: 'font-mono text-xs' }}
          />
          <Input
            label='Since'
            labelPlacement='outside'
            placeholder='2024-01-01T00:00:00Z'
            size='sm'
            className='max-w-[210px]'
            value={draft.since}
            onValueChange={(v) => setDraft((p) => ({ ...p, since: v }))}
            classNames={{ input: 'font-mono text-xs' }}
          />
          <Input
            label='Until'
            labelPlacement='outside'
            placeholder='2024-12-31T23:59:59Z'
            size='sm'
            className='max-w-[210px]'
            value={draft.until}
            onValueChange={(v) => setDraft((p) => ({ ...p, until: v }))}
            classNames={{ input: 'font-mono text-xs' }}
          />
          <Button type='submit' color='primary' size='sm' className='mb-0.5'>
            Apply
          </Button>
        </form>

        {isLoading && <TableSkeleton />}

        {fetchError && !isLoading && (
          <div className='flex items-start gap-3 rounded-[16px] border border-danger/30 bg-danger/5 px-5 py-4'>
            <AlertTriangle className='mt-0.5 h-5 w-5 shrink-0 text-danger' />
            <p className='text-[13px] leading-relaxed text-danger'>{fetchError}</p>
          </div>
        )}

        {!isLoading && !fetchError && items.length === 0 && (
          <EmptyState
            title='No observations'
            description='No observations have been recorded for this project yet.'
            icon={<Eye className='h-6 w-6' />}
          />
        )}

        {!isLoading && items.length > 0 && (
          <div className='space-y-3'>
            <ObservationsTable items={items} onRowClick={setSelectedId} />
            <div className='flex items-center justify-between'>
              <p className='tnum text-[12px] text-default-400'>
                Showing {items.length} observation{items.length === 1 ? '' : 's'}.
              </p>
              {hasMore && (
                <Button
                  size='sm'
                  variant='flat'
                  onPress={() => void fetchPage(offset, applied, true)}
                  isLoading={isLoadingMore}
                >
                  Load more
                </Button>
              )}
            </div>
          </div>
        )}

        <DetailModal
          observationId={selectedId}
          projectId={activeProjectId}
          teamId={activeTeamId}
          onClose={() => setSelectedId(null)}
        />
      </section>
    </CapabilityGate>
  );
}
