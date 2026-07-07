'use client';

import {
  Button,
  Input,
  Modal,
  ModalBody,
  ModalContent,
  ModalFooter,
  ModalHeader,
} from '@heroui/react';
import { useQuery } from '@tanstack/react-query';
import axios from 'axios';
import { Building2, Pencil } from 'lucide-react';
import * as React from 'react';

import { CapabilityGate } from '@/components/ui/capability-gate';
import { EmptyState } from '@/components/ui/empty-state';
import { ErrorState } from '@/components/ui/error-state';
import { PageHeader } from '@/components/ui/page-header';
import { ResponsiveTable } from '@/components/ui/responsive-table';
import { StatusPill } from '@/components/ui/status-pill';
import { TableRowSkeleton } from '@/components/ui/table-row-skeleton';
import { TimeStamp } from '@/components/ui/time-stamp';
import { useOrganizations, useUpdateOrganization } from '@/hooks/use-organizations';
import { fetchMe, hasCapability, type MeResponse } from '@/lib/auth';
import type { Organization, OrganizationWriteInput } from '@/lib/admin-api';
import { useOrgStore } from '@/lib/org-store';

const COLUMN_COUNT = 6;

function OrganizationsTable({
  items,
  canAdmin,
  onEdit,
}: {
  items: Organization[];
  canAdmin: boolean;
  onEdit: (organization: Organization) => void;
}) {
  return (
    <ResponsiveTable minWidth={880}>
      <thead>
        <tr className='border-b border-divider'>
          <th className='py-2 px-3 text-default-500 font-medium'>Name</th>
          <th className='py-2 px-3 text-default-500 font-medium'>Slug</th>
          <th className='py-2 px-3 text-default-500 font-medium'>Status</th>
          <th className='py-2 px-3 text-default-500 font-medium'>Members</th>
          <th className='py-2 px-3 text-default-500 font-medium'>Your role</th>
          <th className='py-2 px-3 text-default-500 font-medium'>Created</th>
          {canAdmin && (
            <th className='py-2 px-3 text-default-500 font-medium text-right'>
              Actions
            </th>
          )}
        </tr>
      </thead>
      <tbody>
        {items.map((organization) => (
          <tr key={organization.id} className='border-b border-divider/50'>
            <td className='py-2 px-3 text-foreground'>{organization.name}</td>
            <td className='py-2 px-3 font-mono text-xs text-default-700'>
              {organization.slug}
            </td>
            <td className='py-2 px-3'>
              {organization.status ? (
                <StatusPill status={organization.status} />
              ) : (
                <span className='text-default-400 text-xs'>—</span>
              )}
            </td>
            <td className='py-2 px-3 tnum text-default-700'>
              {typeof organization.member_count === 'number'
                ? organization.member_count
                : '—'}
            </td>
            <td className='py-2 px-3 text-default-700'>
              {organization.viewer_role ?? '—'}
            </td>
            <td className='py-2 px-3 text-default-700 whitespace-nowrap'>
              <TimeStamp value={organization.created_at} />
            </td>
            {canAdmin && (
              <td className='py-2 px-3'>
                <div className='flex items-center justify-end gap-2'>
                  <Button
                    size='sm'
                    variant='flat'
                    startContent={<Pencil className='w-3.5 h-3.5' />}
                    onPress={() => onEdit(organization)}
                  >
                    Edit
                  </Button>
                </div>
              </td>
            )}
          </tr>
        ))}
      </tbody>
    </ResponsiveTable>
  );
}

interface EditOrganizationModalProps {
  isOpen: boolean;
  initialOrganization: Organization | null;
  isPending: boolean;
  error: string | null;
  onClose: () => void;
  onSubmit: (input: OrganizationWriteInput) => Promise<boolean>;
}

function EditOrganizationModal({
  isOpen,
  initialOrganization,
  isPending,
  error,
  onClose,
  onSubmit,
}: EditOrganizationModalProps) {
  const [name, setName] = React.useState('');

  React.useEffect(() => {
    if (!isOpen) {
      setName('');

      return;
    }

    if (initialOrganization) {
      setName(initialOrganization.name);
    } else {
      setName('');
    }
  }, [isOpen, initialOrganization]);

  const canSubmit = name.trim().length > 0 && !isPending;

  async function handleSubmit() {
    if (!canSubmit) {

      return;
    }

    const ok = await onSubmit({ name: name.trim() });

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
              Edit organization
            </ModalHeader>
            <ModalBody>
              <div className='space-y-4'>
                <Input
                  label='Name'
                  labelPlacement='outside'
                  placeholder='Acme Inc.'
                  value={name}
                  onValueChange={setName}
                  maxLength={255}
                  isDisabled={isPending}
                />
                <Input
                  label='Slug'
                  labelPlacement='outside'
                  value={initialOrganization?.slug ?? ''}
                  isReadOnly
                  description='The slug is immutable and cannot be changed.'
                />
                {error && (
                  <div className='rounded-medium bg-danger-50 dark:bg-danger-500/10 border border-danger-200 dark:border-danger-500/30 p-3'>
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
                Save
              </Button>
            </ModalFooter>
          </>
        )}
      </ModalContent>
    </Modal>
  );
}

