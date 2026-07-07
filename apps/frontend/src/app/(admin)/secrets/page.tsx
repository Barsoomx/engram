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
import { Ban, Info, KeyRound, Plus, Power, RefreshCw } from 'lucide-react';
import * as React from 'react';

import { CapabilityGate } from '@/components/ui/capability-gate';
import { ConfirmDialog } from '@/components/ui/confirm-dialog';
import { EmptyState } from '@/components/ui/empty-state';
import { ErrorState } from '@/components/ui/error-state';
import { PageHeader } from '@/components/ui/page-header';
import { PrimaryButton } from '@/components/ui/primary-button';
import { PulseDot } from '@/components/ui/pulse-dot';
import { TimeStamp } from '@/components/ui/time-stamp';
import { useUrlFilters } from '@/hooks/use-url-filters';
import { fetchMe, hasCapability, type MeResponse } from '@/lib/auth';
import {
  createProviderSecret,
  disableProviderSecret,
  enableProviderSecret,
  genRequestId,
  listProviderSecrets,
  rotateProviderSecret,
  SECRET_PROVIDERS,
  type ProviderSecret,
  type ProviderSecretCreateInput,
  type SecretProvider,
  type SecretScope,
} from '@/lib/console-api';
import { useProjectStore } from '@/lib/project-store';
import { useTeamStore } from '@/lib/team-store';

const SECRET_SCOPES: { key: SecretScope; label: string }[] = [
  { key: 'organization', label: 'Organization' },
  { key: 'team', label: 'Team' },
];

const SECRET_FILTER_DEFAULTS = { provider: '', scope: '', active: '' };

const ACTIVE_OPTIONS: { key: string; label: string }[] = [
  { key: 'active', label: 'Active' },
  { key: 'disabled', label: 'Disabled' },
];

const GRID_COLUMNS =
  'minmax(0,1.1fr) minmax(0,0.7fr) minmax(0,0.7fr) minmax(0,1.1fr) minmax(0,0.5fr) minmax(0,0.8fr) minmax(0,0.7fr) auto';

function extractDetail(error: unknown, fallback: string): string {
  if (axios.isAxiosError(error)) {
    const data = error.response?.data as { detail?: string } | undefined;

    if (data?.detail) {

      return data.detail;
    }
  }

  return fallback;
}

function humanize(value: string): string {
  return value
    .split(/[_\-\s]+/)
    .filter(Boolean)
    .map((word) => word[0].toUpperCase() + word.slice(1))
    .join(' ');
}

function notableRotationState(state: string): string | null {
  const value = (state || '').toLowerCase();

  if (['', 'idle', 'none', 'active', 'stable', 'disabled'].includes(value)) {

    return null;
  }

  return humanize(state);
}

function secretTeamId(secret: ProviderSecret): string | null {
  return secret.scope === 'team' ? secret.team_id : null;
}

function providerLabel(provider: SecretProvider): string {
  if (provider === 'anthropic') {

    return 'Anthropic';
  }

  if (provider === 'deepseek') {

    return 'DeepSeek';
  }

  return 'OpenAI';
}

function providerPillClass(provider: SecretProvider): string {
  if (provider === 'anthropic') {

    return 'bg-primary-soft text-primary-300';
  }

  if (provider === 'deepseek') {

    return 'bg-[rgba(100,181,246,0.15)] text-blue-400';
  }

  return 'bg-[rgba(61,217,172,0.13)] text-success';
}

function ColumnHeader() {
  return (
    <div
      className='grid items-center gap-4 border-b border-divider px-5 py-3 text-[10.5px] font-semibold uppercase tracking-[0.1em] text-default-400'
      style={{ gridTemplateColumns: GRID_COLUMNS }}
    >
      <span>Name</span>
      <span>Provider</span>
      <span>Scope</span>
      <span>Fingerprint</span>
      <span>Version</span>
      <span>Updated</span>
      <span>Status</span>
      <span className='sr-only'>Actions</span>
    </div>
  );
}

