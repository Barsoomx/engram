'use client';

import {
  addToast,
  Button,
  Checkbox,
  Input,
  Modal,
  ModalBody,
  ModalContent,
  ModalFooter,
  ModalHeader,
  Pagination,
  Select,
  SelectItem,
} from '@heroui/react';
import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
} from '@tanstack/react-query';
import axios from 'axios';
import { AlertTriangle, ArrowRight, Ban, Cpu, Eye, Plus, Sparkles } from 'lucide-react';
import Link from 'next/link';
import * as React from 'react';

import { CapabilityGate } from '@/components/ui/capability-gate';
import { ConfirmDialog } from '@/components/ui/confirm-dialog';
import { EmptyState } from '@/components/ui/empty-state';
import { ErrorState } from '@/components/ui/error-state';
import { PageHeader } from '@/components/ui/page-header';
import { PrimaryButton } from '@/components/ui/primary-button';
import { PulseDot } from '@/components/ui/pulse-dot';
import { ResponsiveTable } from '@/components/ui/responsive-table';
import { TableRowSkeleton } from '@/components/ui/table-row-skeleton';
import { useUrlFilters } from '@/hooks/use-url-filters';
import { extractApiError } from '@/lib/api-error';
import { fetchMe, hasCapability, type MeResponse } from '@/lib/auth';
import { formatRelativeTime } from '@/lib/design';
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

const PAGE_SIZE = 50;

const MODEL_POLICY_FILTERS: {
  task_type: string;
  provider: string;
  scope: string;
  active: string;
  page: number;
} = {
  task_type: '',
  provider: '',
  scope: '',
  active: '',
  page: 1,
};

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
  return extractApiError(error, fallback);
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

function HealthBadge({
  lastSuccessAt,
  recentErrorCount,
}: {
  lastSuccessAt: string | null | undefined;
  recentErrorCount: number | undefined;
}) {
  const errorCount = recentErrorCount ?? 0;

  if (errorCount > 0) {
    return (
      <span className='inline-flex max-w-full items-center gap-1.5 truncate rounded-[7px] bg-danger/10 px-2.5 py-1 text-[11px] font-medium text-danger'>
        <PulseDot color='#FB6E72' pulse />
        {`failing · ${errorCount} error${errorCount === 1 ? '' : 's'}`}
      </span>
    );
  }

  if (!lastSuccessAt) {
    return (
      <span className='inline-flex max-w-full items-center gap-1.5 truncate rounded-[7px] bg-content3 px-2.5 py-1 text-[11px] font-medium text-default-400'>
        <PulseDot color='#666C77' pulse={false} />
        never succeeded
      </span>
    );
  }

  return (
    <span className='inline-flex max-w-full items-center gap-1.5 truncate rounded-[7px] bg-success/10 px-2.5 py-1 text-[11px] font-medium text-success'>
      <PulseDot color='#3DD9AC' pulse={false} />
      {`ok · last success ${formatRelativeTime(lastSuccessAt)}`}
    </span>
  );
}

const TASK_FILTER_OPTIONS: { key: string; label: string }[] = [
  { key: '', label: 'All tasks' },
  ...POLICY_TASK_TYPES.map((task) => ({ key: task, label: humanizeTask(task) })),
];

const PROVIDER_FILTER_OPTIONS: { key: string; label: string }[] = [
  { key: '', label: 'All providers' },
  ...SECRET_PROVIDERS.map((provider) => ({
    key: provider,
    label: PROVIDER_LABELS[provider],
  })),
];

const SCOPE_FILTER_OPTIONS: { key: string; label: string }[] = [
  { key: '', label: 'All scopes' },
  ...SCOPE_OPTIONS.map((option) => ({ key: option.key, label: option.label })),
];

const STATUS_FILTER_OPTIONS: { key: string; label: string }[] = [
  { key: '', label: 'All statuses' },
  { key: 'true', label: 'Active' },
  { key: 'false', label: 'Inactive' },
];

const ALL_FILTER_KEY = '__all__';

function FilterSelect({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: { key: string; label: string }[];
  onChange: (value: string) => void;
}) {
  const selectedKey = value === '' ? ALL_FILTER_KEY : value;

  return (
    <Select
      label={label}
      labelPlacement='outside'
      size='sm'
      variant='bordered'
      className='max-w-[200px]'
      selectedKeys={new Set([selectedKey])}
      disallowEmptySelection
      onSelectionChange={(keys) => {
        const next = Array.from(keys)[0];

        if (typeof next === 'string') {
          onChange(next === ALL_FILTER_KEY ? '' : next);
        }
      }}
    >
      {options.map((option) => (
        <SelectItem key={option.key === '' ? ALL_FILTER_KEY : option.key}>
          {option.label}
        </SelectItem>
      ))}
    </Select>
  );
}

