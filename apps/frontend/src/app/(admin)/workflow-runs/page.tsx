'use client';

import {
  Button,
  Chip,
  Input,
  Pagination,
  Select,
  SelectItem,
} from '@heroui/react';
import { useQuery } from '@tanstack/react-query';
import Link from 'next/link';
import { Clock, GitBranch, RefreshCw, Workflow } from 'lucide-react';
import * as React from 'react';

import { CapabilityGate } from '@/components/ui/capability-gate';
import { EmptyState } from '@/components/ui/empty-state';
import { PageHeader } from '@/components/ui/page-header';
import { TableRowSkeleton } from '@/components/ui/table-row-skeleton';
import { useProjects } from '@/hooks/use-projects';
import { useWorkflowRuns } from '@/hooks/use-workflow-runs';
import { fetchMe, hasCapability, type MeResponse } from '@/lib/auth';
import { useOrgStore } from '@/lib/org-store';
import type {
  Project,
  WorkflowRunListItem,
  WorkflowRunListParams,
  WorkflowRunStatus,
  WorkflowRunType,
} from '@/lib/admin-api';

const RUN_TYPE_OPTIONS: { key: WorkflowRunType; label: string }[] = [
  { key: 'daily_digest', label: 'Daily Digest' },
  { key: 'observation_processing', label: 'Observation Processing' },
];

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

const PAGE_SIZE = 20;

function formatDateTime(value: string | null): string {
  if (!value) {

    return '—';
  }

  try {

    return new Date(value).toLocaleString();
  } catch {

    return value;
  }
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

  const diffMs = end - start;
  const seconds = Math.floor(diffMs / 1000);

  if (seconds < 60) {

    return `${seconds}s`;
  }

  const minutes = Math.floor(seconds / 60);
  const remSeconds = seconds % 60;

  if (minutes < 60) {

    return `${minutes}m ${remSeconds}s`;
  }

  const hours = Math.floor(minutes / 60);
  const remMinutes = minutes % 60;

  return `${hours}h ${remMinutes}m`;
}

function shortId(value: string | null): string {
  if (!value) {

    return '—';
  }

  return value.length > 8 ? `${value.slice(0, 8)}…` : value;
}

