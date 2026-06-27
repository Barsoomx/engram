'use client';

import {
  Button,
  Checkbox,
  CheckboxGroup,
  Input,
  Modal,
  ModalBody,
  ModalContent,
  ModalFooter,
  ModalHeader,
} from '@heroui/react';
import { useQuery } from '@tanstack/react-query';
import axios from 'axios';
import {
  Check,
  Copy,
  KeyRound,
  Plus,
  ShieldAlert,
  ShieldCheck,
  Trash2,
} from 'lucide-react';
import * as React from 'react';

import { ConfirmDialog } from '@/components/ui/confirm-dialog';
import { EmptyState } from '@/components/ui/empty-state';
import { CapabilityGate } from '@/components/ui/capability-gate';
import { PageHeader } from '@/components/ui/page-header';
import { TableRowSkeleton } from '@/components/ui/table-row-skeleton';
import { useApiKeys, useIssueApiKey, useRevokeApiKey } from '@/hooks/use-api-keys';
import { fetchMe, hasCapability, type MeResponse } from '@/lib/auth';
import { useOrgStore } from '@/lib/org-store';
import type { ApiKey } from '@/lib/admin-api';

const KNOWN_CAPABILITY_SUBCODES: Record<string, readonly string[]> = {
  api_keys: ['read', 'issue', 'revoke'],
  memories: ['read', 'write', 'admin'],
  observations: ['read', 'write', 'admin'],
  organizations: ['read', 'write'],
  teams: ['read', 'write', 'admin'],
  projects: ['read', 'write', 'admin'],
  members: ['read', 'write', 'admin'],
  roles: ['read', 'write'],
  audit: ['read'],
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

function deriveStatus(key: ApiKey): 'active' | 'revoked' | 'expired' {
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

const STATUS_STYLES: Record<string, string> = {
  active: 'text-success-600 bg-success-100/60 dark:bg-success-500/15',
  revoked: 'text-danger-600 bg-danger-100/60 dark:bg-danger-500/15',
  expired: 'text-warning-600 bg-warning-100/60 dark:bg-warning-500/15',
};

function StatusBadge({ status }: { status: 'active' | 'revoked' | 'expired' }) {
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium ${STATUS_STYLES[status]}`}
    >
      <span className='inline-block w-1.5 h-1.5 rounded-full bg-current' />
      <span className='capitalize'>{status}</span>
    </span>
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
    <div className='overflow-x-auto'>
      <table className='w-full border-collapse text-left text-sm'>
        <thead>
          <tr className='border-b border-divider'>
            <th className='py-2 px-3 text-default-500 font-medium'>Name</th>
            <th className='py-2 px-3 text-default-500 font-medium'>Prefix</th>
            <th className='py-2 px-3 text-default-500 font-medium'>Fingerprint</th>
            <th className='py-2 px-3 text-default-500 font-medium'>Owner</th>
            <th className='py-2 px-3 text-default-500 font-medium'>Capabilities</th>
            <th className='py-2 px-3 text-default-500 font-medium'>Created</th>
            <th className='py-2 px-3 text-default-500 font-medium'>Last used</th>
            <th className='py-2 px-3 text-default-500 font-medium'>Status</th>
            {canRevoke && (
              <th className='py-2 px-3 text-default-500 font-medium text-right'>
                Actions
              </th>
            )}
          </tr>
        </thead>
        <tbody>
          {items.map((key) => {
            const status = deriveStatus(key);

            return (
              <tr key={key.id} className='border-b border-divider/50'>
                <td className='py-2 px-3 text-foreground'>{key.name}</td>
                <td className='py-2 px-3 font-mono text-xs text-default-700'>
                  {key.key_prefix}…
                </td>
                <td className='py-2 px-3 font-mono text-xs text-default-500'>
                  {key.key_fingerprint}
                </td>
                <td className='py-2 px-3 text-default-700'>
                  {key.owner_identity?.display_name ?? '—'}
                </td>
                <td className='py-2 px-3'>
                  <ul className='flex flex-wrap gap-1'>
                    {key.capabilities.map((capability) => (
                      <li
                        key={capability}
                        className='text-xs px-1.5 py-0.5 rounded-medium bg-content2 text-foreground'
                      >
                        {capability}
                      </li>
                    ))}
                  </ul>
                </td>
                <td className='py-2 px-3 text-default-700 whitespace-nowrap'>
                  {formatDateTime(key.created_at)}
                </td>
                <td className='py-2 px-3 text-default-700 whitespace-nowrap'>
                  {formatDateTime(key.last_used_at)}
                </td>
                <td className='py-2 px-3'>
                  <StatusBadge status={status} />
                </td>
                {canRevoke && (
                  <td className='py-2 px-3 text-right'>
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
                  </td>
                )}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

interface IssueModalProps {
  isOpen: boolean;
  onClose: () => void;
  grantableCapabilities: string[];
  isIssuing: boolean;
  issueError: string | null;
  onIssue: (input: { name: string; capabilities: string[] }) => Promise<
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
  const [selectedCapabilities, setSelectedCapabilities] = React.useState<string[]>([]);
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

    const result = await onIssue({
      name: name.trim(),
      capabilities: selectedCapabilities,
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
      isDismissable={!isIssuing}
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
                      copied ? <Check className='w-4 h-4' /> : <Copy className='w-4 h-4' />
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
                              <span className='font-mono text-xs'>{capability}</span>
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

  const params = React.useMemo(() => ({ page: 1, pageSize: 50 }), []);
  const keysQuery = useApiKeys(activeOrgId, params);

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
  }): Promise<{ plaintext: string; key_prefix: string; key_fingerprint: string } | null> {
    setIssueError(null);

    try {
      const result = await issueMutation.mutateAsync({
        name: input.name,
        capabilities: input.capabilities,
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
  const meLoaded = meQuery.data !== undefined;

  return (
    <CapabilityGate capabilities={capabilities} required='api_keys:read'>
      <section className='space-y-6'>
        <PageHeader
          title='API Keys'
          subtitle='Provision and revoke organization API keys.'
          actions={
            canIssue ? (
              <Button
                color='primary'
                startContent={<Plus className='w-4 h-4' />}
                onPress={() => setIssueOpen(true)}
                isDisabled={!meLoaded}
              >
                Issue key
              </Button>
            ) : null
          }
        />

        <div className='surface-card p-2'>
          {isLoading ? (
            <table className='w-full border-collapse text-left text-sm'>
              <thead>
                <tr className='border-b border-divider'>
                  {Array.from({ length: 8 }).map((_, index) => (
                    <th key={index} className='py-2 px-3 text-default-500 font-medium'>
                      <span className='inline-block w-16 h-3 rounded-medium bg-content2/60' />
                    </th>
                  ))}
                </tr>
              </thead>
              <TableRowSkeleton columns={8} />
            </table>
          ) : items.length === 0 ? (
            <EmptyState
              title='No API keys yet'
              description='Issue a key to enable programmatic access for this organization.'
              icon={<KeyRound className='w-6 h-6' />}
              action={
                canIssue ? (
                  <Button
                    color='primary'
                    startContent={<Plus className='w-4 h-4' />}
                    onPress={() => setIssueOpen(true)}
                  >
                    Issue key
                  </Button>
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
        </div>

        {items.length > 0 && (
          <div className='flex items-center justify-between text-xs text-default-500'>
            <p>
              Showing {items.length} key{items.length === 1 ? '' : 's'}.
            </p>
            {canRevoke && (
              <p className='flex items-center gap-1'>
                <ShieldCheck className='w-3.5 h-3.5' />
                Revoke is permanent and cannot be undone.
              </p>
            )}
          </div>
        )}

        {keysQuery.isError && (
          <pre className='text-sm text-danger-500 bg-danger-50 dark:bg-danger-500/10 rounded-medium p-3'>
            {keysQuery.error instanceof Error
              ? keysQuery.error.message
              : 'Failed to load API keys.'}
          </pre>
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
