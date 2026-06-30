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
import { Ban, Info, KeyRound, Plus, RefreshCw } from 'lucide-react';
import * as React from 'react';

import { CapabilityGate } from '@/components/ui/capability-gate';
import { ConfirmDialog } from '@/components/ui/confirm-dialog';
import { EmptyState } from '@/components/ui/empty-state';
import { PageHeader } from '@/components/ui/page-header';
import { PrimaryButton } from '@/components/ui/primary-button';
import { PulseDot } from '@/components/ui/pulse-dot';
import { fetchMe, type MeResponse } from '@/lib/auth';
import {
  createProviderSecret,
  disableProviderSecret,
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

const GRID_COLUMNS =
  'minmax(0,1.2fr) minmax(0,0.8fr) minmax(0,0.8fr) minmax(0,1.2fr) minmax(0,0.55fr) minmax(0,1fr) auto';

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

function providerLabel(provider: SecretProvider): string {
  return provider === 'anthropic' ? 'Anthropic' : 'OpenAI';
}

function providerPillClass(provider: SecretProvider): string {
  if (provider === 'anthropic') {

    return 'bg-primary-soft text-primary-300';
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
      <span>Status</span>
      <span className='sr-only'>Actions</span>
    </div>
  );
}

function SecretsTable({
  items,
  onRotate,
  onDisable,
}: {
  items: ProviderSecret[];
  onRotate: (secret: ProviderSecret) => void;
  onDisable: (secret: ProviderSecret) => void;
}) {
  return (
    <div className='surface-card overflow-hidden'>
      <div className='overflow-x-auto'>
        <div className='min-w-[860px]'>
          <ColumnHeader />
          {items.map((secret) => (
            <div
              key={secret.id}
              className='grid items-center gap-4 border-b border-divider px-5 py-3.5 transition-colors last:border-b-0 hover:bg-content2/60'
              style={{ gridTemplateColumns: GRID_COLUMNS }}
            >
              <div className='flex min-w-0 items-center gap-3'>
                <span className='inline-flex h-[30px] w-[30px] shrink-0 items-center justify-center rounded-[9px] bg-content3 text-primary-300'>
                  <KeyRound className='h-[15px] w-[15px]' strokeWidth={1.8} />
                </span>
                <span className='truncate text-[13.5px] font-semibold text-foreground'>
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
                {secret.rotation_state && (
                  <span className='truncate font-mono text-[11px] text-default-400'>
                    {humanize(secret.rotation_state)}
                  </span>
                )}
              </div>
              <div className='flex items-center justify-end gap-2'>
                <Button
                  size='sm'
                  variant='flat'
                  startContent={<RefreshCw className='h-3.5 w-3.5' />}
                  onPress={() => onRotate(secret)}
                  isDisabled={!secret.active}
                >
                  Rotate
                </Button>
                <Button
                  size='sm'
                  color='danger'
                  variant='flat'
                  startContent={<Ban className='h-3.5 w-3.5' />}
                  onPress={() => onDisable(secret)}
                  isDisabled={!secret.active}
                >
                  Disable
                </Button>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function SecretsTableSkeleton() {
  return (
    <div className='surface-card overflow-hidden'>
      <div className='overflow-x-auto'>
        <div className='min-w-[860px]'>
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

  const canSubmit =
    name.trim().length > 0 && rawSecret.trim().length > 0 && !isPending;

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
                  description='Organization secrets apply org-wide; team secrets are limited to the active team.'
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

  const secretsQuery = useQuery<ProviderSecret[]>({
    queryKey: ['model-policy', 'secrets', activeProjectId, activeTeamId],
    enabled: Boolean(activeProjectId),
    queryFn: async () => {
      try {

        return await listProviderSecrets({
          projectId: activeProjectId ?? '',
          teamId: activeTeamId,
        });
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
      secretId,
      rawSecret,
    }: {
      secretId: string;
      rawSecret: string;
    }) =>
      rotateProviderSecret(secretId, {
        project_id: activeProjectId ?? '',
        team_id: activeTeamId,
        raw_secret: rawSecret,
        request_id: genRequestId(),
      }),
  });
  const disableMutation = useMutation({
    mutationFn: (secretId: string) =>
      disableProviderSecret(secretId, {
        project_id: activeProjectId ?? '',
        team_id: activeTeamId,
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
      team_id: activeTeamId,
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
        secretId: rotateTarget.id,
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

  async function handleDisable() {
    if (!disableTarget) {

      return;
    }

    try {
      await disableMutation.mutateAsync(disableTarget.id);
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

  return (
    <CapabilityGate capabilities={capabilities} required='secrets:*'>
      <section className='space-y-6'>
        <PageHeader
          title='Secrets'
          subtitle='Provider API keys used for model calls.'
          actions={
            <PrimaryButton
              startContent={<Plus className='h-4 w-4' />}
              onPress={openAdd}
              isDisabled={!meLoaded}
            >
              Add secret
            </PrimaryButton>
          }
        />

        {isLoading ? (
          <SecretsTableSkeleton />
        ) : secretsQuery.isError ? (
          <div className='flex items-start gap-3 rounded-[16px] border border-danger/30 bg-danger/5 px-5 py-4'>
            <Info className='mt-0.5 h-5 w-5 shrink-0 text-danger' />
            <p className='text-[13px] leading-relaxed text-danger'>
              {secretsQuery.error instanceof Error
                ? secretsQuery.error.message
                : 'Failed to load secrets.'}
            </p>
          </div>
        ) : items.length === 0 ? (
          <EmptyState
            title='No secrets yet'
            description='Add a provider API key to enable model calls for this scope.'
            icon={<KeyRound className='h-6 w-6' />}
            action={
              <PrimaryButton
                startContent={<Plus className='h-4 w-4' />}
                onPress={openAdd}
              >
                Add secret
              </PrimaryButton>
            }
          />
        ) : (
          <SecretsTable
            items={items}
            onRotate={openRotate}
            onDisable={setDisableTarget}
          />
        )}

        <p className='flex items-center gap-1.5 text-[12px] text-default-400'>
          <Info className='h-3.5 w-3.5' />
          Listing requires the model-policy secrets list endpoint; an empty list
          is shown if it is unavailable.
        </p>

        <AddSecretModal
          isOpen={addOpen}
          isPending={createMutation.isPending}
          error={addError}
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
