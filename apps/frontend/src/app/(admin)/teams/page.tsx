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
} from '@heroui/react';
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { extractApiError } from '@/lib/api-error';
import { Archive, Pencil, Plus, Search, Users } from 'lucide-react';
import * as React from 'react';

import { ConfirmDialog } from '@/components/ui/confirm-dialog';
import { EmptyState } from '@/components/ui/empty-state';
import { ErrorState } from '@/components/ui/error-state';
import { CapabilityGate } from '@/components/ui/capability-gate';
import { PageHeader } from '@/components/ui/page-header';
import { PaginationFooter } from '@/components/ui/pagination-footer';
import { ResponsiveTable } from '@/components/ui/responsive-table';
import { TableRowSkeleton } from '@/components/ui/table-row-skeleton';
import { TimeStamp } from '@/components/ui/time-stamp';
import { useDebouncedValue } from '@/hooks/use-debounced-value';
import {
  useArchiveTeam,
  useCreateTeam,
  useTeams,
  useUpdateTeam,
} from '@/hooks/use-teams';
import { fetchMe, hasCapability, type MeResponse } from '@/lib/auth';
import type { Team, TeamWriteInput } from '@/lib/admin-api';
import { useOrgStore } from '@/lib/org-store';
import { useUrlFilters } from '@/hooks/use-url-filters';

const TEAM_FILTER_DEFAULTS = { search: '', page: 1 };
const TEAM_PAGE_SIZE = 20;

