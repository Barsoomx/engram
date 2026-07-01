'use client';

import { addToast, Button, Input } from '@heroui/react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import axios from 'axios';
import { AlertTriangle, CheckCircle2, Cpu, XCircle } from 'lucide-react';
import * as React from 'react';

import { EmptyState } from '@/components/ui/empty-state';
import { PageHeader } from '@/components/ui/page-header';
import { fetchMe, hasCapability, type MeResponse } from '@/lib/auth';
import {
  applyPreset,
  genRequestId,
  getModelPresets,
  getModelSetupStatus,
  POLICY_TASK_TYPES,
  type ModelPreset,
  type ModelSetupStatus,
  type TaskTypeStatus,
} from '@/lib/console-api';
import { useOrgStore } from '@/lib/org-store';
import { useProjectStore } from '@/lib/project-store';
import { adminQueryKeys } from '@/lib/query-keys';
import { useTeamStore } from '@/lib/team-store';

function humanizeTask(value: string): string {
  return value
    .split('_')
    .filter(Boolean)
    .map((word) => word[0].toUpperCase() + word.slice(1))
    .join(' ');
}

function extractDetail(error: unknown, fallback: string): string {
  if (axios.isAxiosError(error)) {
    const data = error.response?.data as
      | { detail?: string; code?: string }
      | undefined;

    if (data?.detail) {

      return data.detail;
    }
  }

  return fallback;
}

function ReadinessBanner({ status }: { status: ModelSetupStatus }) {
  const configured = status.task_types.filter((t) => t.configured).length;
  const total = status.task_types.length;

  if (status.ready) {

    return (
      <div className='flex items-center gap-3 rounded-[14px] border border-success/30 bg-success/[0.06] px-5 py-4'>
        <CheckCircle2 className='h-5 w-5 shrink-0 text-success' />
        <p className='text-[13.5px] font-medium text-success'>
          All model tasks configured — system ready.
        </p>
      </div>
    );
  }

  const missing = status.task_types
    .filter((t) => !t.configured)
    .map((t) => humanizeTask(t.task_type));

  return (
    <div className='flex items-start gap-3 rounded-[14px] border border-warning/30 bg-warning/[0.06] px-5 py-4'>
      <AlertTriangle className='mt-0.5 h-5 w-5 shrink-0 text-warning' />
      <div className='min-w-0 space-y-1'>
        <p className='text-[13.5px] font-medium text-warning'>
          {configured} of {total} task types configured
        </p>
        <p className='text-[12px] text-warning/80'>
          Missing: {missing.join(', ')}
        </p>
      </div>
    </div>
  );
}

function TaskCard({ task }: { task: TaskTypeStatus }) {
  return (
    <div className='surface-card flex flex-col gap-3 p-5'>
      <div className='flex items-center justify-between gap-2'>
        <div className='flex items-center gap-2'>
          <span className='inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-[9px] bg-content3 text-primary-300'>
            <Cpu className='h-[15px] w-[15px]' strokeWidth={1.8} />
          </span>
          <span className='text-[13.5px] font-semibold text-foreground'>
            {humanizeTask(task.task_type)}
          </span>
        </div>
        {task.configured ? (
          <CheckCircle2 className='h-4 w-4 shrink-0 text-success' />
        ) : (
          <XCircle className='h-4 w-4 shrink-0 text-default-400' />
        )}
      </div>
      {task.configured && (task.provider || task.model) && (
        <div className='space-y-1.5'>
          {task.provider && (
            <p className='text-[11.5px] text-default-500'>
              <span className='font-medium text-default-600'>Provider:</span>{' '}
              {task.provider}
            </p>
          )}
          {task.model && (
            <p className='truncate font-mono text-[11.5px] text-default-500'>
              {task.model}
            </p>
          )}
          <div className='flex items-center gap-1.5'>
            <span
              className={`h-1.5 w-1.5 rounded-full ${task.secret_active ? 'bg-success' : 'bg-danger'}`}
            />
            <span className='text-[11px] text-default-400'>
              {task.secret_active ? 'Secret active' : 'Secret inactive'}
            </span>
          </div>
        </div>
      )}
      {!task.configured && (
        <p className='text-[12px] text-default-400'>Not configured</p>
      )}
    </div>
  );
}

