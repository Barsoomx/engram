'use client';

import { Button, Input, Modal, ModalBody, ModalContent, ModalHeader, Select, SelectItem } from '@heroui/react';
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { Eye } from 'lucide-react';
import * as React from 'react';

import { CapabilityGate } from '@/components/ui/capability-gate';
import { EmptyState } from '@/components/ui/empty-state';
import { ErrorState } from '@/components/ui/error-state';
import { PageHeader } from '@/components/ui/page-header';
import { ResponsiveTable } from '@/components/ui/responsive-table';
import { TimeStamp } from '@/components/ui/time-stamp';
import { useUrlFilters } from '@/hooks/use-url-filters';
import { apiClient, fetchMe, type MeResponse } from '@/lib/auth';
import { endOfDayExclusiveIso, startOfDayIso } from '@/lib/format-time';
import { useProjectStore } from '@/lib/project-store';
import { useTeamStore } from '@/lib/team-store';

const PAGE_SIZE = 20;

type ObservationItem = {
  observation_id: string;
  session_id: string | null;
  observation_type: string;
  title: string;
  subtitle?: string | null;
  body: string;
  facts?: unknown;
  narrative?: string | null;
  concepts?: unknown;
  files_read: string[] | null;
  files_modified: string[] | null;
  observed_at: string | null;
};

type ObservationsListResponse = {
  items: ObservationItem[];
  warnings: string[];
  request_id: string;
};

const OBSERVATIONS_FILTER_DEFAULTS = {
  observation_type: '',
  session_id: '',
  correlation_id: '',
  since: '',
  until: '',
  page: 1,
  selected: '',
};

function asStringList(value: unknown): string[] {
  if (!Array.isArray(value)) {
    return [];
  }

  return value.filter(
    (entry): entry is string => typeof entry === 'string' && entry.trim().length > 0,
  );
}

function TypePill({ type }: { type: string }) {
  return (
    <span className='inline-flex max-w-full items-center truncate rounded-[7px] bg-content3 px-2.5 py-1 font-mono text-[11px] font-medium text-default-500'>
      {type}
    </span>
  );
}

function ObservationsTable({
  items,
  onRowClick,
  onSessionClick,
}: {
  items: ObservationItem[];
  onRowClick: (id: string) => void;
  onSessionClick: (sessionId: string) => void;
}) {
  return (
    <div className='surface-card p-2'>
      <ResponsiveTable minWidth={720}>
        <thead>
          <tr className='border-b border-divider text-[10.5px] font-semibold uppercase tracking-[0.1em] text-default-400'>
            <th className='py-3 px-3 text-left font-medium'>Type</th>
            <th className='py-3 px-3 text-left font-medium'>Title / Session</th>
            <th className='py-3 px-3 text-left font-medium'>Body</th>
            <th className='py-3 px-3 text-right font-medium'>Observed</th>
          </tr>
        </thead>
        <tbody>
          {items.map((obs) => (
            <tr
              key={obs.observation_id}
              role='button'
              tabIndex={0}
              className='cursor-pointer border-b border-divider/60 transition-colors last:border-b-0 hover:bg-content2/60 focus:bg-content2/60 focus:outline-hidden'
              onClick={() => onRowClick(obs.observation_id)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' || event.key === ' ') {
                  event.preventDefault();
                  onRowClick(obs.observation_id);
                }
              }}
            >
              <td className='py-2.5 px-3'>
                <TypePill type={obs.observation_type} />
              </td>
              <td className='py-2.5 px-3'>
                <div className='min-w-0'>
                  <div className='truncate text-[13px] text-foreground' title={obs.title || undefined}>
                    {obs.title || '(untitled)'}
                  </div>
                  {obs.session_id && (
                    <button
                      type='button'
                      onClick={(event) => {
                        event.stopPropagation();
                        onSessionClick(obs.session_id as string);
                      }}
                      className='mt-0.5 block max-w-full truncate font-mono text-[10.5px] text-default-400 hover:text-primary-300'
                      title={`Filter by session ${obs.session_id}`}
                    >
                      {obs.session_id}
                    </button>
                  )}
                </div>
              </td>
              <td className='py-2.5 px-3'>
                <span
                  className='block min-w-0 truncate text-[12.5px] text-default-500'
                  title={obs.body || undefined}
                >
                  {obs.body || '—'}
                </span>
              </td>
              <td className='py-2.5 px-3 text-right'>
                <TimeStamp
                  value={obs.observed_at}
                  className='tnum whitespace-nowrap font-mono text-[11.5px] text-default-400'
                />
              </td>
            </tr>
          ))}
        </tbody>
      </ResponsiveTable>
    </div>
  );
}