function TeamsTable({
  items,
  canAdmin,
  onEdit,
  onArchive,
}: {
  items: Team[];
  canAdmin: boolean;
  onEdit: (team: Team) => void;
  onArchive: (team: Team) => void;
}) {
  return (
    <ResponsiveTable minWidth={640}>
      <thead>
        <tr className='border-b border-divider'>
          <th className='py-2 px-3 text-default-500 font-medium'>Name</th>
          <th className='py-2 px-3 text-default-500 font-medium'>Slug</th>
          <th className='py-2 px-3 text-default-500 font-medium'>Created</th>
          {canAdmin && (
            <th className='py-2 px-3 text-default-500 font-medium text-right'>
              Actions
            </th>
          )}
        </tr>
      </thead>
      <tbody>
        {items.map((team) => (
          <tr key={team.id} className='border-b border-divider/50'>
            <td
              className='py-2 px-3 text-foreground max-w-[280px] truncate'
              title={team.name}
            >
              {team.name}
            </td>
            <td className='py-2 px-3 font-mono text-xs text-default-700'>
              {team.slug}
            </td>
            <td className='py-2 px-3 text-default-700 whitespace-nowrap'>
              <TimeStamp value={team.created_at} />
            </td>
            {canAdmin && (
              <td className='py-2 px-3'>
                <div className='flex items-center justify-end gap-2'>
                  <Button
                    size='sm'
                    variant='flat'
                    startContent={<Pencil className='w-3.5 h-3.5' />}
                    onPress={() => onEdit(team)}
                  >
                    Edit
                  </Button>
                  <Button
                    size='sm'
                    color='danger'
                    variant='flat'
                    startContent={<Archive className='w-3.5 h-3.5' />}
                    onPress={() => onArchive(team)}
                  >
                    Archive
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

type TeamModalMode = 'create' | 'edit';

interface TeamModalProps {
  isOpen: boolean;
  mode: TeamModalMode;
  initialTeam: Team | null;
  isPending: boolean;
  error: string | null;
  onClose: () => void;
  onSubmit: (input: TeamWriteInput) => Promise<boolean>;
}

function TeamModal({
  isOpen,
  mode,
  initialTeam,
  isPending,
  error,
  onClose,
  onSubmit,
}: TeamModalProps) {
  const [name, setName] = React.useState('');
  const [slug, setSlug] = React.useState('');

  React.useEffect(() => {
    if (!isOpen) {
      setName('');
      setSlug('');

      return;
    }

    if (mode === 'edit' && initialTeam) {
      setName(initialTeam.name);
      setSlug(initialTeam.slug);
    } else {
      setName('');
      setSlug('');
    }
  }, [isOpen, mode, initialTeam]);

  const canSubmit =
    name.trim().length > 0 && slug.trim().length > 0 && !isPending;

  async function handleSubmit() {
    if (!canSubmit) {

      return;
    }

    const ok = await onSubmit({ name: name.trim(), slug: slug.trim() });

    if (ok) {
      onClose();
    }
  }

  const title = mode === 'create' ? 'Create team' : 'Edit team';
  const confirmLabel = mode === 'create' ? 'Create' : 'Save';

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
              {title}
            </ModalHeader>
            <ModalBody>
              <div className='space-y-4'>
                <Input
                  label='Name'
                  labelPlacement='outside'
                  placeholder='Engineering'
                  value={name}
                  onValueChange={setName}
                  maxLength={255}
                  isDisabled={isPending}
                />
                <Input
                  label='Slug'
                  labelPlacement='outside'
                  placeholder='engineering'
                  value={slug}
                  onValueChange={setSlug}
                  description='Lowercase, unique within the organization.'
                  isDisabled={isPending}
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
                {confirmLabel}
              </Button>
            </ModalFooter>
          </>
        )}
      </ModalContent>
    </Modal>
  );
}

export default function TeamsPage() {
  const activeOrgId = useOrgStore((state) => state.activeOrgId);
  const meQuery = useQuery<MeResponse>({
    queryKey: ['auth', 'me'],
    queryFn: fetchMe,
  });

  const capabilities = React.useMemo(
    () => meQuery.data?.capabilities ?? [],
    [meQuery.data?.capabilities],
  );

  const [filters, setFilters] = useUrlFilters(TEAM_FILTER_DEFAULTS);
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
      pageSize: TEAM_PAGE_SIZE,
      search: filters.search || undefined,
    }),
    [filters.page, filters.search],
  );
  const teamsQuery = useTeams(activeOrgId, params, {
    placeholderData: keepPreviousData,
  });

  const createMutation = useCreateTeam(activeOrgId);
  const updateMutation = useUpdateTeam(activeOrgId);
  const archiveMutation = useArchiveTeam(activeOrgId);

  const [modalMode, setModalMode] = React.useState<TeamModalMode>('create');
  const [modalOpen, setModalOpen] = React.useState(false);
  const [editTarget, setEditTarget] = React.useState<Team | null>(null);
  const [modalError, setModalError] = React.useState<string | null>(null);
  const [archiveTarget, setArchiveTarget] = React.useState<Team | null>(null);

  const canAdmin = hasCapability(capabilities, 'teams:admin');

  function openCreate() {
    setModalMode('create');
    setEditTarget(null);
    setModalError(null);
    setModalOpen(true);
  }

  function openEdit(team: Team) {
    setModalMode('edit');
    setEditTarget(team);
    setModalError(null);
    setModalOpen(true);
  }

  async function handleSubmit(input: TeamWriteInput): Promise<boolean> {
    setModalError(null);

    try {
      if (modalMode === 'edit' && editTarget) {
        await updateMutation.mutateAsync({ id: editTarget.id, input });
      } else {
        await createMutation.mutateAsync(input);
      }

      return true;
    } catch (error) {
      setModalError(extractApiError(error, 'Failed to save team.'));

      return false;
    }
  }

  async function handleArchive() {
    if (!archiveTarget) {

      return;
    }

    try {
      await archiveMutation.mutateAsync(archiveTarget.id);
      addToast({ title: 'Team archived', color: 'success' });
      setArchiveTarget(null);
    } catch (error) {
      addToast({
        title: 'Failed to archive team',
        description: extractApiError(error, 'Unexpected error.'),
        color: 'danger',
      });
      setArchiveTarget(null);
    }
  }

  const mutationPending =
    modalMode === 'edit'
      ? updateMutation.isPending
      : createMutation.isPending;

  const isLoading = meQuery.isLoading || teamsQuery.isLoading;
  const items = teamsQuery.data?.results ?? [];
  const total = teamsQuery.data?.count ?? 0;
  const meLoaded = meQuery.data !== undefined;
  const hasSearch = filters.search.length > 0;

  return (
    <CapabilityGate capabilities={capabilities} required='teams:read'>
      <section className='space-y-6'>
        <PageHeader
          title='Teams'
          subtitle='Create, edit, and archive teams within this organization.'
          actions={
            canAdmin ? (
              <Button
                color='primary'
                startContent={<Plus className='w-4 h-4' />}
                onPress={openCreate}
                isDisabled={!meLoaded}
              >
                Create team
              </Button>
            ) : null
          }
        />

        <div className='surface-card p-4'>
          <Input
            aria-label='Search teams'
            placeholder='Search by name or slug…'
            value={searchInput}
            onValueChange={setSearchInput}
            variant='bordered'
            size='sm'
            isClearable
            onClear={() => setSearchInput('')}
            startContent={<Search className='w-4 h-4 text-default-400' />}
            className='max-w-xs'
          />
        </div>

        <div className='surface-card p-2'>
          {isLoading ? (
            <table className='w-full border-collapse text-left text-sm'>
              <thead>
                <tr className='border-b border-divider'>
                  {Array.from({ length: canAdmin ? 4 : 3 }).map((_, index) => (
                    <th
                      key={index}
                      className='py-2 px-3 text-default-500 font-medium'
                    >
                      <span className='inline-block w-16 h-3 rounded-medium bg-content2/60' />
                    </th>
                  ))}
                </tr>
              </thead>
              <TableRowSkeleton columns={canAdmin ? 4 : 3} />
            </table>
          ) : teamsQuery.isError ? (
            <ErrorState
              message={
                teamsQuery.error instanceof Error
                  ? teamsQuery.error.message
                  : 'Failed to load teams.'
              }
              onRetry={() => teamsQuery.refetch()}
            />
          ) : items.length === 0 ? (
            <EmptyState
              title={hasSearch ? 'No matching teams' : 'No teams yet'}
              description={
                hasSearch
                  ? 'No teams match your search.'
                  : 'Create a team to group members and projects within this organization.'
              }
              icon={<Users className='w-6 h-6' />}
              action={
                canAdmin && !hasSearch ? (
                  <Button
                    color='primary'
                    startContent={<Plus className='w-4 h-4' />}
                    onPress={openCreate}
                  >
                    Create team
                  </Button>
                ) : undefined
              }
            />
          ) : (
            <TeamsTable
              items={items}
              canAdmin={canAdmin}
              onEdit={openEdit}
              onArchive={setArchiveTarget}
            />
          )}
        </div>

        {!teamsQuery.isError && total > 0 && (
          <PaginationFooter
            page={filters.page}
            pageSize={TEAM_PAGE_SIZE}
            total={total}
            noun='team'
            onPageChange={(page) => setFilters({ page })}
            isDisabled={teamsQuery.isFetching}
          />
        )}

        <TeamModal
          isOpen={modalOpen}
          mode={modalMode}
          initialTeam={editTarget}
          isPending={mutationPending}
          error={modalError}
          onClose={() => setModalOpen(false)}
          onSubmit={handleSubmit}
        />

        <ConfirmDialog
          isOpen={archiveTarget !== null}
          title='Archive team'
          description={
            archiveTarget
              ? `Archive "${archiveTarget.name}" (${archiveTarget.slug})? It will be hidden from active views. Archived teams are not shown in this list and cannot be restored from the console.`
              : undefined
          }
          confirmLabel='Archive'
          confirmColor='danger'
          isLoading={archiveMutation.isPending}
          onClose={() => setArchiveTarget(null)}
          onConfirm={handleArchive}
        />
      </section>
    </CapabilityGate>
  );
}