function FiltersBar({
  filters,
  projects,
  onChange,
  onReset,
}: {
  filters: Omit<WorkflowRunListParams, 'page' | 'pageSize'>;
  projects: Project[];
  onChange: (
    next: Partial<Omit<WorkflowRunListParams, 'page' | 'pageSize'>>,
  ) => void;
  onReset: () => void;
}) {
  const runTypeKeys = React.useMemo(
    () => (filters.run_type ? new Set([filters.run_type]) : new Set<string>()),
    [filters.run_type],
  );
  const statusKeys = React.useMemo(
    () => (filters.status ? new Set([filters.status]) : new Set<string>()),
    [filters.status],
  );
  const escalationKeys = React.useMemo(() => {
    if (filters.escalation === undefined) {

      return new Set<string>();
    }

    return new Set([filters.escalation ? 'true' : 'false']);
  }, [filters.escalation]);
  const projectKeys = React.useMemo(
    () =>
      filters.project_id ? new Set([filters.project_id]) : new Set<string>(),
    [filters.project_id],
  );

  return (
    <div className='surface-card p-4 grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3'>
      <Select
        label='Run type'
        labelPlacement='outside'
        placeholder='All types'
        selectedKeys={runTypeKeys}
        onSelectionChange={(keys) => {
          const next = Array.from(keys)[0];

          onChange({ run_type: typeof next === 'string' ? (next as WorkflowRunType) : undefined });
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
        selectedKeys={statusKeys}
        onSelectionChange={(keys) => {
          const next = Array.from(keys)[0];

          onChange({ status: typeof next === 'string' ? (next as WorkflowRunStatus) : undefined });
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
        selectedKeys={projectKeys}
        onSelectionChange={(keys) => {
          const next = Array.from(keys)[0];

          onChange({ project_id: typeof next === 'string' ? next : undefined });
        }}
      >
        {(project: Project) => (
          <SelectItem key={project.id}>{project.name}</SelectItem>
        )}
      </Select>

      <Select
        label='Escalation'
        labelPlacement='outside'
        placeholder='Any'
        selectedKeys={escalationKeys}
        onSelectionChange={(keys) => {
          const next = Array.from(keys)[0];

          if (typeof next !== 'string') {
            onChange({ escalation: undefined });

            return;
          }

          onChange({ escalation: next === 'true' });
        }}
      >
        <SelectItem key='true'>Escalated</SelectItem>
        <SelectItem key='false'>Not escalated</SelectItem>
      </Select>

      <Input
        label='Created from'
        labelPlacement='outside'
        placeholder='YYYY-MM-DD'
        type='date'
        value={filters.created_at__gte ?? ''}
        onValueChange={(value) => onChange({ created_at__gte: value || undefined })}
      />

      <Input
        label='Created to'
        labelPlacement='outside'
        placeholder='YYYY-MM-DD'
        type='date'
        value={filters.created_at__lte ?? ''}
        onValueChange={(value) => onChange({ created_at__lte: value || undefined })}
      />

      <div className='md:col-span-2 lg:col-span-3 flex justify-end'>
        <Button
          size='sm'
          variant='flat'
          color='default'
          startContent={<RefreshCw className='w-3.5 h-3.5' />}
          onPress={onReset}
        >
          Reset filters
        </Button>
      </div>
    </div>
  );
}

function WorkflowRunsTable({ items }: { items: WorkflowRunListItem[] }) {
  return (
    <div className='overflow-x-auto'>
      <table className='w-full border-collapse text-left text-sm'>
        <thead>
          <tr className='border-b border-divider'>
            <th className='py-2 px-3 text-default-500 font-medium'>Run type</th>
            <th className='py-2 px-3 text-default-500 font-medium'>Status</th>
            <th className='py-2 px-3 text-default-500 font-medium'>Project</th>
            <th className='py-2 px-3 text-default-500 font-medium'>Escalation</th>
            <th className='py-2 px-3 text-default-500 font-medium'>Started at</th>
            <th className='py-2 px-3 text-default-500 font-medium'>Duration</th>
          </tr>
        </thead>
        <tbody>
          {items.map((run) => (
            <tr key={run.id} className='border-b border-divider/50 hover:bg-content2/40'>
              <td className='py-2 px-3'>
                <Link
                  href={`/workflow-runs/${run.id}`}
                  className='text-foreground hover:underline'
                >
                  {run.run_type}
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
              <td className='py-2 px-3 font-mono text-xs text-default-700'>
                {shortId(run.project_id)}
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
              <td className='py-2 px-3 text-default-700 whitespace-nowrap'>
                {formatDateTime(run.started_at)}
              </td>
              <td className='py-2 px-3 text-default-700 whitespace-nowrap'>
                {formatDuration(run.started_at, run.finished_at)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
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

  const [rawFilters, setRawFilters] = React.useState<
    Omit<WorkflowRunListParams, 'page' | 'pageSize'>
  >({});
  const [page, setPage] = React.useState(1);

  const projectsParams = React.useMemo(() => ({ pageSize: 100 }), []);
  const projectsQuery = useProjects(activeOrgId, projectsParams);

  const projects = React.useMemo(
    () => projectsQuery.data?.results ?? [],
    [projectsQuery.data?.results],
  );

  const params = React.useMemo<WorkflowRunListParams>(
    () => ({ ...rawFilters, page, pageSize: PAGE_SIZE }),
    [rawFilters, page],
  );

  const runsQuery = useWorkflowRuns(activeOrgId, params);

  const total = runsQuery.data?.count ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  function handleFilterChange(
    next: Partial<Omit<WorkflowRunListParams, 'page' | 'pageSize'>>,
  ) {
    setRawFilters((prev) => ({ ...prev, ...next }));
    setPage(1);
  }

  function handleReset() {
    setRawFilters({});
    setPage(1);
  }

  const isLoading = meQuery.isLoading || runsQuery.isLoading;
  const items = runsQuery.data?.results ?? [];
  const meLoaded = meQuery.data !== undefined;

  return (
    <CapabilityGate capabilities={capabilities} required='memories:read'>
      <section className='space-y-6'>
        <PageHeader
          title='Workflow Runs'
          subtitle='Track AI workflow executions: daily digests, curator actions, escalations, and reruns.'
        />

        <FiltersBar
          filters={rawFilters}
          projects={projects}
          onChange={handleFilterChange}
          onReset={handleReset}
        />

        <div className='surface-card p-2'>
          {isLoading ? (
            <table className='w-full border-collapse text-left text-sm'>
              <thead>
                <tr className='border-b border-divider'>
                  {Array.from({ length: 6 }).map((_, index) => (
                    <th
                      key={index}
                      className='py-2 px-3 text-default-500 font-medium'
                    >
                      <span className='inline-block w-16 h-3 rounded-medium bg-content2/60' />
                    </th>
                  ))}
                </tr>
              </thead>
              <TableRowSkeleton columns={6} />
            </table>
          ) : items.length === 0 ? (
            <EmptyState
              title='No workflow runs'
              description='No runs match the current filters, or no workflows have executed yet.'
              icon={<Workflow className='w-6 h-6' />}
            />
          ) : (
            <WorkflowRunsTable items={items} />
          )}
        </div>

        {total > 0 && (
          <div className='flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between'>
            <p className='text-xs text-default-500'>
              Showing {(page - 1) * PAGE_SIZE + 1}–
              {Math.min(page * PAGE_SIZE, total)} of {total} run
              {total === 1 ? '' : 's'}.
            </p>
            <Pagination
              total={totalPages}
              page={page}
              onChange={setPage}
              size='sm'
              isDisabled={!meLoaded}
            />
          </div>
        )}

        {runsQuery.isError && (
          <pre className='text-sm text-danger-500 bg-danger-50 dark:bg-danger-500/10 rounded-medium p-3'>
            {runsQuery.error instanceof Error
              ? runsQuery.error.message
              : 'Failed to load workflow runs.'}
          </pre>
        )}

        <div className='flex items-center gap-2 text-xs text-default-500'>
          <Clock className='w-3.5 h-3.5' />
          <span>Duration = finished_at − started_at.</span>
          <GitBranch className='w-3.5 h-3.5 ml-2' />
          <span>Reruns are available on the run detail page.</span>
        </div>
      </section>
    </CapabilityGate>
  );
}
