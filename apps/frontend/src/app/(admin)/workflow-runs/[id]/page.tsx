'use client';

import { addToast, Button, Chip } from '@heroui/react';
import { useQuery } from '@tanstack/react-query';
import Link from 'next/link';
import { useParams } from 'next/navigation';
import { ArrowLeft, RotateCw, Workflow } from 'lucide-react';
import * as React from 'react';

import { ConfirmDialog } from '@/components/ui/confirm-dialog';
import { EmptyState } from '@/components/ui/empty-state';
import { ErrorState } from '@/components/ui/error-state';
import { ResponsiveTable } from '@/components/ui/responsive-table';
import { TimeStamp } from '@/components/ui/time-stamp';
import { CapabilityGate } from '@/components/ui/capability-gate';
import { useProjects } from '@/hooks/use-projects';
import { useTeams } from '@/hooks/use-teams';
import { useRerunWorkflowRun, useWorkflowRun } from '@/hooks/use-workflow-runs';
import { fetchMe, hasCapability, type MeResponse } from '@/lib/auth';
import { auditResultChipColor } from '@/lib/design';
import { useOrgStore } from '@/lib/org-store';
import type {
  WorkflowRunCuratorAction,
  WorkflowRunDetail,
  WorkflowRunProviderCall,
  WorkflowRunStatus,
  WorkflowRunType,
} from '@/lib/admin-api';

const STATUS_CHIP_COLOR: Record<
  WorkflowRunStatus,
  'default' | 'primary' | 'success' | 'warning' | 'danger'
> = {
  queued: 'default',
  running: 'primary',
  succeeded: 'success',
  failed: 'danger',
};

const RUN_TYPE_LABELS: Record<WorkflowRunType, string> = {
  daily_digest: 'Daily digest',
  observation_processing: 'Observation processing',
  session_distillation: 'Session distillation',
  weekly_digest: 'Weekly digest',
};

const RERUNNABLE_TYPES: ReadonlySet<WorkflowRunType> = new Set<WorkflowRunType>([
  'daily_digest',
  'weekly_digest',
  'session_distillation',
]);

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

function shortId(value: string | null | undefined): string {
  if (!value) {

    return '—';
  }

  return value.length > 8 ? `${value.slice(0, 8)}…` : value;
}

function DetailRow({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className='grid grid-cols-1 md:grid-cols-[220px_1fr] gap-1 md:gap-4 py-2 border-b border-divider/40 last:border-b-0'>
      <dt className='text-xs uppercase tracking-wide text-default-500 font-medium'>
        {label}
      </dt>
      <dd className='text-sm text-foreground break-words'>{children}</dd>
    </div>
  );
}

function InputMemories({ snapshot }: { snapshot: Record<string, unknown> }) {
  const rawIds = snapshot.memory_ids;
  const windowDays = snapshot.window_days;
  const memoryIds = Array.isArray(rawIds)
    ? rawIds.filter((value): value is string => typeof value === 'string')
    : [];

  if (memoryIds.length === 0) {

    return (
      <p className='text-default-500'>No source memories recorded for this run.</p>
    );
  }

  return (
    <div className='space-y-3'>
      {typeof windowDays === 'number' && (
        <p className='text-xs text-default-500'>
          Window: last {windowDays} day{windowDays === 1 ? '' : 's'}.
        </p>
      )}
      <ul className='space-y-1'>
        {memoryIds.map((memoryId) => (
          <li key={memoryId} className='flex items-center gap-2 text-sm'>
            <Link
              href={`/memories/${memoryId}`}
              className='font-mono text-xs text-primary hover:underline break-all'
            >
              {memoryId}
            </Link>
          </li>
        ))}
      </ul>
    </div>
  );
}

function CuratorActionsFeed({
  actions,
}: {
  actions: WorkflowRunCuratorAction[];
}) {
  if (actions.length === 0) {

    return (
      <p className='text-default-500'>No curator actions recorded for this run.</p>
    );
  }

  return (
    <ResponsiveTable minWidth={620}>
      <thead>
        <tr className='border-b border-divider'>
          <th className='py-2 px-3 text-default-500 font-medium'>Event type</th>
          <th className='py-2 px-3 text-default-500 font-medium'>Actor</th>
          <th className='py-2 px-3 text-default-500 font-medium'>Target</th>
          <th className='py-2 px-3 text-default-500 font-medium'>Result</th>
          <th className='py-2 px-3 text-default-500 font-medium'>Created</th>
        </tr>
      </thead>
      <tbody>
        {actions.map((action) => (
          <tr key={action.id} className='border-b border-divider/50'>
            <td className='py-2 px-3 font-mono text-xs text-default-700'>
              {action.event_type}
            </td>
            <td className='py-2 px-3 text-default-700'>
              <span className='font-mono text-xs'>{action.actor_type}</span>
            </td>
            <td className='py-2 px-3 text-default-700'>
              {action.target_type ? (
                <span className='font-mono text-xs'>
                  {action.target_type}
                  {action.target_id ? ` · ${shortId(action.target_id)}` : ''}
                </span>
              ) : (
                <span className='text-default-500'>—</span>
              )}
            </td>
            <td className='py-2 px-3'>
              <Chip
                size='sm'
                variant='flat'
                color={auditResultChipColor(action.result)}
              >
                {action.result}
              </Chip>
            </td>
            <td className='py-2 px-3 text-default-700 whitespace-nowrap'>
              <TimeStamp value={action.created_at} />
            </td>
          </tr>
        ))}
      </tbody>
    </ResponsiveTable>
  );
}