function DetailField({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className='min-w-0 space-y-1.5'>
      <p className='text-[10px] font-semibold uppercase tracking-[0.12em] text-default-400'>{label}</p>
      {children}
    </div>
  );
}

function DetailModal({
  observation,
  onClose,
  onSessionClick,
}: {
  observation: ObservationItem | null;
  onClose: () => void;
  onSessionClick: (sessionId: string) => void;
}) {
  const facts = asStringList(observation?.facts);
  const concepts = asStringList(observation?.concepts);
  const filesRead = observation?.files_read ?? [];
  const filesModified = observation?.files_modified ?? [];

  return (
    <Modal isOpen={Boolean(observation)} onClose={onClose} placement='center' size='2xl'>
      <ModalContent>
        {() => (
          <>
            <ModalHeader className='text-foreground'>Observation detail</ModalHeader>
            <ModalBody className='pb-6'>
              {observation && (
                <div className='space-y-5'>
                  <div className='grid grid-cols-2 gap-x-6 gap-y-4 sm:grid-cols-3'>
                    <DetailField label='Type'>
                      <TypePill type={observation.observation_type} />
                    </DetailField>
                    <DetailField label='Observed'>
                      <TimeStamp
                        value={observation.observed_at}
                        relative={false}
                        className='font-mono text-[12.5px] text-default-700'
                      />
                    </DetailField>
                    {observation.session_id && (
                      <DetailField label='Session'>
                        <button
                          type='button'
                          onClick={() => onSessionClick(observation.session_id as string)}
                          className='break-all text-left font-mono text-[11.5px] text-primary-300 hover:underline'
                        >
                          {observation.session_id}
                        </button>
                      </DetailField>
                    )}
                  </div>

                  {observation.title && (
                    <DetailField label='Title'>
                      <p className='text-[13.5px] font-semibold text-foreground'>{observation.title}</p>
                    </DetailField>
                  )}

                  {observation.subtitle && (
                    <DetailField label='Subtitle'>
                      <p className='text-[13px] text-default-600'>{observation.subtitle}</p>
                    </DetailField>
                  )}

                  {observation.narrative && (
                    <DetailField label='Narrative'>
                      <p className='whitespace-pre-wrap text-[13px] leading-relaxed text-default-600'>
                        {observation.narrative}
                      </p>
                    </DetailField>
                  )}

                  {facts.length > 0 && (
                    <DetailField label={`Facts (${facts.length})`}>
                      <ul className='list-disc space-y-1 pl-4'>
                        {facts.map((fact, index) => (
                          <li key={index} className='text-[12.5px] text-default-600'>
                            {fact}
                          </li>
                        ))}
                      </ul>
                    </DetailField>
                  )}

                  {concepts.length > 0 && (
                    <DetailField label={`Concepts (${concepts.length})`}>
                      <div className='flex flex-wrap gap-1.5'>
                        {concepts.map((concept, index) => (
                          <span
                            key={index}
                            className='rounded-[7px] bg-content3 px-2 py-0.5 text-[11px] text-default-600'
                          >
                            {concept}
                          </span>
                        ))}
                      </div>
                    </DetailField>
                  )}

                  <DetailField label='Body'>
                    <pre className='max-h-48 overflow-y-auto whitespace-pre-wrap rounded-[12px] bg-content2 px-4 py-3.5 font-mono text-[11.5px] leading-relaxed text-default-700'>
                      {observation.body || '(empty)'}
                    </pre>
                  </DetailField>

                  {filesRead.length > 0 && (
                    <DetailField label={`Files read (${filesRead.length})`}>
                      <ul className='space-y-1'>
                        {filesRead.map((f) => (
                          <li
                            key={f}
                            className='truncate font-mono text-[11.5px] text-default-500'
                            title={f}
                          >
                            {f}
                          </li>
                        ))}
                      </ul>
                    </DetailField>
                  )}

                  {filesModified.length > 0 && (
                    <DetailField label={`Files modified (${filesModified.length})`}>
                      <ul className='space-y-1'>
                        {filesModified.map((f) => (
                          <li
                            key={f}
                            className='truncate font-mono text-[11.5px] text-warning'
                            title={f}
                          >
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

  const [filters, setFilters] = useUrlFilters(OBSERVATIONS_FILTER_DEFAULTS);
  const page = Math.max(1, filters.page);
  const [knownTypes, setKnownTypes] = React.useState<string[]>([]);

  const query = useQuery({
    queryKey: [
      'observations',
      activeProjectId,
      activeTeamId,
      filters.observation_type,
      filters.session_id,
      filters.correlation_id,
      filters.since,
      filters.until,
      page,
    ],
    enabled: Boolean(activeProjectId),
    placeholderData: keepPreviousData,
    queryFn: async () => {
      const params: Record<string, string> = {
        project_id: activeProjectId ?? '',
        limit: String(PAGE_SIZE),
        offset: String((page - 1) * PAGE_SIZE),
      };

      if (activeTeamId) params.team_id = activeTeamId;
      if (filters.observation_type) params.observation_type = filters.observation_type;
      if (filters.session_id) params.session_id = filters.session_id;
      if (filters.correlation_id) params.correlation_id = filters.correlation_id;

      const since = startOfDayIso(filters.since);
      const until = endOfDayExclusiveIso(filters.until);

      if (since) params.since = since;
      if (until) params.until = until;

      const response = await apiClient().get<ObservationsListResponse>('/v1/observations/', {
        params,
      });

      return response.data;
    },
  });

  const items = React.useMemo(() => query.data?.items ?? [], [query.data]);

  React.useEffect(() => {
    if (items.length === 0) {
      return;
    }

    setKnownTypes((prev) => {
      const set = new Set(prev);

      for (const item of items) {
        if (item.observation_type) {
          set.add(item.observation_type);
        }
      }

      const merged = Array.from(set).sort();

      return merged.length === prev.length ? prev : merged;
    });
  }, [items]);

  const typeOptions = React.useMemo(() => {
    const set = new Set(knownTypes);

    if (filters.observation_type) {
      set.add(filters.observation_type);
    }

    return Array.from(set).sort();
  }, [knownTypes, filters.observation_type]);

  const selectedObservation = filters.selected
    ? items.find((obs) => obs.observation_id === filters.selected) ?? null
    : null;

  const hasMore = items.length === PAGE_SIZE;
  const hasActiveFilters =
    Boolean(filters.observation_type) ||
    Boolean(filters.session_id) ||
    Boolean(filters.correlation_id) ||
    Boolean(filters.since) ||
    Boolean(filters.until);

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

        <div className='surface-card grid grid-cols-1 gap-3 p-4 sm:grid-cols-2 lg:grid-cols-4'>
          <Select
            label='Type'
            labelPlacement='outside'
            placeholder='All types'
            variant='bordered'
            size='sm'
            selectedKeys={
              filters.observation_type ? new Set([filters.observation_type]) : new Set<string>()
            }
            onSelectionChange={(keys) => {
              const next = Array.from(keys)[0];

              setFilters({ observation_type: typeof next === 'string' ? next : '', page: 1 });
            }}
          >
            {typeOptions.map((type) => (
              <SelectItem key={type}>{type}</SelectItem>
            ))}
          </Select>
          <Input
            label='Session ID'
            labelPlacement='outside'
            placeholder='session uuid'
            variant='bordered'
            size='sm'
            value={filters.session_id}
            onValueChange={(v) => setFilters({ session_id: v, page: 1 })}
            isClearable
            onClear={() => setFilters({ session_id: '', page: 1 })}
            classNames={{ input: 'font-mono text-xs' }}
          />
          <Input
            label='Correlation ID'
            labelPlacement='outside'
            placeholder='correlation id'
            variant='bordered'
            size='sm'
            value={filters.correlation_id}
            onValueChange={(v) => setFilters({ correlation_id: v, page: 1 })}
            isClearable
            onClear={() => setFilters({ correlation_id: '', page: 1 })}
            classNames={{ input: 'font-mono text-xs' }}
          />
          <div className='grid grid-cols-2 gap-2'>
            <Input
              label='Since'
              labelPlacement='outside'
              type='date'
              variant='bordered'
              size='sm'
              value={filters.since}
              onValueChange={(v) => setFilters({ since: v, page: 1 })}
            />
            <Input
              label='Until'
              labelPlacement='outside'
              type='date'
              variant='bordered'
              size='sm'
              value={filters.until}
              onValueChange={(v) => setFilters({ until: v, page: 1 })}
            />
          </div>
        </div>

        {query.isLoading ? (
          <div className='surface-card p-2'>
            <ResponsiveTable minWidth={720}>
              <thead>
                <tr className='border-b border-divider text-[10.5px] font-semibold uppercase tracking-[0.1em] text-default-400'>
                  {['Type', 'Title / Session', 'Body', 'Observed'].map((label) => (
                    <th key={label} className='py-3 px-3 text-left font-medium'>
                      {label}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {Array.from({ length: 6 }).map((_, index) => (
                  <tr key={index} className='border-b border-divider/60'>
                    {Array.from({ length: 4 }).map((__, cell) => (
                      <td key={cell} className='py-3 px-3'>
                        <span className='block h-3.5 w-full animate-pulse rounded-medium bg-content2' />
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </ResponsiveTable>
          </div>
        ) : query.isError && !query.data ? (
          <ErrorState
            message={
              query.error instanceof Error ? query.error.message : 'Failed to load observations.'
            }
            onRetry={() => query.refetch()}
          />
        ) : items.length === 0 ? (
          <EmptyState
            title={hasActiveFilters ? 'No matching observations' : 'No observations'}
            description={
              hasActiveFilters
                ? 'No observations match the current filters.'
                : 'No observations have been recorded for this project yet.'
            }
            icon={<Eye className='h-6 w-6' />}
          />
        ) : (
          <div className='space-y-3'>
            <ObservationsTable
              items={items}
              onRowClick={(id) => setFilters({ selected: id })}
              onSessionClick={(sessionId) => setFilters({ session_id: sessionId, page: 1 })}
            />
            <div className='flex items-center justify-between'>
              <p className='tnum text-[12px] text-default-400'>
                Showing {(page - 1) * PAGE_SIZE + 1}–{(page - 1) * PAGE_SIZE + items.length} on page{' '}
                {page}.
              </p>
              <div className='flex items-center gap-2'>
                <Button
                  size='sm'
                  variant='flat'
                  isDisabled={page <= 1 || query.isFetching}
                  onPress={() => setFilters({ page: page - 1 })}
                >
                  Previous
                </Button>
                <Button
                  size='sm'
                  variant='flat'
                  isDisabled={!hasMore || query.isFetching}
                  onPress={() => setFilters({ page: page + 1 })}
                >
                  Next
                </Button>
              </div>
            </div>
          </div>
        )}

        <DetailModal
          observation={selectedObservation}
          onClose={() => setFilters({ selected: '' })}
          onSessionClick={(sessionId) =>
            setFilters({ session_id: sessionId, selected: '', page: 1 })
          }
        />
      </section>
    </CapabilityGate>
  );
}
