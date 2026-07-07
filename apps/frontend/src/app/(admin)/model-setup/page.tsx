'use client';

import { addToast, Button, Input, Select, SelectItem } from '@heroui/react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import axios from 'axios';
import { AlertTriangle, ArrowRight, CheckCircle2, Cpu, XCircle, Zap } from 'lucide-react';
import Link from 'next/link';
import * as React from 'react';

import { ConfirmDialog } from '@/components/ui/confirm-dialog';
import { EmptyState } from '@/components/ui/empty-state';
import { PageHeader } from '@/components/ui/page-header';
import { StatusPill } from '@/components/ui/status-pill';
import { fetchMe, hasCapability, type MeResponse } from '@/lib/auth';
import {
  applyPreset,
  existingPoliciesConflict,
  genRequestId,
  getModelPresets,
  getModelSetupStatus,
  POLICY_TASK_TYPES,
  validateModelPolicies,
  type ExistingPoliciesConflict,
  type ModelPreset,
  type ModelSetupStatus,
  type PolicyScope,
  type PolicyValidationResult,
  type TaskTypeStatus,
} from '@/lib/console-api';
import { useOrgStore } from '@/lib/org-store';
import { useProjectStore } from '@/lib/project-store';
import { adminQueryKeys } from '@/lib/query-keys';
import { useTeamStore } from '@/lib/team-store';

const PRESET_SCOPE_OPTIONS: { key: PolicyScope; label: string }[] = [
  { key: 'organization', label: 'Organization' },
  { key: 'project', label: 'Project' },
  { key: 'team', label: 'Team' },
];

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