function ProviderCallsTable({
  calls,
}: {
  calls: WorkflowRunProviderCall[];
}) {
  if (calls.length === 0) {

    return <p className='text-default-500'>No provider calls recorded for this run.</p>;
  }

  return (
    <ResponsiveTable minWidth={620}>
      <thead>
        <tr className='border-b border-divider'>
          <th className='py-2 px-3 text-default-500 font-medium'>Provider</th>
          <th className='py-2 px-3 text-default-500 font-medium'>Model</th>
          <th className='py-2 px-3 text-default-500 font-medium'>Task type</th>
          <th className='py-2 px-3 text-default-500 font-medium'>Result</th>
          <th className='py-2 px-3 text-default-500 font-medium'>Latency</th>
        </tr>
      </thead>
      <tbody>
        {calls.map((call) => (
          <tr key={call.id} className='border-b border-divider/50'>
            <td className='py-2 px-3 font-mono text-xs text-default-700'>
              {call.provider}
            </td>
            <td className='py-2 px-3 font-mono text-xs text-default-700'>
              {call.model}
            </td>
            <td className='py-2 px-3 text-default-700'>{call.task_type}</td>
            <td className='py-2 px-3'>
              <Chip
                size='sm'
                variant='flat'
                color={auditResultChipColor(call.result)}
              >
                {call.result}
              </Chip>
            </td>
            <td className='py-2 px-3 text-default-700 whitespace-nowrap'>
              {call.latency_ms !== null ? `${call.latency_ms} ms` : '—'}
            </td>
          </tr>
        ))}
      </tbody>
    </ResponsiveTable>
  );
}

function StatusHeader({ run }: { run: WorkflowRunDetail }) {
  return (
    <div className='surface-card p-5'>
      <div className='flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between'>
        <div className='space-y-1'>
          <div className='flex items-center gap-2'>
            <Workflow className='w-5 h-5 text-default-500' />
            <h2 className='text-lg font-semibold text-foreground'>
              {runTypeLabel(run.run_type)}
            </h2>
            <Chip
              size='sm'
              variant='flat'
              color={STATUS_CHIP_COLOR[run.status]}
              className='capitalize'
            >
              {run.status}
            </Chip>
            {run.escalation && (
              <Chip size='sm' variant='flat' color='warning'>
                Escalated
              </Chip>
            )}
          </div>
          <p className='font-mono text-xs text-default-500 break-all'>{run.id}</p>
        </div>
        <div className='text-sm text-default-500 space-y-0.5'>
          <p className='flex items-center justify-end gap-1'>
            Started: <TimeStamp value={run.started_at} />
          </p>
          <p className='flex items-center justify-end gap-1'>
            Finished: <TimeStamp value={run.finished_at} />
          </p>
          <p>Duration: {formatDuration(run.started_at, run.finished_at)}</p>
        </div>
      </div>
    </div>
  );
}

