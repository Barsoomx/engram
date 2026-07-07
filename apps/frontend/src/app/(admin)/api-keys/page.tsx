'use client';

import {
  Button,
  Checkbox,
  CheckboxGroup,
  Chip,
  Input,
  Modal,
  ModalBody,
  ModalContent,
  ModalFooter,
  ModalHeader,
  Select,
  SelectItem,
} from '@heroui/react';
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import axios from 'axios';
import {
  Check,
  Copy,
  KeyRound,
  Plus,
  Search,
  ShieldAlert,
  ShieldCheck,
  Trash2,
} from 'lucide-react';
import * as React from 'react';

import { ConfirmDialog } from '@/components/ui/confirm-dialog';
import { EmptyState } from '@/components/ui/empty-state';
import { ErrorState } from '@/components/ui/error-state';
import { CapabilityGate } from '@/components/ui/capability-gate';
import { PageHeader } from '@/components/ui/page-header';
import { PaginationFooter } from '@/components/ui/pagination-footer';
import { PrimaryButton } from '@/components/ui/primary-button';
import { StatusPill } from '@/components/ui/status-pill';
import { TimeStamp } from '@/components/ui/time-stamp';
import { useApiKeys, useIssueApiKey, useRevokeApiKey } from '@/hooks/use-api-keys';
import { useDebouncedValue } from '@/hooks/use-debounced-value';
import { useUrlFilters } from '@/hooks/use-url-filters';
import { fetchMe, hasCapability, type MeResponse } from '@/lib/auth';
import type { ApiKey, ApiKeyStatus } from '@/lib/admin-api';
import { useOrgStore } from '@/lib/org-store';

const KEY_FILTER_DEFAULTS = { search: '', status: '', page: 1 };
const KEY_PAGE_SIZE = 20;

const STATUS_OPTIONS: { key: ApiKeyStatus; label: string }[] = [
  { key: 'active', label: 'Active' },
  { key: 'expired', label: 'Expired' },
  { key: 'revoked', label: 'Revoked' },
];

const KNOWN_CAPABILITY_SUBCODES: Record<string, readonly string[]> = {
  api_keys: ['read', 'issue', 'revoke'],
  memories: ['read', 'review', 'propose', 'admin'],
  observations: ['read', 'write'],
  organizations: ['read', 'admin'],
  teams: ['read', 'admin'],
  projects: ['read', 'admin'],
  members: ['read', 'admin'],
  roles: ['read'],
  audit: ['read'],
  model_policy: ['read'],
  secrets: ['read'],
  context: ['read'],
};

function expandGrantableCapabilities(capabilities: string[]): string[] {
  const grantable = new Set<string>();

  for (const capability of capabilities) {
    if (capability.endsWith(':*')) {
      const group = capability.slice(0, -2);

      grantable.add(capability);

      const subcodes = KNOWN_CAPABILITY_SUBCODES[group];

      if (subcodes) {
        for (const subcode of subcodes) {
          grantable.add(`${group}:${subcode}`);
        }
      }
    } else if (capability.includes(':')) {
      grantable.add(capability);
    }
  }

  return Array.from(grantable).sort();
}

type KeyStatus = ApiKeyStatus;

function deriveStatus(key: ApiKey): KeyStatus {
  if (key.revoked_at) {

    return 'revoked';
  }

  if (key.expires_at) {
    const expiry = new Date(key.expires_at).getTime();

    if (Number.isFinite(expiry) && expiry <= Date.now()) {

      return 'expired';
    }
  }

  return 'active';
}

function CapabilityChips({ capabilities }: { capabilities: string[] }) {
  if (capabilities.length === 0) {

    return <span className='text-default-400 text-xs'>—</span>;
  }

  return (
    <div className='flex flex-wrap gap-1.5'>
      {capabilities.map((capability) => (
        <Chip
          key={capability}
          size='sm'
          variant='bordered'
          className='font-mono text-[11px]'
        >
          {capability}
        </Chip>
      ))}
    </div>
  );
}

const GRID_COLUMNS_BASE =
  'minmax(0,1.1fr) minmax(0,0.9fr) minmax(0,1.6fr) minmax(0,1fr) minmax(0,0.8fr) minmax(0,0.9fr) minmax(0,0.7fr)';

function gridColumns(canRevoke: boolean): string {
  return canRevoke ? `${GRID_COLUMNS_BASE} auto` : GRID_COLUMNS_BASE;
}