function ModelPoliciesLink() {
  return (
    <Link
      href='/model-policies'
      className='inline-flex items-center gap-1.5 rounded-[9px] border border-divider bg-content1 px-3 py-1.5 text-[12.5px] font-medium text-default-600 transition-colors hover:text-foreground'
    >
      Model policies
      <ArrowRight size={14} strokeWidth={1.9} />
    </Link>
  );
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
  teamId,
  canApply,
  onApplied,
}: {
  preset: ModelPreset;
  projectId: string;
  teamId: string | null;
  canApply: boolean;
  onApplied: () => void;
}) {
  const [expanded, setExpanded] = React.useState(false);
  const [keys, setKeys] = React.useState<Record<string, string>>({});
  const [scope, setScope] = React.useState<PolicyScope>('organization');
  const [applyError, setApplyError] = React.useState<string | null>(null);
  const [conflict, setConflict] =
    React.useState<ExistingPoliciesConflict | null>(null);

  const applyMutation = useMutation({
    mutationFn: (replaceExisting: boolean) =>
      applyPreset({
        project_id: projectId,
        team_id: scope === 'team' ? teamId : null,
        scope,
        preset_key: preset.key,
        provider_keys: keys,
        request_id: genRequestId(),
        replace_existing: replaceExisting,
      }),
    onSuccess: (result) => {
      const replaced = result.disabled_policy_ids.length;
      addToast({
        title: 'Preset applied',
        description:
          replaced > 0
            ? `${preset.name} is now active; ${replaced} previous ${replaced === 1 ? 'policy' : 'policies'} replaced.`
            : `${preset.name} is now active.`,
        color: 'success',
      });
      setExpanded(false);
      setKeys({});
      setApplyError(null);
      setConflict(null);
      onApplied();
    },
    onError: (error) => {
      const existing = existingPoliciesConflict(error);

      if (existing) {
        setConflict(existing);

        return;
      }

      setConflict(null);
      setApplyError(extractDetail(error, 'Failed to apply preset.'));
    },
  });

  function handleSetKey(slot: string, value: string) {
    setKeys((prev) => ({ ...prev, [slot]: value }));
  }

  const allFilled = preset.providers_needed.every(
    (slot) => (keys[slot] ?? '').trim().length > 0,
  );
  const teamScopeMissing = scope === 'team' && !teamId;
  const canSubmit = allFilled && !teamScopeMissing && !applyMutation.isPending;

  const affectedTaskLabels = React.useMemo(() => {
    const seen = new Set<string>();
    const labels: string[] = [];

    for (const tm of preset.task_models) {
      if (!seen.has(tm.task_type)) {
        seen.add(tm.task_type);
        labels.push(humanizeTask(tm.task_type));
      }
    }

    return labels;
  }, [preset.task_models]);

  const conflictCount = conflict?.policies_to_replace.length ?? 0;
  const conflictDescription = conflict
    ? `${conflictCount} active ${conflictCount === 1 ? 'policy' : 'policies'} in the ${scope} scope will be disabled and replaced for these task types: ${affectedTaskLabels.join(', ')}.`
    : undefined;

  return (
    <div className='surface-card overflow-hidden'>
      <div className='p-5'>
        <div className='flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between'>
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
              className='w-full shrink-0 sm:w-auto'
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

        {preset.task_models.length > 0 && (
          <div className='mt-3 space-y-1.5 rounded-[12px] border border-divider bg-content2/30 px-3.5 py-3'>
            <p className='text-[10px] font-semibold uppercase tracking-[0.1em] text-default-400'>
              Writes these policies
            </p>
            {preset.task_models.map((tm) => (
              <div
                key={tm.task_type}
                className='flex items-center justify-between gap-3 text-[12px]'
              >
                <span className='shrink-0 text-default-600'>
                  {humanizeTask(tm.task_type)}
                </span>
                <span
                  className='truncate font-mono text-[11.5px] text-default-500'
                  title={`${tm.provider} · ${tm.model}`}
                >
                  {tm.provider} · {tm.model}
                </span>
              </div>
            ))}
          </div>
        )}
      </div>

      {expanded && canApply && (
        <div className='border-t border-divider bg-content2/30 px-5 py-4 space-y-4'>
          <Select
            label='Apply to scope'
            labelPlacement='outside'
            size='sm'
            variant='bordered'
            selectedKeys={new Set([scope])}
            disallowEmptySelection
            isDisabled={applyMutation.isPending}
            onSelectionChange={(selection) => {
              const next = Array.from(selection)[0];

              if (typeof next === 'string') {
                setScope(next as PolicyScope);
              }
            }}
          >
            {PRESET_SCOPE_OPTIONS.map((option) => (
              <SelectItem key={option.key}>{option.label}</SelectItem>
            ))}
          </Select>
          {teamScopeMissing && (
            <p className='text-[12px] text-warning'>
              Select a team in the top switcher to apply a team-scoped preset.
            </p>
          )}
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
            isDisabled={!canSubmit}
            isLoading={applyMutation.isPending}
            onPress={() => applyMutation.mutate(false)}
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

      <ConfirmDialog
        isOpen={conflict !== null}
        title='Replace existing policies?'
        description={conflictDescription}
        confirmLabel='Replace policies'
        confirmColor='danger'
        isLoading={applyMutation.isPending}
        onClose={() => setConflict(null)}
        onConfirm={() => applyMutation.mutate(true)}
      />
    </div>
  );
}

function ConnectionTestResults({
  results,
}: {
  results: PolicyValidationResult[];
}) {
  if (results.length === 0) {

    return (
      <div className='surface-card px-5 py-4 text-[13px] text-default-500'>
        No active model policies to test yet.
      </div>
    );
  }

  const failed = results.filter((result) => !result.ok).length;

  return (
    <div className='surface-card space-y-3 p-5'>
      <div className='flex items-center gap-2'>
        {failed === 0 ? (
          <CheckCircle2 className='h-4 w-4 shrink-0 text-success' />
        ) : (
          <AlertTriangle className='h-4 w-4 shrink-0 text-warning' />
        )}
        <p className='text-[13px] font-medium text-foreground'>
          {failed === 0
            ? `All ${results.length} connection${results.length === 1 ? '' : 's'} passed`
            : `${failed} of ${results.length} connection${results.length === 1 ? '' : 's'} failed`}
        </p>
      </div>
      <div className='space-y-1.5'>
        {results.map((result) => (
          <div
            key={result.policy_id}
            className='flex items-center justify-between gap-3 rounded-[10px] border border-divider bg-content2/30 px-3.5 py-2.5'
          >
            <div className='min-w-0'>
              <p className='text-[12.5px] font-medium text-foreground'>
                {humanizeTask(result.task_type)}
              </p>
              <p className='truncate font-mono text-[11px] text-default-400'>
                {result.provider} · {result.model}
              </p>
            </div>
            {result.ok ? (
              <StatusPill
                status='ok'
                tone='success'
                label={`OK · ${result.latency_ms} ms`}
              />
            ) : (
              <div className='flex min-w-0 items-center gap-2'>
                <span className='hidden max-w-[240px] truncate text-[11px] text-danger sm:block'>
                  {result.public_error}
                </span>
                <StatusPill
                  status={result.error_code ?? 'failed'}
                  tone='danger'
                />
              </div>
            )}
          </div>
        ))}
      </div>
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

  const validateMutation = useMutation({
    mutationFn: () => validateModelPolicies(),
    onError: (error) => {
      addToast({
        title: 'Connection test failed',
        description: extractDetail(error, 'Could not run connection tests.'),
        color: 'danger',
      });
    },
  });

  if (!activeProjectId) {

    return (
      <section className='space-y-6'>
        <PageHeader
          title='Model Setup'
          subtitle='Configure which model serves each task type via presets.'
          actions={<ModelPoliciesLink />}
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
        actions={
          <div className='flex items-center gap-2'>
            {canManage && (
              <Button
                variant='flat'
                startContent={<Zap className='h-4 w-4' />}
                isLoading={validateMutation.isPending}
                onPress={() => validateMutation.mutate()}
              >
                Test connections
              </Button>
            )}
            <ModelPoliciesLink />
          </div>
        }
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

      {(validateMutation.isPending || validateMutation.data) && (
        <div>
          <h2 className='mb-3 text-[12px] font-semibold uppercase tracking-[0.1em] text-default-400'>
            Connection tests
          </h2>
          {validateMutation.isPending ? (
            <div className='surface-card h-16 animate-pulse bg-content2/50' />
          ) : validateMutation.data ? (
            <ConnectionTestResults results={validateMutation.data} />
          ) : null}
        </div>
      )}

      {!statusQuery.isError && (
        <>
          <div>
            <h2 className='mb-3 text-[12px] font-semibold uppercase tracking-[0.1em] text-default-400'>
              Task types
            </h2>
            <div className='grid gap-3 sm:grid-cols-2 lg:grid-cols-3'>
              {statusQuery.isLoading
                ? Array.from({ length: POLICY_TASK_TYPES.length }).map((_, i) => (
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
                    teamId={activeTeamId}
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