export default function OrganizationsPage() {
  const activeOrgId = useOrgStore((state) => state.activeOrgId);
  const meQuery = useQuery<MeResponse>({
    queryKey: ['auth', 'me'],
    queryFn: fetchMe,
  });

  const capabilities = React.useMemo(
    () => meQuery.data?.capabilities ?? [],
    [meQuery.data?.capabilities],
  );

  const organizationsQuery = useOrganizations(activeOrgId);
  const updateMutation = useUpdateOrganization(activeOrgId);

  const [editTarget, setEditTarget] = React.useState<Organization | null>(null);
  const [modalOpen, setModalOpen] = React.useState(false);
  const [modalError, setModalError] = React.useState<string | null>(null);
  const [search, setSearch] = React.useState('');

  const canAdmin = hasCapability(capabilities, 'organizations:admin');

  function openEdit(organization: Organization) {
    setEditTarget(organization);
    setModalError(null);
    setModalOpen(true);
  }

  async function handleSubmit(input: OrganizationWriteInput): Promise<boolean> {
    setModalError(null);

    if (!editTarget) {

      return false;
    }

    try {
      await updateMutation.mutateAsync({ id: editTarget.id, input });

      return true;
    } catch (error) {
      let detail: string | undefined;

      if (axios.isAxiosError(error)) {
        const data = error.response?.data as { detail?: string } | undefined;

        detail = data?.detail;
      }

      setModalError(detail ?? 'Failed to save organization.');

      return false;
    }
  }

  const isLoading = meQuery.isLoading || organizationsQuery.isLoading;
  const items = organizationsQuery.data?.results ?? [];
  const meLoaded = meQuery.data !== undefined;
  const skeletonColumns = canAdmin ? COLUMN_COUNT + 1 : COLUMN_COUNT;

  const normalizedSearch = search.trim().toLowerCase();
  const filteredItems = normalizedSearch
    ? items.filter(
        (organization) =>
          organization.name.toLowerCase().includes(normalizedSearch) ||
          organization.slug.toLowerCase().includes(normalizedSearch),
      )
    : items;

  return (
    <CapabilityGate capabilities={capabilities} required='organizations:read'>
      <section className='space-y-6'>
        <PageHeader
          title='Organizations'
          subtitle='View the organizations you belong to and edit their display name.'
        />

        <div className='surface-card p-4'>
          <Input
            label='Search'
            labelPlacement='outside'
            placeholder='Name or slug…'
            value={search}
            onValueChange={setSearch}
            variant='bordered'
            size='sm'
            isClearable
            onClear={() => setSearch('')}
            className='max-w-xs'
          />
        </div>

        <div className='surface-card p-2'>
          {isLoading ? (
            <table className='w-full border-collapse text-left text-sm'>
              <thead>
                <tr className='border-b border-divider'>
                  {Array.from({ length: skeletonColumns }).map((_, index) => (
                    <th
                      key={index}
                      className='py-2 px-3 text-default-500 font-medium'
                    >
                      <span className='inline-block w-16 h-3 rounded-medium bg-content2/60' />
                    </th>
                  ))}
                </tr>
              </thead>
              <TableRowSkeleton columns={skeletonColumns} />
            </table>
          ) : organizationsQuery.isError ? (
            <ErrorState
              message={
                organizationsQuery.error instanceof Error
                  ? organizationsQuery.error.message
                  : 'Failed to load organizations.'
              }
              onRetry={() => organizationsQuery.refetch()}
            />
          ) : filteredItems.length === 0 ? (
            <EmptyState
              title={
                normalizedSearch ? 'No matching organizations' : 'No organizations'
              }
              description={
                normalizedSearch
                  ? 'No organizations match your search.'
                  : 'You do not belong to any organization yet.'
              }
              icon={<Building2 className='w-6 h-6' />}
            />
          ) : (
            <OrganizationsTable
              items={filteredItems}
              canAdmin={canAdmin}
              onEdit={openEdit}
            />
          )}
        </div>

        {!organizationsQuery.isError && filteredItems.length > 0 && (
          <div className='flex items-center justify-between text-xs text-default-500'>
            <p>
              Showing {filteredItems.length} organization
              {filteredItems.length === 1 ? '' : 's'}
              {normalizedSearch ? ` of ${items.length}` : ''}.
            </p>
          </div>
        )}

        {canAdmin && meLoaded && (
          <EditOrganizationModal
            isOpen={modalOpen}
            initialOrganization={editTarget}
            isPending={updateMutation.isPending}
            error={modalError}
            onClose={() => setModalOpen(false)}
            onSubmit={handleSubmit}
          />
        )}
      </section>
    </CapabilityGate>
  );
}