function PresetCard({
  preset,
  projectId,
  canApply,
  onApplied,
}: {
  preset: ModelPreset;
  projectId: string;
  canApply: boolean;
  onApplied: () => void;
}) {
  const [expanded, setExpanded] = React.useState(false);
  const [keys, setKeys] = React.useState<Record<string, string>>({});
  const [applyError, setApplyError] = React.useState<string | null>(null);

  const applyMutation = useMutation({
    mutationFn: () =>
      applyPreset({
        project_id: projectId,
        scope: 'organization',
        preset_key: preset.key,
        provider_keys: keys,
        request_id: genRequestId(),
      }),
    onSuccess: () => {
      addToast({
        title: 'Preset applied',
        description: `${preset.name} is now active.`,
        color: 'success',
      });
      setExpanded(false);
      setKeys({});
      setApplyError(null);
      onApplied();
    },
    onError: (error) => {
      setApplyError(extractDetail(error, 'Failed to apply preset.'));
    },
  });

  function handleSetKey(slot: string, value: string) {
    setKeys((prev) => ({ ...prev, [slot]: value }));
  }

  const allFilled = preset.providers_needed.every(
    (slot) => (keys[slot] ?? '').trim().length > 0,
  );

  return (
    <div className='surface-card overflow-hidden'>
      <div className='p-5'>
        <div className='flex items-start justify-between gap-3'>
          <div className='min-w-0 space-y-1'>
            <h3 className='text-[14px] font-semibold text-foreground'>
              {preset.name}
            </h3>
            <p className='text-[12.5px] leading-relaxed text-default-500'>
              {preset.description}
            </p>
          </div>
          {canApply && (
            <Button
              size='sm'
              color='primary'
              variant='flat'
              onPress={() => {
                setApplyError(null);
                setExpanded((prev) => !prev);
              }}
            >
              {expanded ? 'Cancel' : 'Use this preset'}
            </Button>
          )}
        </div>

        {preset.providers_needed.length > 0 && (
          <div className='mt-3 flex flex-wrap gap-1.5'>
            {preset.providers_needed.map((slot) => (
              <span
                key={slot}
                className='rounded-[7px] bg-content3 px-2.5 py-0.5 text-[11.5px] font-medium text-default-500'
              >
                {slot.charAt(0).toUpperCase() + slot.slice(1)}
              </span>
            ))}
          </div>
        )}
      </div>

      {expanded && canApply && (
        <div className='border-t border-divider bg-content2/30 px-5 py-4 space-y-4'>
          {preset.providers_needed.map((slot) => (
            <Input
              key={slot}
              label={`${slot.charAt(0).toUpperCase() + slot.slice(1)} API key`}
              labelPlacement='outside'
              placeholder='sk-…'
              type='password'
              value={keys[slot] ?? ''}
              onValueChange={(v) => handleSetKey(slot, v)}
              isDisabled={applyMutation.isPending}
            />
          ))}
          {applyError && (
            <div className='flex items-start gap-2.5 rounded-[12px] border border-danger/30 bg-danger/5 px-3.5 py-3'>
              <AlertTriangle className='mt-0.5 h-4 w-4 shrink-0 text-danger' />
              <p className='text-[13px] leading-relaxed text-danger'>
                {applyError}
              </p>
            </div>
          )}
          <Button
            color='primary'
            isDisabled={!allFilled}
            isLoading={applyMutation.isPending}
            onPress={() => applyMutation.mutate()}
          >
            Apply preset
          </Button>
        </div>
      )}

      {!canApply && (
        <div className='border-t border-divider px-5 py-3'>
          <p className='text-[12px] text-default-400'>
            Requires <span className='font-mono'>model_policy:*</span> and{' '}
            <span className='font-mono'>secrets:*</span> — ask an admin to
            apply this preset.
          </p>
        </div>
      )}
    </div>
  );
}

