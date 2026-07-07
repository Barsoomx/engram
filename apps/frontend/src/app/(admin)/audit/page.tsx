'use client';

import { Button, Input, Select, SelectItem } from '@heroui/react';
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { ScrollText, X } from 'lucide-react';
import Link from 'next/link';
import * as React from 'react';

import { CapabilityGate } from '@/components/ui/capability-gate';
import { CopyableId } from '@/components/ui/copyable-id';
import { EmptyState } from '@/components/ui/empty-state';
import { ErrorState } from '@/components/ui/error-state';
import { PageHeader } from '@/components/ui/page-header';
import { PaginationFooter } from '@/components/ui/pagination-footer';
import { ResponsiveTable } from '@/components/ui/responsive-table';
import { TimeStamp } from '@/components/ui/time-stamp';
import { useProjects } from '@/hooks/use-projects';
import { useTeams } from '@/hooks/use-teams';
import { useUrlFilters } from '@/hooks/use-url-filters';
import {
  listAuditEvents,
  type AuditEvent,
  type AuditEventListParams,
} from '@/lib/admin-api';
import { fetchMe, type MeResponse } from '@/lib/auth';
import { auditResultColor } from '@/lib/design';
import { endOfDayExclusiveIso, startOfDayIso } from '@/lib/format-time';
import { useOrgStore } from '@/lib/org-store';
import { adminQueryKeys } from '@/lib/query-keys';

const PAGE_SIZE = 50;

const RESULT_OPTIONS: { key: string; label: string }[] = [
  { key: 'allowed', label: 'Allowed' },
  { key: 'denied', label: 'Denied' },
  { key: 'recorded', label: 'Recorded' },
  { key: 'error', label: 'Error' },
];

const AUDIT_FILTER_DEFAULTS = {
  event_type: '',
  result: '',
  actor_id: '',
  target_type: '',
  project_id: '',
  team_id: '',
  since: '',
  until: '',
  page: 1,
  event: '',
};

type AuditAction =
  | 'create'
  | 'delete'
  | 'change'
  | 'promote'
  | 'login'
  | 'secret'
  | 'neutral';

const ACTION_STYLES: Record<AuditAction, string> = {
  create: 'text-success bg-success/10',
  delete: 'text-danger bg-danger/10',
  change: 'text-info bg-info/10',
  promote: 'text-primary-300 bg-primary-soft',
  login: 'text-default-500 bg-content3',
  secret: 'text-warning bg-warning/10',
  neutral: 'text-default-500 bg-content3',
};

function resolveAction(eventType: string): AuditAction {
  const v = eventType.toLowerCase();

  if (v.includes('secret')) {
    return 'secret';
  }

  if (v.includes('delete') || v.includes('archive') || v.includes('revoke')) {
    return 'delete';
  }

  if (v.includes('create') || v.includes('issue')) {
    return 'create';
  }

  if (v.includes('promote')) {
    return 'promote';
  }

  if (v.includes('login')) {
    return 'login';
  }

  if (v.includes('change') || v.includes('update')) {
    return 'change';
  }

  return 'neutral';
}

function shortenId(value: string): string {
  if (value.length <= 12) {
    return value;
  }

  return `${value.slice(0, 8)}…`;
}

function ResultBadge({ result }: { result: string }) {
  const color = auditResultColor(result);

  return (
    <span className='inline-flex items-center gap-1.5'>
      <span
        className='inline-block h-1.5 w-1.5 rounded-full'
        style={{ backgroundColor: color }}
      />
      <span
        className='whitespace-nowrap text-[11.5px] font-medium capitalize'
        style={{ color }}
      >
        {result}
      </span>
    </span>
  );
}

