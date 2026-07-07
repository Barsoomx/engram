'use client';

import { Chip, Input, Select, SelectItem } from '@heroui/react';
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import Link from 'next/link';
import { Workflow } from 'lucide-react';
import * as React from 'react';

import { CapabilityGate } from '@/components/ui/capability-gate';
import { EmptyState } from '@/components/ui/empty-state';
import { ErrorState } from '@/components/ui/error-state';
import { PageHeader } from '@/components/ui/page-header';
import { PaginationFooter } from '@/components/ui/pagination-footer';
import { ResponsiveTable } from '@/components/ui/responsive-table';
import { TableRowSkeleton } from '@/components/ui/table-row-skeleton';
import { TimeStamp } from '@/components/ui/time-stamp';
import { useProjects } from '@/hooks/use-projects';
import { useUrlFilters } from '@/hooks/use-url-filters';
import { useWorkflowRuns } from '@/hooks/use-workflow-runs';
import { fetchMe, type MeResponse } from '@/lib/auth';
import { endOfDayInclusiveIso, startOfDayIso } from '@/lib/format-time';
import { useOrgStore } from '@/lib/org-store';
import type {
  WorkflowRunListItem,
  WorkflowRunListParams,
  WorkflowRunStatus,
  WorkflowRunType,
} from '@/lib/admin-api';

const PAGE_SIZE = 20;

const RUN_TYPE_LABELS: Record<WorkflowRunType, string> = {
  daily_digest: 'Daily digest',
  observation_processing: 'Observation processing',
  session_distillation: 'Session distillation',
  weekly_digest: 'Weekly digest',
};

const RUN_TYPE_OPTIONS = Object.entries(RUN_TYPE_LABELS).map(([key, label]) => ({
  key,
  label,
}));

const STATUS_OPTIONS: { key: WorkflowRunStatus; label: string }[] = [
  { key: 'queued', label: 'Queued' },
  { key: 'running', label: 'Running' },
  { key: 'succeeded', label: 'Succeeded' },
  { key: 'failed', label: 'Failed' },
];

const STATUS_CHIP_COLOR: Record<
  WorkflowRunStatus,
  'default' | 'primary' | 'success' | 'warning' | 'danger'
> = {
  queued: 'default',
  running: 'primary',
  succeeded: 'success',
  failed: 'danger',
};

const ACTIVE_STATUSES: ReadonlySet<string> = new Set(['queued', 'running']);

const WORKFLOW_FILTER_DEFAULTS = {
  run_type: '',
  status: '',
  project_id: '',
  escalation: '',
  request_id: '',
  correlation_id: '',
  since: '',
  until: '',
  page: 1,
};

function runTypeLabel(runType: WorkflowRunType): string {
  return RUN_TYPE_LABELS[runType] ?? runType;
}

function formatDuration(
  startedAt: string | null,
  finishedAt: string | null,
): string {
  if (!startedAt || !finishedAt) {
    return '—';
  }

  const start = new Date(startedAt).getTime();
  const end = new Date(finishedAt).getTime();

  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) {
    return '—';
  }

  const seconds = Math.floor((end - start) / 1000);

  if (seconds < 60) {
    return `${seconds}s`;
  }

  const minutes = Math.floor(seconds / 60);
  const remSeconds = seconds % 60;

  if (minutes < 60) {
    return `${minutes}m ${remSeconds}s`;
  }

  const hours = Math.floor(minutes / 60);

  return `${hours}h ${minutes % 60}m`;
}