function SecretsTable({
  items,
  canManage,
  rotatePending,
  disablePending,
  enablePending,
  onRotate,
  onDisable,
  onEnable,
}: {
  items: ProviderSecret[];
  canManage: boolean;
  rotatePending: boolean;
  disablePending: boolean;
  enablePending: boolean;
  onRotate: (secret: ProviderSecret) => void;
  onDisable: (secret: ProviderSecret) => void;
  onEnable: (secret: ProviderSecret) => void;
}) {
  return (
    <div className='surface-card overflow-hidden'>
      <div className='overflow-x-auto'>
        <div className='min-w-[960px]'>
          <ColumnHeader />
          {items.map((secret) => {
            const rotation = notableRotationState(secret.rotation_state);

            return (
              <div
                key={secret.id}
                className='grid items-center gap-4 border-b border-divider px-5 py-3.5 transition-colors last:border-b-0 hover:bg-content2/60'
                style={{ gridTemplateColumns: GRID_COLUMNS }}
              >
                <div className='flex min-w-0 items-center gap-3'>
                  <span className='inline-flex h-[30px] w-[30px] shrink-0 items-center justify-center rounded-[9px] bg-content3 text-primary-300'>
                    <KeyRound className='h-[15px] w-[15px]' strokeWidth={1.8} />
                  </span>
                  <span
                    className='truncate text-[13.5px] font-semibold text-foreground'
                    title={secret.name}
                  >
                    {secret.name}
                  </span>
                </div>
                <div className='min-w-0'>
                  <span
                    className={`inline-flex max-w-full items-center truncate rounded-[7px] px-2.5 py-1 text-[11.5px] font-medium ${providerPillClass(secret.provider)}`}
                  >
                    {providerLabel(secret.provider)}
                  </span>
                </div>
                <span className='truncate text-[12px] text-default-500'>
                  {humanize(secret.scope)}
                </span>
                <span className='truncate font-mono text-[12px] text-default-400'>
                  {secret.secret_fingerprint || '—'}
                </span>
                <span className='tnum font-mono text-[12px] text-default-500'>
                  v{secret.current_version}
                </span>
                <span className='whitespace-nowrap text-[12px] text-default-400'>
                  <TimeStamp value={secret.updated_at} />
                </span>
                <div className='flex min-w-0 flex-col gap-0.5'>
                  <div className='flex items-center gap-2'>
                    <PulseDot
                      color={secret.active ? '#3DD9AC' : '#666C77'}
                      pulse={secret.active}
                    />
                    <span className='text-[12px] text-default-500'>
                      {secret.active ? 'Active' : 'Disabled'}
                    </span>
                  </div>
                  {rotation && (
                    <span className='truncate font-mono text-[11px] text-default-400'>
                      {rotation}
                    </span>
                  )}
                </div>
                <div className='flex items-center justify-end gap-2'>
                  {canManage &&
                    (secret.active ? (
                      <>
                        <Button
                          size='sm'
                          variant='flat'
                          startContent={<RefreshCw className='h-3.5 w-3.5' />}
                          onPress={() => onRotate(secret)}
                          isDisabled={rotatePending}
                        >
                          Rotate
                        </Button>
                        <Button
                          size='sm'
                          color='danger'
                          variant='flat'
                          startContent={<Ban className='h-3.5 w-3.5' />}
                          onPress={() => onDisable(secret)}
                          isDisabled={disablePending}
                        >
                          Disable
                        </Button>
                      </>
                    ) : (
                      <Button
                        size='sm'
                        color='success'
                        variant='flat'
                        startContent={<Power className='h-3.5 w-3.5' />}
                        onPress={() => onEnable(secret)}
                        isDisabled={enablePending}
                      >
                        Enable
                      </Button>
                    ))}
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

function SecretsTableSkeleton() {
  return (
    <div className='surface-card overflow-hidden'>
      <div className='overflow-x-auto'>
        <div className='min-w-[960px]'>
          <ColumnHeader />
          {Array.from({ length: 5 }).map((_, index) => (
            <div
              key={index}
              className='grid items-center gap-4 border-b border-divider px-5 py-3.5 last:border-b-0'
              style={{ gridTemplateColumns: GRID_COLUMNS }}
            >
              <div className='flex min-w-0 items-center gap-3'>
                <span className='h-[30px] w-[30px] shrink-0 rounded-[9px] bg-content2' />
                <span className='h-3.5 w-24 rounded-medium bg-content2' />
              </div>
              <span className='h-5 w-16 rounded-[7px] bg-content2' />
              <span className='h-3 w-16 rounded-medium bg-content2' />
              <span className='h-3 w-28 rounded-medium bg-content2' />
              <span className='h-3 w-8 rounded-medium bg-content2' />
              <span className='h-3 w-14 rounded-medium bg-content2' />
              <span className='h-3 w-14 rounded-medium bg-content2' />
              <div className='flex items-center justify-end gap-2'>
                <span className='h-8 w-16 rounded-medium bg-content2' />
                <span className='h-8 w-20 rounded-medium bg-content2' />
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

interface AddSecretModalProps {
  isOpen: boolean;
  isPending: boolean;
  error: string | null;
  hasTeam: boolean;
  onClose: () => void;
  onSubmit: (input: {
    name: string;
    provider: SecretProvider;
    scope: SecretScope;
    raw_secret: string;
  }) => Promise<boolean>;
}

function AddSecretModal({
  isOpen,
  isPending,
  error,
  hasTeam,
  onClose,
  onSubmit,
}: AddSecretModalProps) {
  const [name, setName] = React.useState('');
  const [provider, setProvider] = React.useState<SecretProvider>('anthropic');
  const [scope, setScope] = React.useState<SecretScope>('organization');
  const [rawSecret, setRawSecret] = React.useState('');

  React.useEffect(() => {
    if (!isOpen) {
      setName('');
      setProvider('anthropic');
      setScope('organization');
      setRawSecret('');
    }
  }, [isOpen]);

  const teamScopeBlocked = scope === 'team' && !hasTeam;
  const canSubmit =
    name.trim().length > 0 &&
    rawSecret.trim().length > 0 &&
    !teamScopeBlocked &&
    !isPending;

  async function handleSubmit() {
    if (!canSubmit) {

      return;
    }

    const ok = await onSubmit({
      name: name.trim(),
      provider,
      scope,
      raw_secret: rawSecret,
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
      isDismissable={!isPending}
      hideCloseButton={isPending}
    >
      <ModalContent>
        {() => (
          <>
            <ModalHeader className='flex flex-col gap-1 text-foreground'>
              Add secret
            </ModalHeader>
            <ModalBody>
              <div className='space-y-4'>
                <Input
                  label='Name'
                  labelPlacement='outside'
                  placeholder='Primary Anthropic key'
                  value={name}
                  onValueChange={setName}
                  maxLength={255}
                  isDisabled={isPending}
                />
                <Select
                  label='Provider'
                  labelPlacement='outside'
                  placeholder='Select a provider'
                  selectedKeys={new Set([provider])}
                  isDisabled={isPending}
                  onSelectionChange={(keys) => {
                    const next = Array.from(keys)[0];

                    if (typeof next === 'string') {
                      setProvider(next as SecretProvider);
                    }
                  }}
                >
                  {SECRET_PROVIDERS.map((value) => (
                    <SelectItem key={value}>{providerLabel(value)}</SelectItem>
                  ))}
                </Select>
                <Select
                  label='Scope'
                  labelPlacement='outside'
                  placeholder='Select a scope'
                  selectedKeys={new Set([scope])}
                  isDisabled={isPending}
                  description='Organization secrets apply org-wide; team secrets require an active team.'
                  onSelectionChange={(keys) => {
                    const next = Array.from(keys)[0];

                    if (typeof next === 'string') {
                      setScope(next as SecretScope);
                    }
                  }}
                >
                  {SECRET_SCOPES.map((option) => (
                    <SelectItem key={option.key}>{option.label}</SelectItem>
                  ))}
                </Select>
                {teamScopeBlocked && (
                  <div className='flex items-start gap-2.5 rounded-[12px] border border-warning/30 bg-warning/5 px-3.5 py-3'>
                    <Info className='mt-0.5 h-4 w-4 shrink-0 text-warning' />
                    <p className='text-[13px] leading-relaxed text-warning-600'>
                      Select a team in the switcher above to add a team-scoped
                      secret, or choose Organization scope.
                    </p>
                  </div>
                )}
                <Input
                  label='Secret'
                  labelPlacement='outside'
                  placeholder='sk-…'
                  type='password'
                  value={rawSecret}
                  onValueChange={setRawSecret}
                  description='The raw key is encrypted at rest and never shown again.'
                  isDisabled={isPending}
                />
                {error && (
                  <div className='flex items-start gap-2.5 rounded-[12px] border border-danger/30 bg-danger/5 px-3.5 py-3'>
                    <Info className='mt-0.5 h-4 w-4 shrink-0 text-danger' />
                    <p className='text-[13px] leading-relaxed text-danger'>
                      {error}
                    </p>
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
                Add secret
              </Button>
            </ModalFooter>
          </>
        )}
      </ModalContent>
    </Modal>
  );
}

interface RotateSecretModalProps {
  isOpen: boolean;
  target: ProviderSecret | null;
  isPending: boolean;
  error: string | null;
  onClose: () => void;
  onSubmit: (rawSecret: string) => Promise<boolean>;
}

function RotateSecretModal({
  isOpen,
  target,
  isPending,
  error,
  onClose,
  onSubmit,
}: RotateSecretModalProps) {
  const [rawSecret, setRawSecret] = React.useState('');

  React.useEffect(() => {
    if (!isOpen) {
      setRawSecret('');
    }
  }, [isOpen]);

  const canSubmit = rawSecret.trim().length > 0 && !isPending;

  async function handleSubmit() {
    if (!canSubmit) {

      return;
    }

    const ok = await onSubmit(rawSecret);

    if (ok) {
      onClose();
    }
  }

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      placement='center'
      isDismissable={!isPending}
      hideCloseButton={isPending}
    >
      <ModalContent>
        {() => (
          <>
            <ModalHeader className='flex flex-col gap-1 text-foreground'>
              Rotate secret
            </ModalHeader>
            <ModalBody>
              <div className='space-y-4'>
                {target && (
                  <div className='rounded-medium bg-content2/60 p-3 text-sm'>
                    <p className='text-default-500'>Secret</p>
                    <p className='font-medium text-foreground'>{target.name}</p>
                    <p className='mt-1 font-mono text-xs text-default-400'>
                      {providerLabel(target.provider)} · v
                      {target.current_version}
                    </p>
                  </div>
                )}
                <Input
                  label='New secret'
                  labelPlacement='outside'
                  placeholder='sk-…'
                  type='password'
                  value={rawSecret}
                  onValueChange={setRawSecret}
                  description='Rotating increments the version and supersedes the previous key.'
                  isDisabled={isPending}
                />
                {error && (
                  <div className='flex items-start gap-2.5 rounded-[12px] border border-danger/30 bg-danger/5 px-3.5 py-3'>
                    <Info className='mt-0.5 h-4 w-4 shrink-0 text-danger' />
                    <p className='text-[13px] leading-relaxed text-danger'>
                      {error}
                    </p>
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
                Rotate
              </Button>
            </ModalFooter>
          </>
        )}
      </ModalContent>
    </Modal>
  );
}

export default function SecretsPage() {
  const activeProjectId = useProjectStore((s) => s.activeProjectId);
  const activeTeamId = useTeamStore((s) => s.activeTeamId);
  const queryClient = useQueryClient();

  const meQuery = useQuery<MeResponse>({
    queryKey: ['auth', 'me'],
    queryFn: fetchMe,
  });

  const capabilities = React.useMemo(
    () => meQuery.data?.capabilities ?? [],
    [meQuery.data?.capabilities],
  );
  const canManageSecrets = hasCapability(capabilities, 'secrets:*');

  const [filters, setFilters] = useUrlFilters(SECRET_FILTER_DEFAULTS);

  const activeFilter =
    filters.active === 'active'
      ? true
      : filters.active === 'disabled'
        ? false
        : undefined;

  const secretsQuery = useQuery<ProviderSecret[]>({
    queryKey: [
      'model-policy',
      'secrets',
      activeProjectId,
      activeTeamId,
      filters.provider,
      filters.scope,
      filters.active,
    ],
    enabled: Boolean(activeProjectId),
    queryFn: async () => {
      try {

        return await listProviderSecrets(
          {
            projectId: activeProjectId ?? '',
            teamId: activeTeamId,
          },
          {
            provider: (filters.provider || undefined) as
              | SecretProvider
              | undefined,
            scope: (filters.scope || undefined) as SecretScope | undefined,
            active: activeFilter,
          },
        );
      } catch (error) {
        if (axios.isAxiosError(error) && error.response?.status === 404) {

          return [];
        }

        throw error;
      }
    },
  });

  const [addOpen, setAddOpen] = React.useState(false);
  const [addError, setAddError] = React.useState<string | null>(null);
  const [rotateTarget, setRotateTarget] = React.useState<ProviderSecret | null>(
    null,
  );
  const [rotateError, setRotateError] = React.useState<string | null>(null);
  const [disableTarget, setDisableTarget] =
    React.useState<ProviderSecret | null>(null);

  const createMutation = useMutation({
    mutationFn: createProviderSecret,
  });
  const rotateMutation = useMutation({
    mutationFn: ({
      secret,
      rawSecret,
    }: {
      secret: ProviderSecret;
      rawSecret: string;
    }) =>
      rotateProviderSecret(secret.id, {
        project_id: activeProjectId ?? '',
        team_id: secretTeamId(secret),
        raw_secret: rawSecret,
        request_id: genRequestId(),
      }),
  });
  const disableMutation = useMutation({
    mutationFn: (secret: ProviderSecret) =>
      disableProviderSecret(secret.id, {
        project_id: activeProjectId ?? '',
        team_id: secretTeamId(secret),
        request_id: genRequestId(),
      }),
  });
  const enableMutation = useMutation({
    mutationFn: (secret: ProviderSecret) =>
      enableProviderSecret(secret.id, {
        project_id: activeProjectId ?? '',
        team_id: secretTeamId(secret),
        request_id: genRequestId(),
      }),
  });

  function invalidateSecrets() {
    queryClient.invalidateQueries({ queryKey: ['model-policy', 'secrets'] });
  }

  async function handleAdd(input: {
    name: string;
    provider: SecretProvider;
    scope: SecretScope;
    raw_secret: string;
  }): Promise<boolean> {
    setAddError(null);

    const body: ProviderSecretCreateInput = {
      project_id: activeProjectId ?? '',
      team_id: input.scope === 'team' ? activeTeamId : null,
      name: input.name,
      provider: input.provider,
      scope: input.scope,
      raw_secret: input.raw_secret,
      request_id: genRequestId(),
    };

    try {
      await createMutation.mutateAsync(body);
      invalidateSecrets();
      addToast({ title: 'Secret added', color: 'success' });

      return true;
    } catch (error) {
      setAddError(extractDetail(error, 'Failed to add secret.'));

      return false;
    }
  }

  async function handleRotate(rawSecret: string): Promise<boolean> {
    if (!rotateTarget) {

      return false;
    }

    setRotateError(null);

    try {
      await rotateMutation.mutateAsync({
        secret: rotateTarget,
        rawSecret,
      });
      invalidateSecrets();
      addToast({ title: 'Secret rotated', color: 'success' });

      return true;
    } catch (error) {
      setRotateError(extractDetail(error, 'Failed to rotate secret.'));

      return false;
    }
  }

  async function handleEnable(secret: ProviderSecret) {
    try {
      await enableMutation.mutateAsync(secret);
      invalidateSecrets();
      addToast({ title: 'Secret enabled', color: 'success' });
    } catch (error) {
      addToast({
        title: 'Failed to enable secret',
        description: extractDetail(error, 'Unexpected error.'),
        color: 'danger',
      });
    }
  }

  async function handleDisable() {
    if (!disableTarget) {

      return;
    }

    try {
      await disableMutation.mutateAsync(disableTarget);
      invalidateSecrets();
      addToast({ title: 'Secret disabled', color: 'success' });
      setDisableTarget(null);
    } catch (error) {
      addToast({
        title: 'Failed to disable secret',
        description: extractDetail(error, 'Unexpected error.'),
        color: 'danger',
      });
      setDisableTarget(null);
    }
  }

  function openAdd() {
    setAddError(null);
    setAddOpen(true);
  }

  function openRotate(secret: ProviderSecret) {
    setRotateError(null);
    setRotateTarget(secret);
  }

  const meLoaded = meQuery.data !== undefined;

  if (!activeProjectId) {

    return (
      <section className='space-y-6'>
        <PageHeader
          title='Secrets'
          subtitle='Provider API keys used for model calls.'
        />
        <EmptyState
          title='Select a project'
          description='Choose a project from the switcher above to manage provider secrets.'
          icon={<KeyRound className='h-6 w-6' />}
        />
      </section>
    );
  }

  const isLoading = meQuery.isLoading || secretsQuery.isLoading;
  const items = secretsQuery.data ?? [];
  const hasFilters =
    filters.provider.length > 0 ||
    filters.scope.length > 0 ||
    filters.active.length > 0;

  return (
    <CapabilityGate capabilities={capabilities} required='secrets:read'>
      <section className='space-y-6'>
        <PageHeader
          title='Secrets'
          subtitle='Provider API keys used for model calls.'
          actions={
            canManageSecrets ? (
              <PrimaryButton
                startContent={<Plus className='h-4 w-4' />}
                onPress={openAdd}
                isDisabled={!meLoaded}
              >
                Add secret
              </PrimaryButton>
            ) : undefined
          }
        />

        <div className='surface-card flex flex-col gap-3 p-4 sm:flex-row sm:items-end'>
          <Select
            aria-label='Filter by provider'
            placeholder='All providers'
            selectedKeys={
              filters.provider ? new Set([filters.provider]) : new Set()
            }
            variant='bordered'
            size='sm'
            className='max-w-[180px]'
            onSelectionChange={(keys) => {
              const next = Array.from(keys)[0];

              setFilters({ provider: typeof next === 'string' ? next : '' });
            }}
          >
            {SECRET_PROVIDERS.map((value) => (
              <SelectItem key={value}>{providerLabel(value)}</SelectItem>
            ))}
          </Select>
          <Select
            aria-label='Filter by scope'
            placeholder='All scopes'
            selectedKeys={filters.scope ? new Set([filters.scope]) : new Set()}
            variant='bordered'
            size='sm'
            className='max-w-[180px]'
            onSelectionChange={(keys) => {
              const next = Array.from(keys)[0];

              setFilters({ scope: typeof next === 'string' ? next : '' });
            }}
          >
            {SECRET_SCOPES.map((option) => (
              <SelectItem key={option.key}>{option.label}</SelectItem>
            ))}
          </Select>
          <Select
            aria-label='Filter by status'
            placeholder='All statuses'
            selectedKeys={filters.active ? new Set([filters.active]) : new Set()}
            variant='bordered'
            size='sm'
            className='max-w-[160px]'
            onSelectionChange={(keys) => {
              const next = Array.from(keys)[0];

              setFilters({ active: typeof next === 'string' ? next : '' });
            }}
          >
            {ACTIVE_OPTIONS.map((option) => (
              <SelectItem key={option.key}>{option.label}</SelectItem>
            ))}
          </Select>
        </div>

        {isLoading ? (
          <SecretsTableSkeleton />
        ) : secretsQuery.isError ? (
          <ErrorState
            message={
              secretsQuery.error instanceof Error
                ? secretsQuery.error.message
                : 'Failed to load secrets.'
            }
            onRetry={() => secretsQuery.refetch()}
          />
        ) : items.length === 0 ? (
          <EmptyState
            title={hasFilters ? 'No matching secrets' : 'No secrets yet'}
            description={
              hasFilters
                ? 'No secrets match the current filters.'
                : 'Add a provider API key to enable model calls for this scope.'
            }
            icon={<KeyRound className='h-6 w-6' />}
            action={
              canManageSecrets && !hasFilters ? (
                <PrimaryButton
                  startContent={<Plus className='h-4 w-4' />}
                  onPress={openAdd}
                >
                  Add secret
                </PrimaryButton>
              ) : undefined
            }
          />
        ) : (
          <SecretsTable
            items={items}
            canManage={canManageSecrets}
            rotatePending={rotateMutation.isPending}
            disablePending={disableMutation.isPending}
            enablePending={enableMutation.isPending}
            onRotate={openRotate}
            onDisable={setDisableTarget}
            onEnable={handleEnable}
          />
        )}

        <AddSecretModal
          isOpen={addOpen}
          isPending={createMutation.isPending}
          error={addError}
          hasTeam={Boolean(activeTeamId)}
          onClose={() => setAddOpen(false)}
          onSubmit={handleAdd}
        />

        <RotateSecretModal
          isOpen={rotateTarget !== null}
          target={rotateTarget}
          isPending={rotateMutation.isPending}
          error={rotateError}
          onClose={() => setRotateTarget(null)}
          onSubmit={handleRotate}
        />

        <ConfirmDialog
          isOpen={disableTarget !== null}
          title='Disable secret'
          description={
            disableTarget
              ? `Disable "${disableTarget.name}"? Model calls relying on this key will stop until a new secret is provided.`
              : undefined
          }
          confirmLabel='Disable'
          confirmColor='danger'
          isLoading={disableMutation.isPending}
          onClose={() => setDisableTarget(null)}
          onConfirm={handleDisable}
        />
      </section>
    </CapabilityGate>
  );
}
