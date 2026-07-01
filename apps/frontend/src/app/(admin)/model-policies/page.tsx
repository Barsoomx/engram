'use client';

import {
  addToast,
  Button,
  Input,
  Modal,
  ModalBody,
  ModalContent,
  ModalFooter,
  ModalHeader,
  Select,
  SelectItem,
} from '@heroui/react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import axios from 'axios';
import { AlertTriangle, Ban, Cpu, Eye, Plus, Sparkles } from 'lucide-react';
import * as React from 'react';

import { CapabilityGate } from '@/components/ui/capability-gate';
import { ConfirmDialog } from '@/components/ui/confirm-dialog';
import { EmptyState } from '@/components/ui/empty-state';
import { PageHeader } from '@/components/ui/page-header';
import { PrimaryButton } from '@/components/ui/primary-button';
import { PulseDot } from '@/components/ui/pulse-dot';
import { fetchMe, hasCapability, type MeResponse } from '@/lib/auth';
import {
  createModelPolicy,
  disableModelPolicy,
  genRequestId,
  listModelPolicies,
  listProviderSecrets,
  POLICY_TASK_TYPES,
  resolveModelPolicy,
  SECRET_PROVIDERS,
  type ModelPolicy,
  type ProviderSecret,
  type ModelPolicyCreateInput,
  type PolicyScope,
  type PolicyTaskType,
  type SecretProvider,
} from '@/lib/console-api';
import { useProjectStore } from '@/lib/project-store';
import { useTeamStore } from '@/lib/team-store';

const SCOPE_OPTIONS: { key: PolicyScope; label: string }[] = [
  { key: 'organization', label: 'Organization' },
  { key: 'team', label: 'Team' },
  { key: 'project', label: 'Project' },
];

const PROVIDER_LABELS: Record<SecretProvider, string> = {
  anthropic: 'Anthropic',
  openai: 'OpenAI',
  deepseek: 'DeepSeek',
};

const GRID =
  'minmax(0,1.3fr) minmax(0,0.9fr) minmax(0,0.8fr) minmax(0,1.2fr) minmax(0,0.8fr) minmax(0,1fr) auto';

function humanizeTask(value: string): string {
  return value
    .split('_')
    .filter(Boolean)
    .map((word) => word[0].toUpperCase() + word.slice(1))
    .join(' ');
}

function isNotFound(error: unknown): boolean {
  return axios.isAxiosError(error) && error.response?.status === 404;
}

function errorDetail(error: unknown, fallback: string): string {
  if (axios.isAxiosError(error)) {
    const data = error.response?.data as { detail?: string } | undefined;

    if (data?.detail) {

      return data.detail;
    }
  }

  return fallback;
}

function TaskPill({ task }: { task: PolicyTaskType }) {
  return (
    <span className='inline-flex max-w-full items-center truncate rounded-[7px] bg-primary-soft px-2.5 py-1 text-[11.5px] font-medium text-primary-300'>
      {humanizeTask(task)}
    </span>
  );
}

function ScopePill({ scope }: { scope: PolicyScope }) {
  return (
    <span className='inline-flex max-w-full items-center truncate rounded-[7px] bg-content3 px-2.5 py-1 text-[11.5px] font-medium text-default-500'>
      {humanizeTask(scope)}
    </span>
  );
}

function StatusCell({
  active,
  fallbackEnabled,
}: {
  active: boolean;
  fallbackEnabled: boolean;
}) {
  return (
    <div className='flex min-w-0 items-center gap-2'>
      <PulseDot color={active ? '#3DD9AC' : '#666C77'} pulse={active} />
      <span
        className={`text-[12px] font-medium ${active ? 'text-success' : 'text-default-500'}`}
      >
        {active ? 'Active' : 'Inactive'}
      </span>
      {fallbackEnabled && (
        <span className='truncate text-[11px] text-default-400'>· fallback</span>
      )}
    </div>
  );
}