export default function WorkflowRunDetailPage() {
  const params = useParams<{ id: string }>();
  const id = params?.id ?? '';
  const activeOrgId = useOrgStore((state) => state.activeOrgId);

  const meQuery = useQuery<MeResponse>({
    queryKey: ['auth', 'me'],
    queryFn: fetchMe,
  });

  const capabilities = React.useMemo(
    () => meQuery.data?.capabilities ?? [],
    [meQuery.data?.capabilities],
  );

  const runQuery = useWorkflowRun(activeOrgId, id);
  const rerunMutation = useRerunWorkflowRun(activeOrgId);

  const projectsQuery = useProjects(activeOrgId, { pageSize: 100 });
  const teamsQuery = useTeams(activeOrgId, { pageSize: 200 });

  const [rerunOpen, setRerunOpen] = React.useState(false);

  const canAdmin = hasCapability(capabilities, 'memories:admin');
  const run = runQuery.data;
  const canRerun = run ? RERUNNABLE_TYPES.has(run.run_type) : false;
  const projectName =
    (run && projectsQuery.data?.results.find((p) => p.id === run.project_id)?.name) || run?.project_id;
  const teamName = run?.team_id
    ? teamsQuery.data?.results.find((t) => t.id === run.team_id)?.name ?? run.team_id
    : null;

  async function handleRerun() {
    try {
      const result = await rerunMutation.mutateAsync(id);

      addToast({
        title: 'Workflow rerun queued',
        description: `New run ${result.run_id.slice(0, 8)}… queued for processing.`,
        color: 'success',
      });
      setRerunOpen(false);
    } catch (error) {
      const description =
        error instanceof Error ? error.message : 'Unexpected error.';

      addToast({
        title: 'Failed to rerun workflow',
        description,
        color: 'danger',
      });
      setRerunOpen(false);
    }
  }

  return (
    <CapabilityGate capabilities={capabilities} required='memories:read'>
      <section className='space-y-6'>
        <div className='flex items-center justify-between'>
          <Link
            href='/workflow-runs'
            className='inline-flex items-center gap-1 text-sm text-primary hover:underline'
          >
            <ArrowLeft className='w-4 h-4' />
            Back to workflow runs
          </Link>
          {canAdmin && canRerun && (
            <Button
              color='primary'
              variant='flat'
              startContent={<RotateCw className='w-4 h-4' />}
              onPress={() => setRerunOpen(true)}
              isLoading={rerunMutation.isPending}
            >
              Rerun
            </Button>
          )}
        </div>

        {runQuery.isLoading && (
          <p className='text-default-500'>Loading workflow run...</p>
        )}

        {runQuery.isError && (
          <ErrorState
            message={
              runQuery.error instanceof Error
                ? runQuery.error.message
                : 'Failed to load workflow run.'
            }
            onRetry={() => runQuery.refetch()}
          />
        )}

        {runQuery.data && (
          <>
            <StatusHeader run={runQuery.data} />

            <div className='surface-card p-5'>
              <h3 className='text-base font-semibold text-foreground mb-3'>
                Details
              </h3>
              <dl className='flex flex-col'>
                <DetailRow label='Run type'>{runTypeLabel(runQuery.data.run_type)}</DetailRow>
                <DetailRow label='Project'>
                  <span title={runQuery.data.project_id}>{projectName}</span>
                </DetailRow>
                <DetailRow label='Team'>
                  {teamName ? (
                    <span title={runQuery.data.team_id ?? undefined}>{teamName}</span>
                  ) : (
                    <span className='text-default-500'>—</span>
                  )}
                </DetailRow>
                <DetailRow label='Request ID'>
                  <span className='font-mono text-xs break-all'>
                    {runQuery.data.request_id || '—'}
                  </span>
                </DetailRow>
                <DetailRow label='Correlation ID'>
                  <span className='font-mono text-xs break-all'>
                    {runQuery.data.correlation_id || '—'}
                  </span>
                </DetailRow>
                {runQuery.data.rerun_of_id && (
                  <DetailRow label='Rerun of'>
                    <Link
                      href={`/workflow-runs/${runQuery.data.rerun_of_id}`}
                      className='font-mono text-xs text-primary hover:underline'
                    >
                      {runQuery.data.rerun_of_id}
                    </Link>
                  </DetailRow>
                )}
                {runQuery.data.result_memory && (
                  <DetailRow label='Result memory'>
                    <Link
                      href={`/memories/${runQuery.data.result_memory.id}`}
                      className='text-primary hover:underline'
                    >
                      {runQuery.data.result_memory.title || runQuery.data.result_memory.id}
                    </Link>
                  </DetailRow>
                )}
                {runQuery.data.failure_reason && (
                  <DetailRow label='Failure reason'>
                    <span className='text-danger-600'>
                      {runQuery.data.failure_reason}
                    </span>
                  </DetailRow>
                )}
                <DetailRow label='Created at'>
                  <TimeStamp value={runQuery.data.created_at} relative={false} />
                </DetailRow>
              </dl>
            </div>

            <div className='surface-card p-5'>
              <h3 className='text-base font-semibold text-foreground mb-3'>
                Inputs (source memories)
              </h3>
              <InputMemories snapshot={runQuery.data.input_snapshot} />
            </div>

            <div className='surface-card p-5'>
              <h3 className='text-base font-semibold text-foreground mb-3'>
                Curator actions
              </h3>
              <CuratorActionsFeed actions={runQuery.data.curator_actions} />
            </div>

            <div className='surface-card p-5'>
              <h3 className='text-base font-semibold text-foreground mb-3'>
                Provider calls
              </h3>
              <ProviderCallsTable calls={runQuery.data.provider_calls} />
            </div>
          </>
        )}

        {runQuery.data === undefined && !runQuery.isLoading && !runQuery.isError && (
          <EmptyState
            title='Workflow run not found'
            description='This run may have been removed, or you may not have access to it.'
            icon={<Workflow className='w-6 h-6' />}
          />
        )}

        <ConfirmDialog
          isOpen={rerunOpen}
          title='Rerun workflow'
          description='Re-execute this workflow with the same inputs. A new run will be created and tracked.'
          confirmLabel='Rerun'
          confirmColor='primary'
          isLoading={rerunMutation.isPending}
          onClose={() => setRerunOpen(false)}
          onConfirm={handleRerun}
        />
      </section>
    </CapabilityGate>
  );
}