function WorkflowRunsTable({
  items,
  projectName,
}: {
  items: WorkflowRunListItem[];
  projectName: (id: string) => string;
}) {
  return (
    <div className='surface-card p-2'>
      <ResponsiveTable minWidth={820}>
        <thead>
          <tr className='border-b border-divider'>
            <th className='py-2 px-3 font-medium text-default-500'>Run type</th>
            <th className='py-2 px-3 font-medium text-default-500'>Status</th>
            <th className='py-2 px-3 font-medium text-default-500'>Project</th>
            <th className='py-2 px-3 font-medium text-default-500'>Escalation</th>
            <th className='py-2 px-3 font-medium text-default-500'>Started</th>
            <th className='py-2 px-3 font-medium text-default-500'>Duration</th>
          </tr>
        </thead>
        <tbody>
          {items.map((run) => (
            <tr key={run.id} className='border-b border-divider/50 hover:bg-content2/40'>
              <td className='py-2 px-3'>
                <Link
                  href={`/workflow-runs/${run.id}`}
                  className='font-medium text-foreground hover:underline'
                >
                  {runTypeLabel(run.run_type)}
                </Link>
              </td>
              <td className='py-2 px-3'>
                <Chip
                  size='sm'
                  variant='flat'
                  color={STATUS_CHIP_COLOR[run.status]}
                  className='capitalize'
                >
                  {run.status}
                </Chip>
              </td>
              <td className='py-2 px-3 text-default-700'>
                <span className='block max-w-[220px] truncate' title={projectName(run.project_id)}>
                  {projectName(run.project_id)}
                </span>
              </td>
              <td className='py-2 px-3'>
                {run.escalation ? (
                  <Chip size='sm' variant='flat' color='warning'>
                    Escalated
                  </Chip>
                ) : (
                  <span className='text-default-500'>—</span>
                )}
              </td>
              <td className='py-2 px-3 whitespace-nowrap text-default-700'>
                <TimeStamp value={run.started_at} />
              </td>
              <td className='py-2 px-3 whitespace-nowrap text-default-700'>
                {formatDuration(run.started_at, run.finished_at)}
              </td>
            </tr>
          ))}
        </tbody>
      </ResponsiveTable>
    </div>
  );
}