function ColumnHeader() {
  return (
    <div
      className='grid items-center gap-4 border-b border-divider px-5 py-3 text-[10.5px] font-semibold uppercase tracking-[0.1em] text-default-400'
      style={{ gridTemplateColumns: GRID }}
    >
      <span>Name</span>
      <span>Task</span>
      <span>Provider</span>
      <span>Model</span>
      <span>Scope</span>
      <span>Status</span>
      <span className='sr-only'>Actions</span>
    </div>
  );
}

function PoliciesTable({
  items,
  onDisable,
  onSelect,
}: {
  items: ModelPolicy[];
  onDisable: (policy: ModelPolicy) => void;
  onSelect: (policy: ModelPolicy) => void;
}) {
  return (
    <div className='surface-card overflow-hidden'>
      <div className='overflow-x-auto'>
        <div className='min-w-[820px]'>
          <ColumnHeader />
          {items.map((policy) => (
            <div
              key={policy.id}
              className='grid items-center gap-4 border-b border-divider px-5 py-3.5 transition-colors last:border-b-0 hover:bg-content2/60'
              style={{ gridTemplateColumns: GRID }}
            >
              <div className='flex min-w-0 items-center gap-3'>
                <span className='inline-flex h-[30px] w-[30px] shrink-0 items-center justify-center rounded-[9px] bg-content3 text-primary-300'>
                  <Cpu className='h-[15px] w-[15px]' strokeWidth={1.8} />
                </span>
                <span className='truncate text-[13.5px] font-semibold text-foreground'>
                  {policy.name}
                </span>
              </div>
              <div className='min-w-0'>
                <TaskPill task={policy.task_type} />
              </div>
              <span className='truncate text-[12px] text-default-500'>
                {PROVIDER_LABELS[policy.provider] ?? policy.provider}
              </span>
              <span className='truncate font-mono text-[12px] text-default-500'>
                {policy.model}
              </span>
              <div className='min-w-0'>
                <ScopePill scope={policy.scope} />
              </div>
              <StatusCell
                active={policy.active}
                fallbackEnabled={policy.fallback_enabled}
              />
              <div className='flex items-center justify-end gap-2'>
                <Button
                  size='sm'
                  variant='flat'
                  startContent={<Eye className='h-3.5 w-3.5' />}
                  onPress={() => onSelect(policy)}
                >
                  View
                </Button>
                {policy.active && (
                  <Button
                    size='sm'
                    color='danger'
                    variant='flat'
                    startContent={<Ban className='h-3.5 w-3.5' />}
                    onPress={() => onDisable(policy)}
                  >
                    Disable
                  </Button>
                )}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function PoliciesTableSkeleton() {
  return (
    <div className='surface-card overflow-hidden'>
      <div className='overflow-x-auto'>
        <div className='min-w-[820px]'>
          <ColumnHeader />
          {Array.from({ length: 6 }).map((_, index) => (
            <div
              key={index}
              className='grid items-center gap-4 border-b border-divider px-5 py-3.5 last:border-b-0'
              style={{ gridTemplateColumns: GRID }}
            >
              <div className='flex min-w-0 items-center gap-3'>
                <span className='h-[30px] w-[30px] shrink-0 rounded-[9px] bg-content2' />
                <span className='h-3.5 w-28 rounded-medium bg-content2' />
              </div>
              <span className='h-5 w-20 rounded-[7px] bg-content2' />
              <span className='h-3 w-16 rounded-medium bg-content2' />
              <span className='h-3 w-32 rounded-medium bg-content2' />
              <span className='h-5 w-16 rounded-[7px] bg-content2' />
              <span className='h-3 w-16 rounded-medium bg-content2' />
              <div className='flex items-center justify-end gap-2'>
                <span className='h-8 w-14 rounded-medium bg-content2' />
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

interface CreatePolicyModalProps {
  isOpen: boolean;
  isPending: boolean;
  error: string | null;
  projectId: string | null;
  teamId: string | null;
  onClose: () => void;
  onSubmit: (input: {
    name: string;
    scope: PolicyScope;
    task_type: PolicyTaskType;
    provider: SecretProvider;
    model: string;
    secret_id: string;
    base_url?: string;
  }) => Promise<boolean>;
}

function CreatePolicyModal({
  isOpen,
  isPending,
  error,
  projectId,
  teamId,
  onClose,
  onSubmit,
}: CreatePolicyModalProps) {
  const [name, setName] = React.useState('');
  const [scope, setScope] = React.useState<PolicyScope>('organization');
  const [taskType, setTaskType] = React.useState<PolicyTaskType>('generation');
  const [provider, setProvider] = React.useState<SecretProvider>('anthropic');
  const [model, setModel] = React.useState('');
  const [secretId, setSecretId] = React.useState('');
  const [baseUrl, setBaseUrl] = React.useState('');

  React.useEffect(() => {
    if (!isOpen) {
      setName('');
      setScope('organization');
      setTaskType('generation');
      setProvider('anthropic');
      setModel('');
      setSecretId('');
      setBaseUrl('');
    }
  }, [isOpen]);

  const secretsQuery = useQuery({
    queryKey: ['model-policy', 'secrets', projectId, teamId],
    queryFn: () => listProviderSecrets({ projectId: projectId ?? '', teamId }),
    enabled: isOpen && Boolean(projectId),
  });

  const providerSecrets = React.useMemo(
    () =>
      (secretsQuery.data ?? []).filter(
        (secret) => secret.provider === provider && secret.active,
      ),
    [secretsQuery.data, provider],
  );

  React.useEffect(() => {
    setSecretId('');
  }, [provider]);

  const canSubmit =
    name.trim().length > 0 &&
    model.trim().length > 0 &&
    secretId.trim().length > 0 &&
    !isPending;

  async function handleSubmit() {
    if (!canSubmit) {

      return;
    }

    const ok = await onSubmit({
      name: name.trim(),
      scope,
      task_type: taskType,
      provider,
      model: model.trim(),
      secret_id: secretId.trim(),
      base_url: baseUrl.trim() || undefined,
    });

    if (ok) {
      onClose();
    }
  }

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      placement='center'
      size='lg'
      isDismissable={!isPending}
      hideCloseButton={isPending}
    >
      <ModalContent>
        {() => (
          <>
            <ModalHeader className='flex flex-col gap-1 text-foreground'>
              New model policy
            </ModalHeader>
            <ModalBody>
              <div className='space-y-4'>
                <Input
                  label='Name'
                  labelPlacement='outside'
                  placeholder='Primary generation'
                  value={name}
                  onValueChange={setName}
                  maxLength={255}
                  isDisabled={isPending}
                />
                <div className='grid gap-4 sm:grid-cols-2'>
                  <Select
                    label='Scope'
                    labelPlacement='outside'
                    selectedKeys={new Set([scope])}
                    isDisabled={isPending}
                    disallowEmptySelection
                    onSelectionChange={(keys) => {
                      const next = Array.from(keys)[0];

                      if (typeof next === 'string') {
                        setScope(next as PolicyScope);
                      }
                    }}
                  >
                    {SCOPE_OPTIONS.map((option) => (
                      <SelectItem key={option.key}>{option.label}</SelectItem>
                    ))}
                  </Select>
                  <Select
                    label='Task type'
                    labelPlacement='outside'
                    selectedKeys={new Set([taskType])}
                    isDisabled={isPending}
                    disallowEmptySelection
                    onSelectionChange={(keys) => {
                      const next = Array.from(keys)[0];

                      if (typeof next === 'string') {
                        setTaskType(next as PolicyTaskType);
                      }
                    }}
                  >
                    {POLICY_TASK_TYPES.map((task) => (
                      <SelectItem key={task}>{humanizeTask(task)}</SelectItem>
                    ))}
                  </Select>
                  <Select
                    label='Provider'
                    labelPlacement='outside'
                    selectedKeys={new Set([provider])}
                    isDisabled={isPending}
                    disallowEmptySelection
                    onSelectionChange={(keys) => {
                      const next = Array.from(keys)[0];

                      if (typeof next === 'string') {
                        setProvider(next as SecretProvider);
                      }
                    }}
                  >
                    {SECRET_PROVIDERS.map((value) => (
                      <SelectItem key={value}>
                        {PROVIDER_LABELS[value]}
                      </SelectItem>
                    ))}
                  </Select>
                  <Input
                    label='Model'
                    labelPlacement='outside'
                    placeholder='claude-sonnet-4'
                    value={model}
                    onValueChange={setModel}
                    isDisabled={isPending}
                    classNames={{ input: 'font-mono text-xs' }}
                  />
                </div>
                <Select
                  label='Provider secret'
                  labelPlacement='outside'
                  items={providerSecrets}
                  selectedKeys={secretId ? new Set([secretId]) : new Set()}
                  isDisabled={isPending || providerSecrets.length === 0}
                  description={
                    providerSecrets.length === 0
                      ? `No active ${PROVIDER_LABELS[provider]} secret — add one on the Secrets page first.`
                      : 'Choose an existing provider secret for the selected provider.'
                  }
                  onSelectionChange={(keys) => {
                    const next = Array.from(keys)[0];

                    if (typeof next === 'string') {
                      setSecretId(next);
                    }
                  }}
                >
                  {(secret: ProviderSecret) => (
                    <SelectItem key={secret.id}>{secret.name}</SelectItem>
                  )}
                </Select>
                <Input
                  label='Base URL (optional)'
                  labelPlacement='outside'
                  placeholder='https://api.deepseek.com/v1 or https://open.bigmodel.cn/api/paas/v4 (leave blank for default)'
                  value={baseUrl}
                  onValueChange={setBaseUrl}
                  isDisabled={isPending}
                  description='For GLM / self-hosted / OpenAI-compatible endpoints. Leave blank to use the provider default.'
                  classNames={{ input: 'font-mono text-xs' }}
                />
                {error && (
                  <div className='rounded-medium border border-danger-200 bg-danger-50 p-3 dark:border-danger-500/30 dark:bg-danger-500/10'>
                    <p className='text-sm text-danger-600'>{error}</p>
                  </div>
                )}
              </div>
            </ModalBody>
            <ModalFooter>
              <Button
                color='default'
                variant='light'
                onPress={onClose}
                isDisabled={isPending}
              >
                Cancel
              </Button>
              <Button
                color='primary'
                onPress={handleSubmit}
                isDisabled={!canSubmit}
                isLoading={isPending}
              >
                Create policy
              </Button>
            </ModalFooter>
          </>
        )}
      </ModalContent>
    </Modal>
  );
}

type ResolveOutcome =
  | { kind: 'policy'; policy: ModelPolicy }
  | { kind: 'none' }
  | { kind: 'error'; message: string };

function ResolveTester({
  taskType,
  onTaskTypeChange,
  outcome,
  isPending,
  onResolve,
}: {
  taskType: PolicyTaskType;
  onTaskTypeChange: (task: PolicyTaskType) => void;
  outcome: ResolveOutcome | null;
  isPending: boolean;
  onResolve: () => void;
}) {
  return (
    <div className='surface-card space-y-4 p-[22px]'>
      <div className='flex items-start gap-3'>
        <span className='inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-[10px] bg-primary-soft text-primary-300'>
          <Sparkles className='h-[18px] w-[18px]' strokeWidth={1.8} />
        </span>
        <div className='min-w-0 space-y-1'>
          <h3 className='text-[14.5px] font-semibold text-foreground'>
            Resolve tester
          </h3>
          <p className='text-[12.5px] leading-relaxed text-default-500'>
            Check which policy serves a task type in the current scope.
          </p>
        </div>
      </div>

      <div className='flex flex-col gap-3 sm:flex-row sm:items-end'>
        <Select
          label='Task type'
          labelPlacement='outside'
          className='sm:max-w-xs'
          selectedKeys={new Set([taskType])}
          isDisabled={isPending}
          disallowEmptySelection
          onSelectionChange={(keys) => {
            const next = Array.from(keys)[0];

            if (typeof next === 'string') {
              onTaskTypeChange(next as PolicyTaskType);
            }
          }}
        >
          {POLICY_TASK_TYPES.map((task) => (
            <SelectItem key={task}>{humanizeTask(task)}</SelectItem>
          ))}
        </Select>
        <Button color='primary' onPress={onResolve} isLoading={isPending}>
          Resolve
        </Button>
      </div>

      {outcome?.kind === 'policy' && (
        <div className='rounded-[14px] border border-divider bg-content2/50 px-4 py-3.5'>
          <div className='flex items-center justify-between gap-3'>
            <span className='truncate text-[13.5px] font-semibold text-foreground'>
              {outcome.policy.name}
            </span>
            <TaskPill task={outcome.policy.task_type} />
          </div>
          <div className='mt-3 grid grid-cols-2 gap-x-4 gap-y-2.5 sm:grid-cols-4'>
            <ResolveField label='Provider'>
              {PROVIDER_LABELS[outcome.policy.provider] ?? outcome.policy.provider}
            </ResolveField>
            <ResolveField label='Model' mono>
              {outcome.policy.model}
            </ResolveField>
            <ResolveField label='Version' mono>
              {`v${outcome.policy.version}`}
            </ResolveField>
            <ResolveField label='Scope'>
              {humanizeTask(outcome.policy.scope)}
            </ResolveField>
          </div>
        </div>
      )}

      {outcome?.kind === 'none' && (
        <div className='rounded-[14px] border border-divider bg-content2/40 px-4 py-3.5 text-[13px] text-default-500'>
          No policy resolves for{' '}
          <span className='font-medium text-default-700'>
            {humanizeTask(taskType)}
          </span>{' '}
          in the current scope.
        </div>
      )}

      {outcome?.kind === 'error' && (
        <div className='flex items-start gap-3 rounded-[14px] border border-danger/30 bg-danger/5 px-4 py-3.5'>
          <AlertTriangle className='mt-0.5 h-4 w-4 shrink-0 text-danger' />
          <p className='text-[13px] leading-relaxed text-danger'>
            {outcome.message}
          </p>
        </div>
      )}
    </div>
  );
}

function ResolveField({
  label,
  mono,
  children,
}: {
  label: string;
  mono?: boolean;
  children: React.ReactNode;
}) {
  return (
    <div className='min-w-0 space-y-1'>
      <p className='text-[10px] font-semibold uppercase tracking-[0.12em] text-default-400'>
        {label}
      </p>
      <p
        className={`truncate text-[12.5px] text-default-700 ${mono ? 'font-mono' : ''}`}
      >
        {children}
      </p>
    </div>
  );
}

function PolicyDetailModal({
  isOpen,
  policy,
  onClose,
}: {
  isOpen: boolean;
  policy: ModelPolicy | null;
  onClose: () => void;
}) {
  return (
    <Modal isOpen={isOpen} onClose={onClose} placement='center' size='lg'>
      <ModalContent>
        {() => (
          <>
            <ModalHeader className='flex flex-col gap-1 text-foreground'>
              {policy?.name ?? 'Policy details'}
            </ModalHeader>
            <ModalBody>
              {policy && (
                <div className='space-y-4'>
                  <div className='flex flex-wrap items-center gap-2'>
                    <TaskPill task={policy.task_type} />
                    <ScopePill scope={policy.scope} />
                    <StatusCell
                      active={policy.active}
                      fallbackEnabled={policy.fallback_enabled}
                    />
                  </div>
                  <div className='grid grid-cols-2 gap-x-4 gap-y-3'>
                    <ResolveField label='Provider'>
                      {PROVIDER_LABELS[policy.provider] ?? policy.provider}
                    </ResolveField>
                    <ResolveField label='Model' mono>
                      {policy.model}
                    </ResolveField>
                    <ResolveField label='Version' mono>
                      {`v${policy.version}`}
                    </ResolveField>
                    <ResolveField label='Secret ID' mono>
                      {policy.secret_id}
                    </ResolveField>
                    {policy.project_id && (
                      <ResolveField label='Project ID' mono>
                        {policy.project_id}
                      </ResolveField>
                    )}
                    {policy.team_id && (
                      <ResolveField label='Team ID' mono>
                        {policy.team_id}
                      </ResolveField>
                    )}
                    <ResolveField label='Policy ID' mono>
                      {policy.id}
                    </ResolveField>
                  </div>
                </div>
              )}
            </ModalBody>
            <ModalFooter>
              <Button color='default' variant='light' onPress={onClose}>
                Close
              </Button>
            </ModalFooter>
          </>
        )}
      </ModalContent>
    </Modal>
  );
}

export default function ModelPoliciesPage() {
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
  const canManagePolicies = hasCapability(capabilities, 'model_policy:*');

  const queryClient = useQueryClient();

  const policiesQuery = useQuery<ModelPolicy[]>({
    queryKey: ['model-policy', 'policies', activeProjectId, activeTeamId],
    enabled: Boolean(activeProjectId),
    queryFn: async () => {
      try {
        return await listModelPolicies({
          projectId: activeProjectId ?? '',
          teamId: activeTeamId,
        });
      } catch (error) {
        if (isNotFound(error)) {

          return [];
        }

        throw error;
      }
    },
  });

  const [createOpen, setCreateOpen] = React.useState(false);
  const [createError, setCreateError] = React.useState<string | null>(null);

  const createMutation = useMutation({
    mutationFn: (input: ModelPolicyCreateInput) => createModelPolicy(input),
    onSuccess: (policy) => {
      queryClient.invalidateQueries({ queryKey: ['model-policy', 'policies'] });
      addToast({
        title: 'Policy created',
        description: `${policy.name} now serves ${humanizeTask(policy.task_type)}.`,
        color: 'success',
      });
      setCreateOpen(false);
    },
  });

  const [resolveTask, setResolveTask] =
    React.useState<PolicyTaskType>('generation');
  const [resolveOutcome, setResolveOutcome] =
    React.useState<ResolveOutcome | null>(null);

  const resolveMutation = useMutation({
    mutationFn: (taskType: PolicyTaskType) =>
      resolveModelPolicy({
        project_id: activeProjectId ?? '',
        team_id: activeTeamId,
        task_type: taskType,
      }),
    onSuccess: (policy) => {
      setResolveOutcome({ kind: 'policy', policy });
    },
    onError: (error) => {
      if (isNotFound(error)) {
        setResolveOutcome({ kind: 'none' });

        return;
      }

      setResolveOutcome({
        kind: 'error',
        message: errorDetail(error, 'Failed to resolve policy.'),
      });
    },
  });

  const [detailPolicy, setDetailPolicy] = React.useState<ModelPolicy | null>(
    null,
  );
  const [disablePolicyTarget, setDisablePolicyTarget] =
    React.useState<ModelPolicy | null>(null);

  const disablePolicyMutation = useMutation({
    mutationFn: (policyId: string) =>
      disableModelPolicy(policyId, {
        project_id: activeProjectId ?? '',
        team_id: activeTeamId ?? null,
        request_id: genRequestId(),
      }),
  });

  async function handleDisablePolicy() {
    if (!disablePolicyTarget) {

      return;
    }

    try {
      await disablePolicyMutation.mutateAsync(disablePolicyTarget.id);
      queryClient.invalidateQueries({ queryKey: ['model-policy', 'policies'] });
      addToast({ title: 'Policy disabled', color: 'success' });
      setDisablePolicyTarget(null);
    } catch (error) {
      addToast({
        title: 'Failed to disable policy',
        description: errorDetail(error, 'Unexpected error.'),
        color: 'danger',
      });
      setDisablePolicyTarget(null);
    }
  }

  async function handleCreate(input: {
    name: string;
    scope: PolicyScope;
    task_type: PolicyTaskType;
    provider: SecretProvider;
    model: string;
    secret_id: string;
    base_url?: string;
  }): Promise<boolean> {
    setCreateError(null);

    try {
      await createMutation.mutateAsync({
        project_id: activeProjectId ?? '',
        team_id: activeTeamId,
        name: input.name,
        scope: input.scope,
        task_type: input.task_type,
        provider: input.provider,
        model: input.model,
        secret_id: input.secret_id,
        request_id: genRequestId(),
        base_url: input.base_url,
      });

      return true;
    } catch (error) {
      setCreateError(errorDetail(error, 'Failed to create policy.'));

      return false;
    }
  }

  function openCreate() {
    setCreateError(null);
    setCreateOpen(true);
  }

  const meLoaded = meQuery.data !== undefined;
  const isLoading = meQuery.isLoading || policiesQuery.isLoading;
  const items = policiesQuery.data ?? [];

  return (
    <CapabilityGate capabilities={capabilities} required='model_policy:read'>
      {!activeProjectId ? (
        <section className='space-y-6'>
          <PageHeader
            title='Model Policies'
            subtitle='Which model serves each task type.'
          />
          <EmptyState
            title='Select a project'
            description='Choose a project from the switcher above to manage its model policies.'
            icon={<Cpu className='h-6 w-6' />}
          />
        </section>
      ) : (
        <section className='space-y-6'>
          <PageHeader
            title='Model Policies'
            subtitle='Which model serves each task type.'
            actions={
              canManagePolicies ? (
                <PrimaryButton
                  startContent={<Plus className='h-4 w-4' />}
                  onPress={openCreate}
                  isDisabled={!meLoaded}
                >
                  New policy
                </PrimaryButton>
              ) : undefined
            }
          />

          {isLoading ? (
            <PoliciesTableSkeleton />
          ) : policiesQuery.isError ? (
            <div className='flex items-start gap-3 rounded-[16px] border border-danger/30 bg-danger/5 px-5 py-4'>
              <AlertTriangle className='mt-0.5 h-5 w-5 shrink-0 text-danger' />
              <p className='text-[13px] leading-relaxed text-danger'>
                {policiesQuery.error instanceof Error
                  ? policiesQuery.error.message
                  : 'Failed to load model policies.'}
              </p>
            </div>
          ) : items.length === 0 ? (
            <EmptyState
              title='No model policies yet'
              description='Create a policy to route a task type to a specific provider and model.'
              icon={<Cpu className='h-6 w-6' />}
              action={
                canManagePolicies ? (
                  <PrimaryButton
                    startContent={<Plus className='h-4 w-4' />}
                    onPress={openCreate}
                  >
                    New policy
                  </PrimaryButton>
                ) : undefined
              }
            />
          ) : (
            <PoliciesTable
              items={items}
              onDisable={setDisablePolicyTarget}
              onSelect={setDetailPolicy}
            />
          )}

          {items.length > 0 && (
            <p className='text-[12px] text-default-400'>
              Showing {items.length} polic{items.length === 1 ? 'y' : 'ies'}.
            </p>
          )}

          <ResolveTester
            taskType={resolveTask}
            onTaskTypeChange={(task) => {
              setResolveTask(task);
              setResolveOutcome(null);
            }}
            outcome={resolveOutcome}
            isPending={resolveMutation.isPending}
            onResolve={() => resolveMutation.mutate(resolveTask)}
          />

          <CreatePolicyModal
            isOpen={createOpen}
            isPending={createMutation.isPending}
            error={createError}
            projectId={activeProjectId}
            teamId={activeTeamId}
            onClose={() => setCreateOpen(false)}
            onSubmit={handleCreate}
          />

          <PolicyDetailModal
            isOpen={detailPolicy !== null}
            policy={detailPolicy}
            onClose={() => setDetailPolicy(null)}
          />

          <ConfirmDialog
            isOpen={disablePolicyTarget !== null}
            title='Disable policy'
            description={
              disablePolicyTarget
                ? `Disable "${disablePolicyTarget.name}"? Requests routing to this task type will fall through to the next policy or fail.`
                : undefined
            }
            confirmLabel='Disable'
            confirmColor='danger'
            isLoading={disablePolicyMutation.isPending}
            onClose={() => setDisablePolicyTarget(null)}
            onConfirm={handleDisablePolicy}
          />
        </section>
      )}
    </CapabilityGate>
  );
}
