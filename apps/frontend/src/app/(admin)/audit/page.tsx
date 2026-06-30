'use client';

import { useQuery } from '@tanstack/react-query';
import { AlertTriangle, ChevronDown, ScrollText, X } from 'lucide-react';
import * as React from 'react';

import { EmptyState } from '@/components/ui/empty-state';
import { PageHeader } from '@/components/ui/page-header';
import { apiClient } from '@/lib/auth';
import { formatRelativeTime } from '@/lib/design';
import { useProjectStore } from '@/lib/project-store';
import { useTeamStore } from '@/lib/team-store';

const LIMIT = 50;

type AuditEventItem = {
  id: string;
  project_id: string;
  team_id: string | null;
  event_type: string;
  actor_type: string;
  actor_id: string | null;
  target_type: string | null;
  target_id: string | null;
  capability: string | null;
  result: string;
  request_id: string | null;
  correlation_id: string | null;
  metadata: Record<string, unknown> | null;
  created_at: string | null;
  updated_at: string | null;
};

type AuditEventDetail = AuditEventItem & {
  actor_display?: string | null;
  target_display?: string | null;
};

type AuditEventsResponse = {
  count: number;
  items: AuditEventItem[];
};

type AppliedFilters = {
  eventType: string;
  correlationId: string;
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

function AuditRow({
  event,
  onSelect,
}: {
  event: AuditEventItem;
  onSelect: (id: string) => void;
}) {
  const action = resolveAction(event.event_type);
  const resultColor = RESULT_COLORS[event.result] ?? '#666C77';

  return (
    <div
      role='button'
      tabIndex={0}
      onClick={() => onSelect(event.id)}
      onKeyDown={(e) => e.key === 'Enter' && onSelect(event.id)}
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
        {event.target_type || event.target_id ? (
          <div className='truncate text-[13px] text-default-700'>
            {event.target_type ?? 'target'}
            {event.target_id && (
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
        <span className='text-default-500'>{event.actor_type}</span>
        {event.actor_id && (
          <span className='ml-1 font-mono text-[11.5px] text-default-400'>
            {shortenId(event.actor_id)}
          </span>
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
  items: AuditEventItem[];
  onSelect: (id: string) => void;
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
  eventId,
  projectId,
  teamId,
  onClose,
}: {
  eventId: string;
  projectId: string;
  teamId: string | null;
  onClose: () => void;
}) {
  const query = useQuery<AuditEventDetail>({
    queryKey: ['inspection', 'audit-event-detail', eventId, projectId, teamId],
    queryFn: async () => {
      const client = apiClient();
      const params: Record<string, string> = { project_id: projectId };

      if (teamId) {
        params.team_id = teamId;
      }

      const response = await client.get<AuditEventDetail>(
        `/v1/inspection/audit-events/${eventId}`,
        { params },
      );

      return response.data;
    },
  });

  const ev = query.data;
  const resultColor = ev ? (RESULT_COLORS[ev.result] ?? '#666C77') : undefined;

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
          {query.isLoading && (
            <div className='space-y-3'>
              {Array.from({ length: 6 }).map((_, i) => (
                <div key={i} className='h-4 animate-pulse rounded-medium bg-content2' />
              ))}
            </div>
          )}

          {query.isError && (
            <div className='flex items-start gap-3 rounded-[16px] border border-danger/30 bg-danger/5 px-5 py-4'>
              <AlertTriangle className='mt-0.5 h-5 w-5 shrink-0 text-danger' />
              <p className='text-[13px] text-danger'>
                {query.error instanceof Error
                  ? query.error.message
                  : 'Failed to load event detail.'}
              </p>
            </div>
          )}

          {ev && (
            <div>
              <DetailRow
                label='Event type'
                value={<span className='font-mono text-[12px]'>{ev.event_type}</span>}
              />
              <DetailRow
                label='Result'
                value={
                  <span className='inline-flex items-center gap-1.5'>
                    <span
                      className='inline-block h-1.5 w-1.5 rounded-full'
                      style={{ backgroundColor: resultColor }}
                    />
                    <span
                      className='font-medium capitalize'
                      style={{ color: resultColor }}
                    >
                      {ev.result}
                    </span>
                  </span>
                }
              />
              {ev.capability && (
                <DetailRow
                  label='Capability'
                  value={<span className='font-mono text-[12px]'>{ev.capability}</span>}
                />
              )}
              {ev.actor_display ? (
                <DetailRow label='Actor' value={ev.actor_display} />
              ) : (
                <DetailRow
                  label='Actor'
                  value={
                    <span>
                      <span className='text-default-500'>{ev.actor_type}</span>
                      {ev.actor_id && (
                        <span className='ml-1.5 font-mono text-[11.5px] text-default-400'>
                          {ev.actor_id}
                        </span>
                      )}
                    </span>
                  }
                />
              )}
              {(ev.target_display || ev.target_type || ev.target_id) && (
                ev.target_display ? (
                  <DetailRow label='Target' value={ev.target_display} />
                ) : (
                  <DetailRow
                    label='Target'
                    value={
                      <span>
                        <span className='text-default-500'>{ev.target_type}</span>
                        {ev.target_id && (
                          <span className='ml-1.5 font-mono text-[11.5px] text-default-400'>
                            {ev.target_id}
                          </span>
                        )}
                      </span>
                    }
                  />
                )
              )}
              {ev.request_id && (
                <DetailRow
                  label='Request ID'
                  value={
                    <span className='break-all font-mono text-[12px]'>{ev.request_id}</span>
                  }
                />
              )}
              {ev.correlation_id && (
                <DetailRow
                  label='Correlation'
                  value={
                    <span className='break-all font-mono text-[12px]'>{ev.correlation_id}</span>
                  }
                />
              )}
              {ev.created_at && (
                <DetailRow label='When' value={formatRelativeTime(ev.created_at)} />
              )}
              {ev.metadata && Object.keys(ev.metadata).length > 0 && (
                <div className='py-2.5'>
                  <span className='block pb-2 text-[11.5px] font-semibold uppercase tracking-[0.08em] text-default-400'>
                    Metadata
                  </span>
                  <pre className='overflow-x-auto rounded-[10px] bg-content2 p-3.5 font-mono text-[11.5px] leading-relaxed text-default-600'>
                    {JSON.stringify(ev.metadata, null, 2)}
                  </pre>
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export default function AuditPage() {
  const activeProjectId = useProjectStore((s) => s.activeProjectId);
  const activeTeamId = useTeamStore((s) => s.activeTeamId);

  const [draftEventType, setDraftEventType] = React.useState('');
  const [draftCorrelationId, setDraftCorrelationId] = React.useState('');
  const [draftSince, setDraftSince] = React.useState('');
  const [draftUntil, setDraftUntil] = React.useState('');
  const [appliedFilters, setAppliedFilters] = React.useState<AppliedFilters>({
    eventType: '',
    correlationId: '',
    since: '',
    until: '',
  });

  const [extraItems, setExtraItems] = React.useState<AuditEventItem[]>([]);
  const [nextOffset, setNextOffset] = React.useState(LIMIT);
  const [serverCount, setServerCount] = React.useState<number | null>(null);
  const [isLoadingMore, setIsLoadingMore] = React.useState(false);
  const [loadMoreError, setLoadMoreError] = React.useState<string | null>(null);

  const [selectedEventId, setSelectedEventId] = React.useState<string | null>(null);

  React.useEffect(() => {
    setExtraItems([]);
    setNextOffset(LIMIT);
    setServerCount(null);
    setLoadMoreError(null);
  }, [activeProjectId, activeTeamId, appliedFilters]);

  const buildParams = (offset: number): Record<string, string> => {
    const params: Record<string, string> = {
      project_id: activeProjectId ?? '',
      limit: String(LIMIT),
      offset: String(offset),
    };

    if (activeTeamId) params.team_id = activeTeamId;
    if (appliedFilters.eventType) params.event_type = appliedFilters.eventType;
    if (appliedFilters.correlationId) params.correlation_id = appliedFilters.correlationId;
    if (appliedFilters.since) params.since = appliedFilters.since;
    if (appliedFilters.until) params.until = appliedFilters.until;

    return params;
  };

  const query = useQuery<AuditEventsResponse>({
    queryKey: ['inspection', 'audit-events', activeProjectId, activeTeamId, appliedFilters],
    enabled: Boolean(activeProjectId),
    queryFn: async () => {
      const client = apiClient();
      const response = await client.get<AuditEventsResponse>('/v1/inspection/audit-events', {
        params: buildParams(0),
      });

      return response.data;
    },
  });

  React.useEffect(() => {
    if (query.data) {
      setServerCount(query.data.count);
    }
  }, [query.data]);

  const allItems = React.useMemo(
    () => [...(query.data?.items ?? []), ...extraItems],
    [query.data, extraItems],
  );

  const totalCount = serverCount ?? query.data?.count ?? 0;
  const hasMore = allItems.length < totalCount;

  const hasActiveFilters =
    Boolean(appliedFilters.eventType) ||
    Boolean(appliedFilters.correlationId) ||
    Boolean(appliedFilters.since) ||
    Boolean(appliedFilters.until);

  const applyFilters = () => {
    setAppliedFilters({
      eventType: draftEventType.trim(),
      correlationId: draftCorrelationId.trim(),
      since: draftSince,
      until: draftUntil,
    });
  };

  const clearFilters = () => {
    setDraftEventType('');
    setDraftCorrelationId('');
    setDraftSince('');
    setDraftUntil('');
    setAppliedFilters({ eventType: '', correlationId: '', since: '', until: '' });
  };

  const loadMore = async () => {
    if (isLoadingMore || !activeProjectId) {
      return;
    }

    setIsLoadingMore(true);
    setLoadMoreError(null);

    try {
      const client = apiClient();
      const res = await client.get<AuditEventsResponse>('/v1/inspection/audit-events', {
        params: buildParams(nextOffset),
      });

      setExtraItems((prev) => [...prev, ...res.data.items]);
      setServerCount(res.data.count);
      setNextOffset((prev) => prev + LIMIT);
    } catch (err) {
      setLoadMoreError(err instanceof Error ? err.message : 'Failed to load more events.');
    } finally {
      setIsLoadingMore(false);
    }
  };

  if (!activeProjectId) {
    return (
      <section className='space-y-6'>
        <PageHeader
          title='Audit log'
          subtitle='Every privileged action across this project, with actor and outcome.'
        />
        <EmptyState
          title='No project selected'
          description='Select a project to view its audit events.'
          icon={<ScrollText className='h-6 w-6' />}
        />
      </section>
    );
  }

  return (
    <section className='space-y-6'>
      <PageHeader
        title='Audit log'
        subtitle='Every privileged action across this project, with actor and outcome.'
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
              placeholder='e.g. memory.create'
              className='h-9 rounded-[9px] border border-divider-strong bg-content2 px-3 text-[13px] text-foreground outline-none placeholder:text-default-400 focus:border-primary'
            />
          </div>
          <div className='flex min-w-[180px] flex-1 flex-col gap-1.5'>
            <label className='text-[11px] font-semibold uppercase tracking-[0.08em] text-default-400'>
              Correlation ID
            </label>
            <input
              value={draftCorrelationId}
              onChange={(e) => setDraftCorrelationId(e.target.value)}
              placeholder='UUID or prefix'
              className='h-9 rounded-[9px] border border-divider-strong bg-content2 px-3 font-mono text-[13px] text-foreground outline-none placeholder:font-sans placeholder:text-default-400 focus:border-primary'
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
            <AuditTable items={allItems} onSelect={setSelectedEventId} />

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
            description='Privileged actions in this project will appear here as they happen.'
            icon={<ScrollText className='h-6 w-6' />}
          />
        ))}

      {selectedEventId && activeProjectId && (
        <AuditDetailModal
          eventId={selectedEventId}
          projectId={activeProjectId}
          teamId={activeTeamId}
          onClose={() => setSelectedEventId(null)}
        />
      )}
    </section>
  );
}