export default function WorkflowRunsPage() {
  const activeOrgId = useOrgStore((state) => state.activeOrgId);
  const meQuery = useQuery<MeResponse>({
    queryKey: ['auth', 'me'],
    queryFn: fetchMe,
  });

  const capabilities = React.useMemo(
    () => meQuery.data?.capabilities ?? [],
    [meQuery.data?.capabilities],
  );

  const [filters, setFilters] = useUrlFilters(WORKFLOW_FILTER_DEFAULTS);
  const page = Math.max(1, filters.page);

  const projectsQuery = useProjects(activeOrgId, { pageSize: 100 });
  const projects = React.useMemo(
    () => projectsQuery.data?.results ?? [],
    [projectsQuery.data?.results],
  );
  const projectName = React.useCallback(
    (id: string) => projects.find((project) => project.id === id)?.name ?? id,
    [projects],
  );

  const params = React.useMemo<WorkflowRunListParams>(() => {
    const next: WorkflowRunListParams = { page, pageSize: PAGE_SIZE };

    if (filters.run_type) next.run_type = filters.run_type as WorkflowRunType;
    if (filters.status) next.status = filters.status as WorkflowRunStatus;
    if (filters.project_id) next.project_id = filters.project_id;
    if (filters.escalation) next.escalation = filters.escalation === 'true';
    if (filters.request_id) next.request_id = filters.request_id;
    if (filters.correlation_id) next.correlation_id = filters.correlation_id;

    const since = startOfDayIso(filters.since);
    const until = endOfDayInclusiveIso(filters.until);

    if (since) next.created_at__gte = since;
    if (until) next.created_at__lte = until;

    return next;
  }, [
    page,
    filters.run_type,
    filters.status,
    filters.project_id,
    filters.escalation,
    filters.request_id,
    filters.correlation_id,
    filters.since,
    filters.until,
  ]);

  const runsQuery = useWorkflowRuns(activeOrgId, params, {
    placeholderData: keepPreviousData,
  });

  const items = runsQuery.data?.results ?? [];
  const total = runsQuery.data?.count ?? 0;
  const hasActiveRun = items.some((run) => ACTIVE_STATUSES.has(run.status));

  React.useEffect(() => {
    if (!hasActiveRun) {
      return;
    }

    const handle = setInterval(() => void runsQuery.refetch(), 5000);

    return () => clearInterval(handle);
  }, [hasActiveRun, runsQuery]);

  const hasActiveFilters =
    Boolean(filters.run_type) ||
    Boolean(filters.status) ||
    Boolean(filters.project_id) ||
    Boolean(filters.escalation) ||
    Boolean(filters.request_id) ||
    Boolean(filters.correlation_id) ||
    Boolean(filters.since) ||
    Boolean(filters.until);

  return (
    <CapabilityGate capabilities={capabilities} required='memories:read'>
      <section className='space-y-6'>
        <PageHeader
          title='Workflow runs'
          subtitle='Track AI workflow executions: digests, observation processing, distillations, escalations, and reruns.'
        />

        <div className='surface-card grid grid-cols-1 gap-3 p-4 sm:grid-cols-2 lg:grid-cols-4'>
          <Select
            label='Run type'
            labelPlacement='outside'
            placeholder='All types'
            variant='bordered'
            size='sm'
            selectedKeys={filters.run_type ? new Set([filters.run_type]) : new Set<string>()}
            onSelectionChange={(keys) => {
              const next = Array.from(keys)[0];

              setFilters({ run_type: typeof next === 'string' ? next : '', page: 1 });
            }}
          >
            {RUN_TYPE_OPTIONS.map((option) => (
              <SelectItem key={option.key}>{option.label}</SelectItem>
            ))}
          </Select>
          <Select
            label='Status'
            labelPlacement='outside'
            placeholder='All statuses'
            variant='bordered'
            size='sm'
            selectedKeys={filters.status ? new Set([filters.status]) : new Set<string>()}
            onSelectionChange={(keys) => {
              const next = Array.from(keys)[0];

              setFilters({ status: typeof next === 'string' ? next : '', page: 1 });
            }}
          >
            {STATUS_OPTIONS.map((option) => (
              <SelectItem key={option.key}>{option.label}</SelectItem>
            ))}
          </Select>
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
            label='Escalation'
            labelPlacement='outside'
            placeholder='Any'
            variant='bordered'
            size='sm'
            selectedKeys={filters.escalation ? new Set([filters.escalation]) : new Set<string>()}
            onSelectionChange={(keys) => {
              const next = Array.from(keys)[0];

              setFilters({ escalation: typeof next === 'string' ? next : '', page: 1 });
            }}
          >
            <SelectItem key='true'>Escalated</SelectItem>
            <SelectItem key='false'>Not escalated</SelectItem>
          </Select>
          <Input
            label='Request ID'
            labelPlacement='outside'
            placeholder='request id'
            variant='bordered'
            size='sm'
            value={filters.request_id}
            onValueChange={(v) => setFilters({ request_id: v, page: 1 })}
            isClearable
            onClear={() => setFilters({ request_id: '', page: 1 })}
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
          <Input
            label='Created from'
            labelPlacement='outside'
            type='date'
            variant='bordered'
            size='sm'
            value={filters.since}
            onValueChange={(v) => setFilters({ since: v, page: 1 })}
          />
          <Input
            label='Created to'
            labelPlacement='outside'
            type='date'
            variant='bordered'
            size='sm'
            value={filters.until}
            onValueChange={(v) => setFilters({ until: v, page: 1 })}
          />
        </div>

        {runsQuery.isLoading ? (
          <div className='surface-card p-2'>
            <table className='w-full border-collapse text-left text-sm'>
              <thead>
                <tr className='border-b border-divider'>
                  {Array.from({ length: 6 }).map((_, index) => (
                    <th key={index} className='py-2 px-3 font-medium text-default-500'>
                      <span className='inline-block h-3 w-16 rounded-medium bg-content2/60' />
                    </th>
                  ))}
                </tr>
              </thead>
              <TableRowSkeleton columns={6} />
            </table>
          </div>
        ) : runsQuery.isError && !runsQuery.data ? (
          <ErrorState
            message={
              runsQuery.error instanceof Error
                ? runsQuery.error.message
                : 'Failed to load workflow runs.'
            }
            onRetry={() => runsQuery.refetch()}
          />
        ) : items.length === 0 ? (
          <EmptyState
            title={hasActiveFilters ? 'No matching runs' : 'No workflow runs'}
            description={
              hasActiveFilters
                ? 'No runs match the current filters.'
                : 'No workflows have executed yet.'
            }
            icon={<Workflow className='h-6 w-6' />}
          />
        ) : (
          <div className='space-y-3'>
            <WorkflowRunsTable items={items} projectName={projectName} />
            <PaginationFooter
              page={page}
              pageSize={PAGE_SIZE}
              total={total}
              noun='run'
              onPageChange={(next) => setFilters({ page: next })}
              isDisabled={runsQuery.isFetching}
            />
          </div>
        )}
      </section>
    </CapabilityGate>
  );
}