function AuditRow({
  event,
  onSelect,
}: {
  event: AuditEvent;
  onSelect: (event: AuditEvent) => void;
}) {
  const action = resolveAction(event.event_type);

  return (
    <tr
      role='button'
      tabIndex={0}
      onClick={() => onSelect(event)}
      onKeyDown={(e) => e.key === 'Enter' && onSelect(event)}
      className='cursor-pointer border-b border-divider/50 transition-colors hover:bg-content2/60'
    >
      <td className='py-2.5 px-3'>
        <span
          className={`inline-flex max-w-full items-center truncate rounded-[7px] px-2 py-0.5 font-mono text-[11px] font-medium ${ACTION_STYLES[action]}`}
          title={event.event_type}
        >
          {event.event_type}
        </span>
      </td>
      <td className='py-2.5 px-3'>
        {event.target_display || event.target_type || event.target_id ? (
          <div className='min-w-0'>
            <div className='truncate text-[13px] text-default-700' title={event.target_display ?? event.target_id ?? ''}>
              {event.target_display ?? event.target_type ?? 'target'}
              {!event.target_display && event.target_id && (
                <span className='ml-1.5 font-mono text-[11.5px] text-default-400'>
                  {shortenId(event.target_id)}
                </span>
              )}
            </div>
            {event.capability && (
              <div className='truncate font-mono text-[11px] text-default-400'>
                {event.capability}
              </div>
            )}
          </div>
        ) : (
          <span className='text-[13px] text-default-400'>—</span>
        )}
      </td>
      <td className='py-2.5 px-3'>
        <div className='min-w-0 truncate text-[13px]' title={event.actor_display ?? event.actor_id}>
          {event.actor_display ? (
            <span className='text-default-600'>{event.actor_display}</span>
          ) : (
            <>
              <span className='text-default-500'>{event.actor_type}</span>
              {event.actor_id && (
                <span className='ml-1 font-mono text-[11.5px] text-default-400'>
                  {shortenId(event.actor_id)}
                </span>
              )}
            </>
          )}
        </div>
      </td>
      <td className='py-2.5 px-3'>
        <ResultBadge result={event.result} />
      </td>
      <td className='py-2.5 px-3 text-right'>
        <TimeStamp
          value={event.created_at}
          className='tnum whitespace-nowrap font-mono text-[11.5px] text-default-400'
        />
      </td>
    </tr>
  );
}

function AuditTable({
  items,
  onSelect,
}: {
  items: AuditEvent[];
  onSelect: (event: AuditEvent) => void;
}) {
  return (
    <div className='surface-card p-2'>
      <ResponsiveTable minWidth={760}>
        <thead>
          <tr className='border-b border-divider text-[10.5px] font-semibold uppercase tracking-[0.1em] text-default-400'>
            <th className='py-2 px-3 text-left font-medium'>Event</th>
            <th className='py-2 px-3 text-left font-medium'>Target</th>
            <th className='py-2 px-3 text-left font-medium'>Actor</th>
            <th className='py-2 px-3 text-left font-medium'>Result</th>
            <th className='py-2 px-3 text-right font-medium'>When</th>
          </tr>
        </thead>
        <tbody>
          {items.map((event) => (
            <AuditRow key={event.id} event={event} onSelect={onSelect} />
          ))}
        </tbody>
      </ResponsiveTable>
    </div>
  );
}

