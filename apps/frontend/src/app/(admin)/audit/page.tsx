'use client';

import { useQuery } from '@tanstack/react-query';
import { AlertTriangle, ChevronDown, ScrollText, X } from 'lucide-react';
import * as React from 'react';

import { CapabilityGate } from '@/components/ui/capability-gate';
import { EmptyState } from '@/components/ui/empty-state';
import { PageHeader } from '@/components/ui/page-header';
import {
  listAuditEvents,
  type AuditEvent,
  type AuditEventListParams,
} from '@/lib/admin-api';
import { fetchMe, type MeResponse } from '@/lib/auth';
import { formatRelativeTime } from '@/lib/design';
import { useOrgStore } from '@/lib/org-store';
import { adminQueryKeys } from '@/lib/query-keys';

const PAGE_SIZE = 50;

type AppliedFilters = {
  eventType: string;
  result: string;
  since: string;
  until: string;
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

const RESULT_COLORS: Record<string, string> = {
  success: '#3DD9AC',
  allowed: '#3DD9AC',
  recorded: '#6BA6FF',
  denied: '#FB6E72',
};

const GRID =
  'grid grid-cols-[minmax(150px,0.9fr)_minmax(0,1.4fr)_minmax(0,1fr)_auto] items-center gap-4';

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

function resultColorFor(result: string): string {
  return RESULT_COLORS[result] ?? '#666C77';
}

function AuditRow({
  event,
  onSelect,
}: {
  event: AuditEvent;
  onSelect: (event: AuditEvent) => void;
}) {
  const action = resolveAction(event.event_type);
  const resultColor = resultColorFor(event.result);

  return (
    <div
      role='button'
      tabIndex={0}
      onClick={() => onSelect(event)}
      onKeyDown={(e) => e.key === 'Enter' && onSelect(event)}
      className={`${GRID} cursor-pointer border-b border-divider px-5 py-3.5 transition-colors last:border-b-0 hover:bg-content2/60`}
    >
      <div className='min-w-0'>
        <span
          className={`inline-flex max-w-full items-center truncate rounded-[7px] px-2 py-0.5 font-mono text-[11px] font-medium ${ACTION_STYLES[action]}`}
        >
          {event.event_type}
        </span>
      </div>

      <div className='min-w-0'>
        {event.target_display || event.target_type || event.target_id ? (
          <div className='truncate text-[13px] text-default-700'>
            {event.target_display ?? event.target_type ?? 'target'}
            {!event.target_display && event.target_id && (
              <span className='ml-1.5 font-mono text-[11.5px] text-default-400'>
                {shortenId(event.target_id)}
              </span>
            )}
          </div>
        ) : (
          <span className='text-[13px] text-default-400'>—</span>
        )}
        {event.capability && (
          <div className='truncate font-mono text-[11px] text-default-400'>
            {event.capability}
          </div>
        )}
      </div>

      <div className='min-w-0 truncate text-[13px]'>
        <span className='text-default-400'>by </span>
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

      <div className='flex items-center justify-end gap-2.5'>
        <span className='inline-flex items-center gap-1.5'>
          <span
            className='inline-block h-1.5 w-1.5 rounded-full'
            style={{ backgroundColor: resultColor }}
          />
          <span
            className='whitespace-nowrap text-[11.5px] font-medium capitalize'
            style={{ color: resultColor }}
          >
            {event.result}
          </span>
        </span>
        <span className='tnum whitespace-nowrap font-mono text-[11.5px] text-default-400'>
          {formatRelativeTime(event.created_at)}
        </span>
      </div>
    </div>
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
    <div className='surface-card overflow-hidden'>
      <div
        className={`${GRID} border-b border-divider px-5 py-3 text-[10.5px] font-semibold uppercase tracking-[0.1em] text-default-400`}
      >
        <span>Event</span>
        <span>Target</span>
        <span>Actor</span>
        <span className='text-right'>When</span>
      </div>
      {items.map((event) => (
        <AuditRow key={event.id} event={event} onSelect={onSelect} />
      ))}
    </div>
  );
}

function AuditSkeleton() {
  return (
    <div className='surface-card overflow-hidden'>
      <div
        className={`${GRID} border-b border-divider px-5 py-3 text-[10.5px] font-semibold uppercase tracking-[0.1em] text-default-400`}
      >
        <span>Event</span>
        <span>Target</span>
        <span>Actor</span>
        <span className='text-right'>When</span>
      </div>
      {Array.from({ length: 6 }).map((_, index) => (
        <div
          key={index}
          className={`${GRID} border-b border-divider px-5 py-3.5 last:border-b-0`}
        >
          <span className='h-5 w-24 rounded-[7px] bg-content2 animate-pulse' />
          <span className='h-3.5 w-40 rounded-medium bg-content2 animate-pulse' />
          <span className='h-3.5 w-28 rounded-medium bg-content2 animate-pulse' />
          <span className='ml-auto h-3.5 w-16 rounded-medium bg-content2 animate-pulse' />
        </div>
      ))}
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
}: {
  event: AuditEvent;
  onClose: () => void;
}) {
  const resultColor = resultColorFor(event.result);

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
          <DetailRow
            label='Result'
            value={
              <span className='inline-flex items-center gap-1.5'>
                <span
                  className='inline-block h-1.5 w-1.5 rounded-full'
                  style={{ backgroundColor: resultColor }}
                />
                <span className='font-medium capitalize' style={{ color: resultColor }}>
                  {event.result}
                </span>
              </span>
            }
          />
          {event.capability && (
            <DetailRow
              label='Capability'
              value={<span className='font-mono text-[12px]'>{event.capability}</span>}
            />
          )}
          <DetailRow
            label='Actor'
            value={
              event.actor_display ? (
                <span>
                  {event.actor_display}
                  <span className='ml-1.5 text-[11.5px] text-default-400'>
                    ({event.actor_type})
                  </span>
                </span>
              ) : (
                <span>
                  <span className='text-default-500'>{event.actor_type}</span>
                  {event.actor_id && (
                    <span className='ml-1.5 font-mono text-[11.5px] text-default-400'>
                      {event.actor_id}
                    </span>
                  )}
                </span>
              )
            }
          />
          {(event.target_display || event.target_type || event.target_id) && (
            <DetailRow
              label='Target'
              value={
                event.target_display ? (
                  <span>
                    {event.target_display}
                    {event.target_type && (
                      <span className='ml-1.5 text-[11.5px] text-default-400'>
                        ({event.target_type})
                      </span>
                    )}
                  </span>
                ) : (
                  <span>
                    <span className='text-default-500'>{event.target_type}</span>
                    {event.target_id && (
                      <span className='ml-1.5 font-mono text-[11.5px] text-default-400'>
                        {event.target_id}
                      </span>
                    )}
                  </span>
                )
              }
            />
          )}
          {event.request_id && (
            <DetailRow
              label='Request ID'
              value={<span className='break-all font-mono text-[12px]'>{event.request_id}</span>}
            />
          )}
          <DetailRow label='When' value={formatRelativeTime(event.created_at)} />
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

  const [draftEventType, setDraftEventType] = React.useState('');
  const [draftResult, setDraftResult] = React.useState('');
  const [draftSince, setDraftSince] = React.useState('');
  const [draftUntil, setDraftUntil] = React.useState('');
  const [appliedFilters, setAppliedFilters] = React.useState<AppliedFilters>({
    eventType: '',
    result: '',
    since: '',
    until: '',
  });

  const [extraItems, setExtraItems] = React.useState<AuditEvent[]>([]);
  const [nextPage, setNextPage] = React.useState(2);
  const [isLoadingMore, setIsLoadingMore] = React.useState(false);
  const [loadMoreError, setLoadMoreError] = React.useState<string | null>(null);

  const [selectedEvent, setSelectedEvent] = React.useState<AuditEvent | null>(null);

  React.useEffect(() => {
    setExtraItems([]);
    setNextPage(2);
    setLoadMoreError(null);
  }, [appliedFilters]);

  const buildParams = React.useCallback(
    (page: number): AuditEventListParams => {
      const params: AuditEventListParams = { page, pageSize: PAGE_SIZE };

      if (appliedFilters.eventType) params.event_type = appliedFilters.eventType;
      if (appliedFilters.result) params.result = appliedFilters.result;
      if (appliedFilters.since) params.created_at__gte = appliedFilters.since;
      if (appliedFilters.until) params.created_at__lt = appliedFilters.until;

      return params;
    },
    [appliedFilters],
  );

  const query = useQuery({
    queryKey: adminQueryKeys.auditEvents(activeOrgId, appliedFilters),
    enabled: Boolean(activeOrgId),
    queryFn: () => listAuditEvents(buildParams(1)),
  });

  const allItems = React.useMemo(
    () => [...(query.data?.results ?? []), ...extraItems],
    [query.data, extraItems],
  );

  const totalCount = query.data?.count ?? 0;
  const hasMore = allItems.length < totalCount;

  const hasActiveFilters =
    Boolean(appliedFilters.eventType) ||
    Boolean(appliedFilters.result) ||
    Boolean(appliedFilters.since) ||
    Boolean(appliedFilters.until);

  const applyFilters = () => {
    setAppliedFilters({
      eventType: draftEventType.trim(),
      result: draftResult.trim(),
      since: draftSince,
      until: draftUntil,
    });
  };

  const clearFilters = () => {
    setDraftEventType('');
    setDraftResult('');
    setDraftSince('');
    setDraftUntil('');
    setAppliedFilters({ eventType: '', result: '', since: '', until: '' });
  };

  const loadMore = async () => {
    if (isLoadingMore) {
      return;
    }

    setIsLoadingMore(true);
    setLoadMoreError(null);

    try {
      const res = await listAuditEvents(buildParams(nextPage));

      setExtraItems((prev) => [...prev, ...res.results]);
      setNextPage((prev) => prev + 1);
    } catch (err) {
      setLoadMoreError(err instanceof Error ? err.message : 'Failed to load more events.');
    } finally {
      setIsLoadingMore(false);
    }
  };

  return (
    <section className='space-y-6'>
      <PageHeader
        title='Audit log'
        subtitle='Every privileged action across your organization, with actor and outcome.'
      />

      <form
        onSubmit={(e) => {
          e.preventDefault();
          applyFilters();
        }}
        className='surface-card px-5 py-4'
      >
        <div className='flex flex-wrap gap-3'>
          <div className='flex min-w-[160px] flex-1 flex-col gap-1.5'>
            <label className='text-[11px] font-semibold uppercase tracking-[0.08em] text-default-400'>
              Event type
            </label>
            <input
              value={draftEventType}
              onChange={(e) => setDraftEventType(e.target.value)}
              placeholder='e.g. ProjectCreated'
              className='h-9 rounded-[9px] border border-divider-strong bg-content2 px-3 text-[13px] text-foreground outline-none placeholder:text-default-400 focus:border-primary'
            />
          </div>
          <div className='flex min-w-[130px] flex-col gap-1.5'>
            <label className='text-[11px] font-semibold uppercase tracking-[0.08em] text-default-400'>
              Result
            </label>
            <input
              value={draftResult}
              onChange={(e) => setDraftResult(e.target.value)}
              placeholder='success / denied'
              className='h-9 rounded-[9px] border border-divider-strong bg-content2 px-3 text-[13px] text-foreground outline-none placeholder:text-default-400 focus:border-primary'
            />
          </div>
          <div className='flex min-w-[130px] flex-col gap-1.5'>
            <label className='text-[11px] font-semibold uppercase tracking-[0.08em] text-default-400'>
              Since
            </label>
            <input
              type='date'
              value={draftSince}
              onChange={(e) => setDraftSince(e.target.value)}
              className='h-9 rounded-[9px] border border-divider-strong bg-content2 px-3 text-[13px] text-foreground outline-none focus:border-primary'
            />
          </div>
          <div className='flex min-w-[130px] flex-col gap-1.5'>
            <label className='text-[11px] font-semibold uppercase tracking-[0.08em] text-default-400'>
              Until
            </label>
            <input
              type='date'
              value={draftUntil}
              onChange={(e) => setDraftUntil(e.target.value)}
              className='h-9 rounded-[9px] border border-divider-strong bg-content2 px-3 text-[13px] text-foreground outline-none focus:border-primary'
            />
          </div>
          <div className='flex items-end gap-2'>
            <button
              type='submit'
              className='h-9 rounded-[9px] bg-foreground px-4 text-[13px] font-medium text-background transition-colors hover:opacity-80'
            >
              Apply
            </button>
            {hasActiveFilters && (
              <button
                type='button'
                onClick={clearFilters}
                className='h-9 rounded-[9px] border border-divider-strong px-3 text-[13px] font-medium text-default-500 transition-colors hover:text-foreground'
              >
                Clear
              </button>
            )}
          </div>
        </div>
      </form>

      {query.isLoading && <AuditSkeleton />}

      {query.isError && (
        <div className='flex items-start gap-3 rounded-[16px] border border-danger/30 bg-danger/5 px-5 py-4'>
          <AlertTriangle className='mt-0.5 h-5 w-5 shrink-0 text-danger' />
          <p className='text-[13px] leading-relaxed text-danger'>
            {query.error instanceof Error ? query.error.message : 'Failed to load audit events.'}
          </p>
        </div>
      )}

      {query.data &&
        (allItems.length > 0 ? (
          <div className='space-y-3'>
            <AuditTable items={allItems} onSelect={setSelectedEvent} />

            {loadMoreError && (
              <div className='flex items-start gap-3 rounded-[16px] border border-danger/30 bg-danger/5 px-5 py-4'>
                <AlertTriangle className='mt-0.5 h-5 w-5 shrink-0 text-danger' />
                <p className='text-[13px] leading-relaxed text-danger'>{loadMoreError}</p>
              </div>
            )}

            {hasMore ? (
              <button
                type='button'
                onClick={loadMore}
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
            ) : (
              <p className='tnum text-[12px] text-default-400'>
                Total {totalCount} {totalCount === 1 ? 'event' : 'events'}
              </p>
            )}
          </div>
        ) : (
          <EmptyState
            title='No audit events yet'
            description='Privileged actions across your organization will appear here as they happen.'
            icon={<ScrollText className='h-6 w-6' />}
          />
        ))}

      {selectedEvent && (
        <AuditDetailModal event={selectedEvent} onClose={() => setSelectedEvent(null)} />
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