function ColumnHeader({ canRevoke }: { canRevoke: boolean }) {
  return (
    <div
      className='grid items-center gap-4 border-b border-divider px-5 py-3 text-[10.5px] font-semibold uppercase tracking-[0.1em] text-default-400'
      style={{ gridTemplateColumns: gridColumns(canRevoke) }}
    >
      <span>Name</span>
      <span>Key</span>
      <span>Capabilities</span>
      <span>Owner</span>
      <span>Last used</span>
      <span>Expires</span>
      <span>Status</span>
      {canRevoke && <span className='sr-only'>Actions</span>}
    </div>
  );
}

function ApiKeysTable({
  items,
  canRevoke,
  onRevoke,
}: {
  items: ApiKey[];
  canRevoke: boolean;
  onRevoke: (key: ApiKey) => void;
}) {
  return (
    <div className='surface-card overflow-hidden'>
      <div className='overflow-x-auto'>
        <div className='min-w-[1080px]'>
          <ColumnHeader canRevoke={canRevoke} />
          {items.map((key) => {
            const status = deriveStatus(key);

            return (
              <div
                key={key.id}
                className='grid items-start gap-4 border-b border-divider px-5 py-3.5 transition-colors last:border-b-0 hover:bg-content2/60'
                style={{ gridTemplateColumns: gridColumns(canRevoke) }}
              >
                <div className='flex min-w-0 items-center gap-3'>
                  <span className='inline-flex h-[30px] w-[30px] shrink-0 items-center justify-center rounded-[9px] bg-content3 text-primary-300'>
                    <KeyRound className='h-[15px] w-[15px]' strokeWidth={1.8} />
                  </span>
                  <span
                    className='truncate text-[13.5px] font-semibold text-foreground'
                    title={key.name}
                  >
                    {key.name}
                  </span>
                </div>
                <span className='truncate pt-1.5 font-mono text-[12px] text-default-500'>
                  {key.key_prefix}…
                </span>
                <div className='min-w-0 pt-0.5'>
                  <CapabilityChips capabilities={key.capabilities} />
                </div>
                <span
                  className='truncate pt-1.5 text-[12px] text-default-500'
                  title={key.owner_identity?.display_name ?? undefined}
                >
                  {key.owner_identity?.display_name ?? '—'}
                </span>
                <span className='whitespace-nowrap pt-1.5 text-[12px] text-default-400'>
                  <TimeStamp value={key.last_used_at} />
                </span>
                <span className='whitespace-nowrap pt-1.5 text-[12px] text-default-400'>
                  <TimeStamp value={key.expires_at} relative={false} />
                </span>
                <span className='pt-1'>
                  <StatusPill status={status} />
                </span>
                {canRevoke && (
                  <div className='flex items-center justify-end pt-0.5'>
                    <Button
                      size='sm'
                      color='danger'
                      variant='flat'
                      startContent={<Trash2 className='w-3.5 h-3.5' />}
                      onPress={() => onRevoke(key)}
                      isDisabled={status === 'revoked'}
                    >
                      Revoke
                    </Button>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

function ApiKeysTableSkeleton({ canRevoke }: { canRevoke: boolean }) {
  return (
    <div className='surface-card overflow-hidden'>
      <div className='overflow-x-auto'>
        <div className='min-w-[1080px]'>
          <ColumnHeader canRevoke={canRevoke} />
          {Array.from({ length: 6 }).map((_, index) => (
            <div
              key={index}
              className='grid items-center gap-4 border-b border-divider px-5 py-3.5 last:border-b-0'
              style={{ gridTemplateColumns: gridColumns(canRevoke) }}
            >
              <div className='flex min-w-0 items-center gap-3'>
                <span className='h-[30px] w-[30px] shrink-0 rounded-[9px] bg-content2' />
                <span className='h-3.5 w-28 rounded-medium bg-content2' />
              </div>
              <span className='h-3 w-24 rounded-medium bg-content2' />
              <span className='h-5 w-32 rounded-[7px] bg-content2' />
              <span className='h-3 w-20 rounded-medium bg-content2' />
              <span className='h-3 w-12 rounded-medium bg-content2' />
              <span className='h-3 w-16 rounded-medium bg-content2' />
              <span className='h-3 w-16 rounded-medium bg-content2' />
              {canRevoke && (
                <div className='flex items-center justify-end'>
                  <span className='h-8 w-20 rounded-medium bg-content2' />
                </div>
              )}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

interface IssueModalProps {
  isOpen: boolean;
  onClose: () => void;
  grantableCapabilities: string[];
  isIssuing: boolean;
  issueError: string | null;
  onIssue: (input: {
    name: string;
    capabilities: string[];
    expires_at: string | null;
  }) => Promise<
    { plaintext: string; key_prefix: string; key_fingerprint: string } | null
  >;
}

function IssueModal({
  isOpen,
  onClose,
  grantableCapabilities,
  isIssuing,
  issueError,
  onIssue,
}: IssueModalProps) {
  const [name, setName] = React.useState('');
  const [selectedCapabilities, setSelectedCapabilities] = React.useState<
    string[]
  >([]);
  const [expiresAt, setExpiresAt] = React.useState('');
  const [issuedSecret, setIssuedSecret] = React.useState<{
    plaintext: string;
    key_prefix: string;
    key_fingerprint: string;
  } | null>(null);
  const [copied, setCopied] = React.useState(false);

  React.useEffect(() => {
    if (!isOpen) {
      setName('');
      setSelectedCapabilities([]);
      setExpiresAt('');
      setIssuedSecret(null);
      setCopied(false);
    }
  }, [isOpen]);

  const canSubmit =
    name.trim().length > 0 &&
    selectedCapabilities.length > 0 &&
    !isIssuing &&
    issuedSecret === null;

  async function handleSubmit() {
    if (!canSubmit) {

      return;
    }

    const isoExpiry = expiresAt ? new Date(expiresAt).toISOString() : null;

    const result = await onIssue({
      name: name.trim(),
      capabilities: selectedCapabilities,
      expires_at: isoExpiry,
    });

    if (result) {
      setIssuedSecret(result);
    }
  }

  async function handleCopy() {
    if (!issuedSecret) {

      return;
    }

    try {
      await navigator.clipboard.writeText(issuedSecret.plaintext);
      setCopied(true);

      return;
    } catch {

      setCopied(false);
    }
  }

  function handleClose() {
    setIssuedSecret(null);
    setCopied(false);
    onClose();
  }

  return (
    <Modal
      isOpen={isOpen}
      onClose={handleClose}
      placement='center'
      size='lg'
      scrollBehavior='inside'
      isDismissable={!isIssuing}
      isKeyboardDismissDisabled={isIssuing}
      hideCloseButton={isIssuing}
    >
      <ModalContent>
        {() => (
          <>
            <ModalHeader className='flex flex-col gap-1 text-foreground'>
              {issuedSecret ? 'API key issued' : 'Issue API key'}
            </ModalHeader>
            <ModalBody>
              {issuedSecret ? (
                <div className='space-y-4'>
                  <div className='flex items-start gap-3 rounded-medium bg-warning-50 dark:bg-warning-500/10 border border-warning-200 dark:border-warning-500/30 p-3'>
                    <ShieldAlert className='w-5 h-5 text-warning-600 shrink-0 mt-0.5' />
                    <div className='space-y-1'>
                      <p className='text-sm font-medium text-warning-700 dark:text-warning-300'>
                        Copy it now. You will not see this again.
                      </p>
                      <p className='text-xs text-default-500'>
                        Engram stores only a hashed fingerprint. The plaintext
                        below is shown once and discarded when you close this
                        dialog.
                      </p>
                    </div>
                  </div>
                  <Input
                    isReadOnly
                    label='Plaintext key'
                    labelPlacement='outside'
                    value={issuedSecret.plaintext}
                    description={`Prefix ${issuedSecret.key_prefix}… · fingerprint ${issuedSecret.key_fingerprint}`}
                    classNames={{
                      input: 'font-mono text-xs break-all',
                    }}
                  />
                  <Button
                    color='primary'
                    variant='flat'
                    startContent={
                      copied ? (
                        <Check className='w-4 h-4' />
                      ) : (
                        <Copy className='w-4 h-4' />
                      )
                    }
                    onPress={handleCopy}
                  >
                    {copied ? 'Copied' : 'Copy to clipboard'}
                  </Button>
                </div>
              ) : (
                <div className='space-y-4'>
                  <Input
                    label='Name'
                    labelPlacement='outside'
                    placeholder='ci-deploy-key'
                    value={name}
                    onValueChange={setName}
                    maxLength={255}
                    isDisabled={isIssuing}
                  />
                  <Input
                    label='Expiry (optional)'
                    labelPlacement='outside'
                    type='datetime-local'
                    value={expiresAt}
                    onValueChange={setExpiresAt}
                    description='Leave blank for a key that never expires.'
                    isDisabled={isIssuing}
                  />
                  <div>
                    <p className='text-sm text-default-500 mb-2'>
                      Capabilities you can grant
                    </p>
                    {grantableCapabilities.length === 0 ? (
                      <p className='text-sm text-default-500'>
                        You have no grantable capabilities.
                      </p>
                    ) : (
                      <CheckboxGroup
                        value={selectedCapabilities}
                        onValueChange={setSelectedCapabilities}
                        isDisabled={isIssuing}
                      >
                        <div className='grid grid-cols-1 sm:grid-cols-2 gap-2'>
                          {grantableCapabilities.map((capability) => (
                            <Checkbox key={capability} value={capability}>
                              <span className='font-mono text-xs'>
                                {capability}
                              </span>
                            </Checkbox>
                          ))}
                        </div>
                      </CheckboxGroup>
                    )}
                  </div>
                  {issueError && (
                    <div className='rounded-medium bg-danger-50 dark:bg-danger-500/10 border border-danger-200 dark:border-danger-500/30 p-3'>
                      <p className='text-sm text-danger-600'>{issueError}</p>
                    </div>
                  )}
                </div>
              )}
            </ModalBody>
            <ModalFooter>
              {issuedSecret ? (
                <Button color='primary' onPress={handleClose}>
                  Done
                </Button>
              ) : (
                <>
                  <Button
                    color='default'
                    variant='light'
                    onPress={handleClose}
                    isDisabled={isIssuing}
                  >
                    Cancel
                  </Button>
                  <Button
                    color='primary'
                    onPress={handleSubmit}
                    isDisabled={!canSubmit}
                    isLoading={isIssuing}
                  >
                    Issue key
                  </Button>
                </>
              )}
            </ModalFooter>
          </>
        )}
      </ModalContent>
    </Modal>
  );
}

export default function ApiKeysPage() {
  const activeOrgId = useOrgStore((state) => state.activeOrgId);
  const meQuery = useQuery<MeResponse>({
    queryKey: ['auth', 'me'],
    queryFn: fetchMe,
  });

  const capabilities = React.useMemo(
    () => meQuery.data?.capabilities ?? [],
    [meQuery.data?.capabilities],
  );
  const grantableCapabilities = React.useMemo(
    () => expandGrantableCapabilities(capabilities),
    [capabilities],
  );

  const [filters, setFilters] = useUrlFilters(KEY_FILTER_DEFAULTS);
  const [searchInput, setSearchInput] = React.useState(filters.search);
  const debouncedSearch = useDebouncedValue(searchInput, 300);

  React.useEffect(() => {
    if (debouncedSearch === filters.search) {

      return;
    }

    setFilters({ search: debouncedSearch, page: 1 });
  }, [debouncedSearch, filters.search, setFilters]);

  const params = React.useMemo(
    () => ({
      page: filters.page,
      pageSize: KEY_PAGE_SIZE,
      search: filters.search || undefined,
      status: (filters.status || undefined) as ApiKeyStatus | undefined,
    }),
    [filters.page, filters.search, filters.status],
  );
  const keysQuery = useApiKeys(activeOrgId, params, {
    placeholderData: keepPreviousData,
  });

  const issueMutation = useIssueApiKey(activeOrgId);
  const revokeMutation = useRevokeApiKey(activeOrgId);

  const [issueOpen, setIssueOpen] = React.useState(false);
  const [issueError, setIssueError] = React.useState<string | null>(null);
  const [revokeTarget, setRevokeTarget] = React.useState<ApiKey | null>(null);

  const canIssue = hasCapability(capabilities, 'api_keys:issue');
  const canRevoke = hasCapability(capabilities, 'api_keys:revoke');

  async function handleIssue(input: {
    name: string;
    capabilities: string[];
    expires_at: string | null;
  }): Promise<{
    plaintext: string;
    key_prefix: string;
    key_fingerprint: string;
  } | null> {
    setIssueError(null);

    try {
      const result = await issueMutation.mutateAsync({
        name: input.name,
        capabilities: input.capabilities,
        expires_at: input.expires_at,
      });

      return {
        plaintext: result.plaintext,
        key_prefix: result.key_prefix,
        key_fingerprint: result.key_fingerprint,
      };
    } catch (error) {
      let detail: string | undefined;

      if (axios.isAxiosError(error)) {
        const data = error.response?.data as { detail?: string } | undefined;

        detail = data?.detail;
      }

      setIssueError(detail ?? 'Failed to issue API key.');

      return null;
    }
  }

  async function handleRevoke() {
    if (!revokeTarget) {

      return;
    }

    try {
      await revokeMutation.mutateAsync(revokeTarget.id);
      setRevokeTarget(null);
    } catch {
      setRevokeTarget(null);
    }
  }

  const isLoading = meQuery.isLoading || keysQuery.isLoading;
  const items = keysQuery.data?.results ?? [];
  const total = keysQuery.data?.count ?? 0;
  const meLoaded = meQuery.data !== undefined;
  const hasFilters = filters.search.length > 0 || filters.status.length > 0;

  return (
    <CapabilityGate capabilities={capabilities} required='api_keys:read'>
      <section className='space-y-6'>
        <PageHeader
          title='API Keys'
          subtitle='Provision and revoke organization API keys.'
          actions={
            canIssue ? (
              <PrimaryButton
                startContent={<Plus className='w-4 h-4' />}
                onPress={() => setIssueOpen(true)}
                isDisabled={!meLoaded}
              >
                Issue key
              </PrimaryButton>
            ) : null
          }
        />

        <div className='surface-card flex flex-col gap-3 p-4 sm:flex-row sm:items-end'>
          <Input
            aria-label='Search API keys'
            placeholder='Search by name or prefix…'
            value={searchInput}
            onValueChange={setSearchInput}
            variant='bordered'
            size='sm'
            isClearable
            onClear={() => setSearchInput('')}
            startContent={<Search className='w-4 h-4 text-default-400' />}
            className='max-w-xs'
          />
          <Select
            aria-label='Filter by status'
            placeholder='All statuses'
            selectedKeys={filters.status ? new Set([filters.status]) : new Set()}
            variant='bordered'
            size='sm'
            className='max-w-[180px]'
            onSelectionChange={(keys) => {
              const next = Array.from(keys)[0];

              setFilters({
                status: typeof next === 'string' ? next : '',
                page: 1,
              });
            }}
          >
            {STATUS_OPTIONS.map((option) => (
              <SelectItem key={option.key}>{option.label}</SelectItem>
            ))}
          </Select>
        </div>

        {isLoading ? (
          <ApiKeysTableSkeleton canRevoke={canRevoke} />
        ) : keysQuery.isError ? (
          <ErrorState
            message={
              keysQuery.error instanceof Error
                ? keysQuery.error.message
                : 'Failed to load API keys.'
            }
            onRetry={() => keysQuery.refetch()}
          />
        ) : items.length === 0 ? (
          <EmptyState
            title={hasFilters ? 'No matching keys' : 'No API keys yet'}
            description={
              hasFilters
                ? 'No API keys match the current filters.'
                : 'Issue a key to enable programmatic access for this organization.'
            }
            icon={<KeyRound className='w-6 h-6' />}
            action={
              canIssue && !hasFilters ? (
                <PrimaryButton
                  startContent={<Plus className='w-4 h-4' />}
                  onPress={() => setIssueOpen(true)}
                >
                  Issue key
                </PrimaryButton>
              ) : undefined
            }
          />
        ) : (
          <ApiKeysTable
            items={items}
            canRevoke={canRevoke}
            onRevoke={setRevokeTarget}
          />
        )}

        {!keysQuery.isError && total > 0 && (
          <div className='flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between'>
            <PaginationFooter
              page={filters.page}
              pageSize={KEY_PAGE_SIZE}
              total={total}
              noun='key'
              onPageChange={(page) => setFilters({ page })}
              isDisabled={keysQuery.isFetching}
            />
            {canRevoke && (
              <p className='flex items-center gap-1.5 text-[12px] text-default-400'>
                <ShieldCheck className='w-3.5 h-3.5' />
                Revoke is permanent and cannot be undone.
              </p>
            )}
          </div>
        )}

        <IssueModal
          isOpen={issueOpen}
          onClose={() => setIssueOpen(false)}
          grantableCapabilities={grantableCapabilities}
          isIssuing={issueMutation.isPending}
          issueError={issueError}
          onIssue={handleIssue}
        />

        <ConfirmDialog
          isOpen={revokeTarget !== null}
          title='Revoke API key'
          description={
            revokeTarget
              ? `Revoke "${revokeTarget.name}" (prefix ${revokeTarget.key_prefix}…)? This permanently disables the key. Existing clients using it will lose access immediately.`
              : undefined
          }
          confirmLabel='Revoke'
          confirmColor='danger'
          isLoading={revokeMutation.isPending}
          onClose={() => setRevokeTarget(null)}
          onConfirm={handleRevoke}
        />
      </section>
    </CapabilityGate>
  );
}