function AuditSkeleton() {
  return (
    <div className='surface-card p-2'>
      <ResponsiveTable minWidth={760}>
        <thead>
          <tr className='border-b border-divider text-[10.5px] font-semibold uppercase tracking-[0.1em] text-default-400'>
            {['Event', 'Target', 'Actor', 'Result', 'When'].map((label) => (
              <th key={label} className='py-2 px-3 text-left font-medium'>
                {label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {Array.from({ length: 8 }).map((_, index) => (
            <tr key={index} className='border-b border-divider/50'>
              {Array.from({ length: 5 }).map((__, cell) => (
                <td key={cell} className='py-3 px-3'>
                  <span className='block h-3.5 w-full animate-pulse rounded-medium bg-content2' />
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </ResponsiveTable>
    </div>
  );
}

function DetailRow({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className='flex gap-3 border-b border-divider py-2.5 last:border-b-0'>
      <span className='w-28 shrink-0 text-[11.5px] font-semibold uppercase tracking-[0.08em] text-default-400'>
        {label}
      </span>
      <span className='min-w-0 break-all text-[13px] text-default-700'>{value}</span>
    </div>
  );
}

function AuditDetailModal({
  event,
  onClose,
  onFilterActor,
}: {
  event: AuditEvent;
  onClose: () => void;
  onFilterActor: (actorId: string) => void;
}) {
  const isMemoryTarget = event.target_type === 'memory' && Boolean(event.target_id);

  return (
    <div
      className='fixed inset-0 z-50 flex items-end justify-center sm:items-center'
      onClick={onClose}
    >
      <div className='absolute inset-0 bg-black/40 backdrop-blur-[2px]' />
      <div
        className='relative z-10 mx-4 mb-4 w-full max-w-lg rounded-[20px] border border-divider-strong bg-content1 shadow-2xl sm:mb-0'
        onClick={(e) => e.stopPropagation()}
      >
        <div className='flex items-center justify-between border-b border-divider px-6 py-4'>
          <span className='text-[15px] font-semibold text-foreground'>Event detail</span>
          <button
            type='button'
            onClick={onClose}
            className='rounded-[8px] p-1.5 text-default-400 transition-colors hover:bg-content2 hover:text-foreground'
          >
            <X size={18} />
          </button>
        </div>

        <div className='max-h-[70vh] overflow-y-auto p-6'>
          <DetailRow
            label='Event type'
            value={<span className='font-mono text-[12px]'>{event.event_type}</span>}
          />
          <DetailRow label='Result' value={<ResultBadge result={event.result} />} />
          {event.capability && (
            <DetailRow
              label='Capability'
              value={<span className='font-mono text-[12px]'>{event.capability}</span>}
            />
          )}
          <DetailRow
            label='Actor'
            value={
              <div className='flex min-w-0 flex-wrap items-center gap-2'>
                <span>
                  {event.actor_display ?? event.actor_type}
                  <span className='ml-1.5 text-[11.5px] text-default-400'>
                    ({event.actor_type})
                  </span>
                </span>
                {event.actor_id && (
                  <>
                    <CopyableId value={event.actor_id} display={shortenId(event.actor_id)} />
                    <button
                      type='button'
                      onClick={() => onFilterActor(event.actor_id)}
                      className='text-[11.5px] font-medium text-primary-300 hover:underline'
                    >
                      Filter by actor
                    </button>
                  </>
                )}
              </div>
            }
          />
          {(event.target_display || event.target_type || event.target_id) && (
            <DetailRow
              label='Target'
              value={
                <div className='flex min-w-0 flex-wrap items-center gap-2'>
                  <span>
                    {event.target_display ?? event.target_type}
                    {event.target_type && event.target_display && (
                      <span className='ml-1.5 text-[11.5px] text-default-400'>
                        ({event.target_type})
                      </span>
                    )}
                  </span>
                  {isMemoryTarget ? (
                    <Link
                      href={`/memories/${event.target_id}`}
                      className='text-[11.5px] font-medium text-primary-300 hover:underline'
                    >
                      Open memory
                    </Link>
                  ) : (
                    event.target_id && (
                      <CopyableId value={event.target_id} display={shortenId(event.target_id)} />
                    )
                  )}
                </div>
              }
            />
          )}
          {event.request_id && (
            <DetailRow
              label='Request ID'
              value={<span className='break-all font-mono text-[12px]'>{event.request_id}</span>}
            />
          )}
          <DetailRow label='When' value={<TimeStamp value={event.created_at} relative={false} />} />
          {event.metadata && Object.keys(event.metadata).length > 0 && (
            <div className='py-2.5'>
              <span className='block pb-2 text-[11.5px] font-semibold uppercase tracking-[0.08em] text-default-400'>
                Metadata
              </span>
              <pre className='overflow-x-auto rounded-[10px] bg-content2 p-3.5 font-mono text-[11.5px] leading-relaxed text-default-600'>
                {JSON.stringify(event.metadata, null, 2)}
              </pre>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function AuditLog() {
  const activeOrgId = useOrgStore((s) => s.activeOrgId);
  const [filters, setFilters] = useUrlFilters(AUDIT_FILTER_DEFAULTS);

  const projectsQuery = useProjects(activeOrgId, { pageSize: 100 });
  const teamsQuery = useTeams(activeOrgId, { pageSize: 200 });
  const projects = projectsQuery.data?.results ?? [];
  const teams = teamsQuery.data?.results ?? [];

  const page = Math.max(1, filters.page);

  const params = React.useMemo<AuditEventListParams>(() => {
    const next: AuditEventListParams = { page, pageSize: PAGE_SIZE };

    if (filters.event_type) next.event_type = filters.event_type;
    if (filters.result) next.result = filters.result;
    if (filters.actor_id) next.actor_id = filters.actor_id;
    if (filters.target_type) next.target_type = filters.target_type;
    if (filters.project_id) next.project_id = filters.project_id;
    if (filters.team_id) next.team_id = filters.team_id;

    const since = startOfDayIso(filters.since);
    const until = endOfDayExclusiveIso(filters.until);

    if (since) next.created_at__gte = since;
    if (until) next.created_at__lt = until;

    return next;
  }, [
    page,
    filters.event_type,
    filters.result,
    filters.actor_id,
    filters.target_type,
    filters.project_id,
    filters.team_id,
    filters.since,
    filters.until,
  ]);

  const query = useQuery({
    queryKey: adminQueryKeys.auditEvents(activeOrgId, params),
    enabled: Boolean(activeOrgId),
    placeholderData: keepPreviousData,
    queryFn: () => listAuditEvents(params),
  });

  const items = query.data?.results ?? [];
  const total = query.data?.count ?? 0;
  const selectedEvent = filters.event
    ? items.find((event) => event.id === filters.event) ?? null
    : null;

  const hasActiveFilters =
    Boolean(filters.event_type) ||
    Boolean(filters.result) ||
    Boolean(filters.actor_id) ||
    Boolean(filters.target_type) ||
    Boolean(filters.project_id) ||
    Boolean(filters.team_id) ||
    Boolean(filters.since) ||
    Boolean(filters.until);

  return (
    <section className='space-y-6'>
      <PageHeader
        title='Audit log'
        subtitle='Every privileged action across your organization, with actor and outcome.'
      />

      <div className='surface-card p-4'>
        <div className='grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4'>
          <Input
            label='Event type'
            labelPlacement='outside'
            placeholder='e.g. ProjectCreated'
            variant='bordered'
            size='sm'
            value={filters.event_type}
            onValueChange={(v) => setFilters({ event_type: v, page: 1 })}
            isClearable
            onClear={() => setFilters({ event_type: '', page: 1 })}
          />
          <Select
            label='Result'
            labelPlacement='outside'
            placeholder='Any result'
            variant='bordered'
            size='sm'
            selectedKeys={filters.result ? new Set([filters.result]) : new Set<string>()}
            onSelectionChange={(keys) => {
              const next = Array.from(keys)[0];

              setFilters({ result: typeof next === 'string' ? next : '', page: 1 });
            }}
          >
            {RESULT_OPTIONS.map((option) => (
              <SelectItem key={option.key}>{option.label}</SelectItem>
            ))}
          </Select>
          <Input
            label='Actor ID'
            labelPlacement='outside'
            placeholder='actor uuid'
            variant='bordered'
            size='sm'
            value={filters.actor_id}
            onValueChange={(v) => setFilters({ actor_id: v, page: 1 })}
            isClearable
            onClear={() => setFilters({ actor_id: '', page: 1 })}
            classNames={{ input: 'font-mono text-xs' }}
          />
          <Input
            label='Target type'
            labelPlacement='outside'
            placeholder='e.g. memory'
            variant='bordered'
            size='sm'
            value={filters.target_type}
            onValueChange={(v) => setFilters({ target_type: v, page: 1 })}
            isClearable
            onClear={() => setFilters({ target_type: '', page: 1 })}
          />
          <Select
            label='Project'
            labelPlacement='outside'
            placeholder='All projects'
            variant='bordered'
            size='sm'
            selectedKeys={filters.project_id ? new Set([filters.project_id]) : new Set<string>()}
            onSelectionChange={(keys) => {
              const next = Array.from(keys)[0];

              setFilters({ project_id: typeof next === 'string' ? next : '', page: 1 });
            }}
          >
            {projects.map((project) => (
              <SelectItem key={project.id}>{project.name}</SelectItem>
            ))}
          </Select>
          <Select
            label='Team'
            labelPlacement='outside'
            placeholder='All teams'
            variant='bordered'
            size='sm'
            selectedKeys={filters.team_id ? new Set([filters.team_id]) : new Set<string>()}
            onSelectionChange={(keys) => {
              const next = Array.from(keys)[0];

              setFilters({ team_id: typeof next === 'string' ? next : '', page: 1 });
            }}
          >
            {teams.map((team) => (
              <SelectItem key={team.id}>{team.name}</SelectItem>
            ))}
          </Select>
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
        {hasActiveFilters && (
          <div className='mt-3 flex justify-end'>
            <Button size='sm' variant='light' onPress={() => setFilters(AUDIT_FILTER_DEFAULTS)}>
              Clear filters
            </Button>
          </div>
        )}
      </div>

      {query.isLoading ? (
        <AuditSkeleton />
      ) : query.isError && !query.data ? (
        <ErrorState
          message={
            query.error instanceof Error ? query.error.message : 'Failed to load audit events.'
          }
          onRetry={() => query.refetch()}
        />
      ) : items.length === 0 ? (
        <EmptyState
          title={hasActiveFilters ? 'No matching events' : 'No audit events yet'}
          description={
            hasActiveFilters
              ? 'No audit events match the current filters.'
              : 'Privileged actions across your organization will appear here as they happen.'
          }
          icon={<ScrollText className='h-6 w-6' />}
        />
      ) : (
        <div className='space-y-3'>
          <AuditTable items={items} onSelect={(event) => setFilters({ event: event.id })} />
          <PaginationFooter
            page={page}
            pageSize={PAGE_SIZE}
            total={total}
            noun='event'
            onPageChange={(next) => setFilters({ page: next })}
            isDisabled={query.isFetching}
          />
        </div>
      )}

      {selectedEvent && (
        <AuditDetailModal
          event={selectedEvent}
          onClose={() => setFilters({ event: '' })}
          onFilterActor={(actorId) => setFilters({ actor_id: actorId, event: '', page: 1 })}
        />
      )}
    </section>
  );
}

export default function AuditPage() {
  const meQuery = useQuery<MeResponse>({
    queryKey: ['auth', 'me'],
    queryFn: fetchMe,
  });
  const capabilities = meQuery.data?.capabilities ?? [];

  return (
    <CapabilityGate capabilities={capabilities} required='audit:read'>
      <AuditLog />
    </CapabilityGate>
  );
}