function PolicyTableHead() {
  return (
    <thead>
      <tr className='border-b border-divider text-[10.5px] uppercase tracking-[0.1em]'>
        <th className='px-3 py-2.5 text-left font-semibold text-default-400'>
          Name
        </th>
        <th className='px-3 py-2.5 text-left font-semibold text-default-400'>
          Task
        </th>
        <th className='px-3 py-2.5 text-left font-semibold text-default-400'>
          Provider
        </th>
        <th className='px-3 py-2.5 text-left font-semibold text-default-400'>
          Model
        </th>
        <th className='px-3 py-2.5 text-left font-semibold text-default-400'>
          Scope
        </th>
        <th className='px-3 py-2.5 text-left font-semibold text-default-400'>
          Status
        </th>
        <th className='px-3 py-2.5 text-left font-semibold text-default-400'>
          Health
        </th>
        <th className='px-3 py-2.5 text-right font-semibold text-default-400'>
          <span className='sr-only'>Actions</span>
        </th>
      </tr>
    </thead>
  );
}

function PoliciesTable({
  items,
  canManage,
  onDisable,
  onSelect,
}: {
  items: ModelPolicy[];
  canManage: boolean;
  onDisable: (policy: ModelPolicy) => void;
  onSelect: (policy: ModelPolicy) => void;
}) {
  return (
    <div className='surface-card p-2'>
      <ResponsiveTable minWidth={980}>
        <PolicyTableHead />
        <tbody>
          {items.map((policy) => (
            <tr
              key={policy.id}
              className='border-b border-divider/50 transition-colors last:border-b-0 hover:bg-content2/40'
            >
              <td className='px-3 py-2.5'>
                <div className='flex items-center gap-3'>
                  <span className='inline-flex h-[30px] w-[30px] shrink-0 items-center justify-center rounded-[9px] bg-content3 text-primary-300'>
                    <Cpu className='h-[15px] w-[15px]' strokeWidth={1.8} />
                  </span>
                  <span
                    className='block max-w-[220px] truncate text-[13.5px] font-semibold text-foreground'
                    title={policy.name}
                  >
                    {policy.name}
                  </span>
                </div>
              </td>
              <td className='px-3 py-2.5'>
                <TaskPill task={policy.task_type} />
              </td>
              <td className='px-3 py-2.5 text-[12px] text-default-500'>
                {PROVIDER_LABELS[policy.provider] ?? policy.provider}
              </td>
              <td className='px-3 py-2.5'>
                <div className='flex flex-col gap-1'>
                  <div className='flex items-center gap-2'>
                    <span
                      className='block max-w-[260px] truncate font-mono text-[12px] text-default-500'
                      title={policy.model}
                    >
                      {policy.model}
                    </span>
                    {policy.json_mode && (
                      <span
                        className='shrink-0 rounded-[6px] bg-primary-soft px-1.5 py-0.5 text-[10px] font-semibold text-primary-300'
                        title='Sends response_format: json_object'
                      >
                        JSON
                      </span>
                    )}
                  </div>
                  {policy.base_url && (
                    <span
                      className='block max-w-[260px] truncate font-mono text-[10.5px] text-default-400'
                      title={policy.base_url}
                    >
                      {policy.base_url}
                    </span>
                  )}
                </div>
              </td>
              <td className='px-3 py-2.5'>
                <ScopePill scope={policy.scope} />
              </td>
              <td className='px-3 py-2.5'>
                <StatusCell
                  active={policy.active}
                  fallbackEnabled={policy.fallback_enabled}
                />
              </td>
              <td className='px-3 py-2.5'>
                <HealthBadge
                  lastSuccessAt={policy.last_success_at}
                  recentErrorCount={policy.recent_error_count}
                />
              </td>
              <td className='px-3 py-2.5'>
                <div className='flex items-center justify-end gap-2'>
                  <Button
                    size='sm'
                    variant='flat'
                    startContent={<Eye className='h-3.5 w-3.5' />}
                    onPress={() => onSelect(policy)}
                  >
                    View
                  </Button>
                  {canManage && policy.active && (
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
              </td>
            </tr>
          ))}
        </tbody>
      </ResponsiveTable>
    </div>
  );
}

function PoliciesTableSkeleton() {
  return (
    <div className='surface-card p-2'>
      <ResponsiveTable minWidth={980}>
        <PolicyTableHead />
        <TableRowSkeleton columns={8} rows={6} />
      </ResponsiveTable>
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
    context_window_tokens?: number;
    fallback_enabled: boolean;
    json_mode?: boolean;
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
  const [contextWindowTokens, setContextWindowTokens] = React.useState('');
  const [fallbackEnabled, setFallbackEnabled] = React.useState(false);
  const [jsonMode, setJsonMode] = React.useState(false);

  React.useEffect(() => {
    if (!isOpen) {
      setName('');
      setScope('organization');
      setTaskType('generation');
      setProvider('anthropic');
      setModel('');
      setSecretId('');
      setBaseUrl('');
      setContextWindowTokens('');
      setFallbackEnabled(false);
      setJsonMode(false);
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
    (scope !== 'team' || Boolean(teamId)) &&
    !isPending;

  async function handleSubmit() {
    if (!canSubmit) {

      return;
    }

    const parsedContextWindowTokens = contextWindowTokens.trim()
      ? Number(contextWindowTokens.trim())
      : undefined;

    const ok = await onSubmit({
      name: name.trim(),
      scope,
      task_type: taskType,
      provider,
      model: model.trim(),
      secret_id: secretId.trim(),
      base_url: baseUrl.trim() || undefined,
      context_window_tokens:
        parsedContextWindowTokens !== undefined &&
        Number.isFinite(parsedContextWindowTokens)
          ? parsedContextWindowTokens
          : undefined,
      fallback_enabled: fallbackEnabled,
      json_mode: jsonMode ? true : undefined,
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
                    placeholder='claude-sonnet-5'
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
                  placeholder='https://api.deepseek.com/v1 or https://api.z.ai/api/paas/v4 (leave blank for default)'
                  value={baseUrl}
                  onValueChange={setBaseUrl}
                  isDisabled={isPending}
                  description='For GLM / self-hosted / OpenAI-compatible endpoints. Leave blank to use the provider default.'
                  classNames={{ input: 'font-mono text-xs' }}
                />
                <Input
                  type='number'
                  label='Context window (tokens)'
                  labelPlacement='outside'
                  placeholder='200000'
                  value={contextWindowTokens}
                  onValueChange={setContextWindowTokens}
                  isDisabled={isPending}
                  min={1}
                  step={1000}
                  description='Optional override of the model context window used to size distillation chunks; leave blank to auto-detect.'
                  classNames={{ input: 'font-mono text-xs' }}
                />
                <div className='space-y-2.5'>
                  <Checkbox
                    isSelected={fallbackEnabled}
                    onValueChange={setFallbackEnabled}
                    isDisabled={isPending}
                  >
                    <span className='text-[13px] text-foreground'>
                      Enable provider fallback to the generation policy on
                      failure
                    </span>
                  </Checkbox>
                  <Checkbox
                    isSelected={jsonMode}
                    onValueChange={setJsonMode}
                    isDisabled={isPending}
                  >
                    <span className='text-[13px] text-foreground'>
                      Send response_format: json_object (JSON-mode capable
                      models only)
                    </span>
                  </Checkbox>
                </div>
                {scope === 'team' && !teamId && (
                  <p className='text-[12px] text-warning'>
                    Select a team in the top switcher to create a team-scoped
                    policy.
                  </p>
                )}
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
            <ResolveField label='Context window' mono>
              {outcome.policy.context_window_tokens
                ? `${outcome.policy.context_window_tokens} tokens`
                : 'Auto-detected'}
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
                    <ResolveField label='Context window' mono>
                      {policy.context_window_tokens
                        ? `${policy.context_window_tokens} tokens`
                        : 'Auto-detected'}
                    </ResolveField>
                    <ResolveField label='JSON mode'>
                      {policy.json_mode ? 'Enabled' : 'Disabled'}
                    </ResolveField>
                    <ResolveField label='Base URL' mono>
                      {policy.base_url || 'Provider default'}
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

  const [filters, setFilters, resetFilters] = useUrlFilters(MODEL_POLICY_FILTERS);

  const scopeKey = `${activeProjectId ?? ''}|${activeTeamId ?? ''}`;
  const prevScopeKey = React.useRef(scopeKey);

  React.useEffect(() => {
    if (prevScopeKey.current !== scopeKey) {
      prevScopeKey.current = scopeKey;
      setFilters({ page: 1 });
    }
  }, [scopeKey, setFilters]);

  const activeFilter =
    filters.active === 'true'
      ? true
      : filters.active === 'false'
        ? false
        : undefined;

  const queryClient = useQueryClient();

  const policiesQuery = useQuery<{ count: number; items: ModelPolicy[] }>({
    queryKey: [
      'model-policy',
      'policies',
      activeProjectId,
      activeTeamId,
      filters.task_type,
      filters.provider,
      filters.scope,
      filters.active,
      filters.page,
    ],
    enabled: Boolean(activeProjectId),
    placeholderData: keepPreviousData,
    queryFn: async () => {
      try {
        return await listModelPolicies({
          projectId: activeProjectId ?? '',
          teamId: activeTeamId,
          task_type: (filters.task_type || undefined) as
            | PolicyTaskType
            | undefined,
          provider: (filters.provider || undefined) as
            | SecretProvider
            | undefined,
          scope: (filters.scope || undefined) as PolicyScope | undefined,
          active: activeFilter,
          limit: PAGE_SIZE,
          offset: (filters.page - 1) * PAGE_SIZE,
        });
      } catch (error) {
        if (isNotFound(error)) {

          return { count: 0, items: [] };
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
    context_window_tokens?: number;
    fallback_enabled: boolean;
    json_mode?: boolean;
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
        context_window_tokens: input.context_window_tokens,
        fallback_enabled: input.fallback_enabled,
        json_mode: input.json_mode,
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
  const items = policiesQuery.data?.items ?? [];
  const total = policiesQuery.data?.count ?? 0;
  const hasActiveFilters = Boolean(
    filters.task_type || filters.provider || filters.scope || filters.active,
  );
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

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
              <div className='flex items-center gap-2'>
                <Link
                  href='/model-setup'
                  className='inline-flex items-center gap-1.5 rounded-[9px] border border-divider bg-content1 px-3 py-1.5 text-[12.5px] font-medium text-default-600 transition-colors hover:text-foreground'
                >
                  Model setup
                  <ArrowRight size={14} strokeWidth={1.9} />
                </Link>
                {canManagePolicies && (
                  <PrimaryButton
                    startContent={<Plus className='h-4 w-4' />}
                    onPress={openCreate}
                    isDisabled={!meLoaded}
                  >
                    New policy
                  </PrimaryButton>
                )}
              </div>
            }
          />

          <div className='surface-card flex flex-wrap items-end gap-3 p-4'>
            <FilterSelect
              label='Task type'
              value={filters.task_type}
              options={TASK_FILTER_OPTIONS}
              onChange={(value) => setFilters({ task_type: value, page: 1 })}
            />
            <FilterSelect
              label='Provider'
              value={filters.provider}
              options={PROVIDER_FILTER_OPTIONS}
              onChange={(value) => setFilters({ provider: value, page: 1 })}
            />
            <FilterSelect
              label='Scope'
              value={filters.scope}
              options={SCOPE_FILTER_OPTIONS}
              onChange={(value) => setFilters({ scope: value, page: 1 })}
            />
            <FilterSelect
              label='Status'
              value={filters.active}
              options={STATUS_FILTER_OPTIONS}
              onChange={(value) => setFilters({ active: value, page: 1 })}
            />
            {hasActiveFilters && (
              <Button size='sm' variant='light' onPress={() => resetFilters()}>
                Clear filters
              </Button>
            )}
          </div>

          {isLoading ? (
            <PoliciesTableSkeleton />
          ) : policiesQuery.isError ? (
            <ErrorState
              message={
                policiesQuery.error instanceof Error
                  ? policiesQuery.error.message
                  : 'Failed to load model policies.'
              }
              onRetry={() => policiesQuery.refetch()}
            />
          ) : items.length === 0 ? (
            hasActiveFilters ? (
              <EmptyState
                title='No matching policies'
                description='No model policies match the current filters.'
                icon={<Cpu className='h-6 w-6' />}
                action={
                  <Button size='sm' variant='flat' onPress={() => resetFilters()}>
                    Clear filters
                  </Button>
                }
              />
            ) : (
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
            )
          ) : (
            <PoliciesTable
              items={items}
              canManage={canManagePolicies}
              onDisable={setDisablePolicyTarget}
              onSelect={setDetailPolicy}
            />
          )}

          {total > 0 && (
            <div className='flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between'>
              <p className='text-xs text-default-500'>
                Showing {(filters.page - 1) * PAGE_SIZE + 1}–
                {Math.min(filters.page * PAGE_SIZE, total)} of {total} polic
                {total === 1 ? 'y' : 'ies'}.
              </p>
              {totalPages > 1 && (
                <Pagination
                  total={totalPages}
                  page={filters.page}
                  onChange={(page) => setFilters({ page })}
                  size='sm'
                  isDisabled={policiesQuery.isFetching}
                />
              )}
            </div>
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