export default function ModelSetupPage() {
  const activeProjectId = useProjectStore((s) => s.activeProjectId);
  const activeTeamId = useTeamStore((s) => s.activeTeamId);
  const activeOrgId = useOrgStore((s) => s.activeOrgId);
  const queryClient = useQueryClient();

  const meQuery = useQuery<MeResponse>({
    queryKey: ['auth', 'me'],
    queryFn: fetchMe,
  });

  const capabilities = React.useMemo(
    () => meQuery.data?.capabilities ?? [],
    [meQuery.data?.capabilities],
  );

  const canManage =
    hasCapability(capabilities, 'model_policy:*') &&
    hasCapability(capabilities, 'secrets:*');

  const statusQuery = useQuery<ModelSetupStatus>({
    queryKey: adminQueryKeys.modelSetupStatus(activeOrgId, activeProjectId, activeTeamId),
    enabled: Boolean(activeProjectId),
    queryFn: () =>
      getModelSetupStatus(activeProjectId ?? '', activeTeamId ?? undefined),
  });

  const presetsQuery = useQuery<{ presets: ModelPreset[] }>({
    queryKey: adminQueryKeys.modelPresets(activeOrgId),
    queryFn: getModelPresets,
  });

  function invalidateStatus() {
    queryClient.invalidateQueries({
      queryKey: adminQueryKeys.modelSetupStatus(activeOrgId, activeProjectId, activeTeamId),
    });
  }

  if (!activeProjectId) {

    return (
      <section className='space-y-6'>
        <PageHeader
          title='Model Setup'
          subtitle='Configure which model serves each task type via presets.'
        />
        <EmptyState
          title='Select a project'
          description='Choose a project from the switcher above to view model setup status.'
          icon={<Cpu className='h-6 w-6' />}
        />
      </section>
    );
  }

  const status = statusQuery.data;
  const presets = presetsQuery.data?.presets ?? [];

  const taskTypes = status?.task_types ?? POLICY_TASK_TYPES.map((t) => ({
    task_type: t,
    configured: false,
    policy_id: null,
    provider: null,
    model: null,
    secret_active: false,
  }));

  return (
    <section className='space-y-6'>
      <PageHeader
        title='Model Setup'
        subtitle='Configure which model serves each task type via presets.'
      />

      {statusQuery.isError && (
        <div className='flex items-start gap-3 rounded-[16px] border border-danger/30 bg-danger/5 px-5 py-4'>
          <AlertTriangle className='mt-0.5 h-5 w-5 shrink-0 text-danger' />
          <p className='text-[13px] leading-relaxed text-danger'>
            {statusQuery.error instanceof Error
              ? statusQuery.error.message
              : 'Failed to load model setup status.'}
          </p>
        </div>
      )}

      {status && <ReadinessBanner status={status} />}

      {!statusQuery.isError && (
        <>
          <div>
            <h2 className='mb-3 text-[12px] font-semibold uppercase tracking-[0.1em] text-default-400'>
              Task types
            </h2>
            <div className='grid gap-3 sm:grid-cols-2 lg:grid-cols-3'>
              {statusQuery.isLoading
                ? Array.from({ length: 6 }).map((_, i) => (
                    <div
                      key={i}
                      className='surface-card h-[100px] animate-pulse bg-content2/50'
                    />
                  ))
                : taskTypes.map((task) => (
                    <TaskCard key={task.task_type} task={task} />
                  ))}
            </div>
          </div>

          <div>
            <h2 className='mb-3 text-[12px] font-semibold uppercase tracking-[0.1em] text-default-400'>
              Presets
            </h2>
            {presetsQuery.isLoading ? (
              <div className='surface-card h-24 animate-pulse bg-content2/50' />
            ) : presetsQuery.isError ? (
              <div className='flex items-start gap-3 rounded-[16px] border border-danger/30 bg-danger/5 px-5 py-4'>
                <AlertTriangle className='mt-0.5 h-5 w-5 shrink-0 text-danger' />
                <p className='text-[13px] leading-relaxed text-danger'>
                  Failed to load presets.
                </p>
              </div>
            ) : presets.length === 0 ? (
              <div className='surface-card px-5 py-8 text-center text-[13px] text-default-400'>
                No presets available.
              </div>
            ) : (
              <div className='space-y-4'>
                {presets.map((preset) => (
                  <PresetCard
                    key={preset.key}
                    preset={preset}
                    projectId={activeProjectId}
                    canApply={canManage}
                    onApplied={invalidateStatus}
                  />
                ))}
              </div>
            )}
          </div>
        </>
      )}
    </section>
  );
}
