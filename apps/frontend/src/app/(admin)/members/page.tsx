'use client';

import {
  addToast,
  Button,
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
import { useQuery } from '@tanstack/react-query';
import axios from 'axios';
import {
  ShieldCheck,
  UserCog,
  UserMinus,
  UserPlus,
  Users,
} from 'lucide-react';
import * as React from 'react';

import { ConfirmDialog } from '@/components/ui/confirm-dialog';
import { EmptyState } from '@/components/ui/empty-state';
import { CapabilityGate } from '@/components/ui/capability-gate';
import { InitialTile } from '@/components/ui/initial-tile';
import { PageHeader } from '@/components/ui/page-header';
import { PrimaryButton } from '@/components/ui/primary-button';
import {
  useDeactivateMember,
  useInviteMember,
  useMembers,
  useRoles,
  useUpdateMemberRole,
} from '@/hooks/use-members';
import { extractApiError } from '@/lib/api-error';
import { fetchMe, hasCapability, type MeResponse } from '@/lib/auth';
import type {
  Member,
  MemberInviteInput,
  MemberRoleInput,
  MembershipStatus,
  Role,
} from '@/lib/admin-api';
import { useOrgStore } from '@/lib/org-store';

const LAST_OWNER_MESSAGE =
  'Cannot remove the last organization owner.';

function isLastOwnerError(error: unknown): boolean {
  if (!axios.isAxiosError(error)) {

    return false;
  }

  const status = error.response?.status;
  const data = error.response?.data as { code?: string } | undefined;

  return status === 409 && data?.code === 'last_owner';
}

function memberInitialError(error: unknown): string | null {
  if (isLastOwnerError(error)) {

    return LAST_OWNER_MESSAGE;
  }

  if (axios.isAxiosError(error) && error.response) {

    return extractApiError(error);
  }

  return null;
}

function getDisplayName(member: Member): string {
  const name = member.display_name?.trim();

  if (name) {

    return name;
  }

  const email = member.email?.trim();

  if (email) {

    return email;
  }

  return member.external_id || member.id;
}

function getPrimaryIdentity(member: Member): string {
  const email = member.email?.trim();

  if (email) {

    return email;
  }

  return member.external_id || '—';
}

function getSeed(member: Member): string {
  return member.email?.trim() || member.external_id || member.id;
}

function humanizeRole(role: string): string {
  return role
    .split(/[_\-\s]+/)
    .filter(Boolean)
    .map((word) => word[0].toUpperCase() + word.slice(1))
    .join(' ');
}

function rolePillClass(role: string): string {
  const value = role.toLowerCase();

  if (value.includes('owner')) {

    return 'bg-primary-soft text-primary-300';
  }

  if (value.includes('admin')) {

    return 'bg-[rgba(107,166,255,0.13)] text-info';
  }

  return 'bg-content3 text-default-500';
}

function statusChipColor(
  status: MembershipStatus,
): 'success' | 'warning' | 'danger' {
  if (status === 'active') {
    return 'success';
  }

  if (status === 'invited') {
    return 'warning';
  }

  return 'danger';
}

function statusLabel(status: MembershipStatus): string {
  if (status === 'active') {
    return 'Active';
  }

  if (status === 'invited') {
    return 'Invited';
  }

  return 'Suspended';
}

function gridColumns(canAdmin: boolean): string {
  return canAdmin
    ? 'minmax(0,2fr) minmax(0,1fr) minmax(0,1fr) auto'
    : 'minmax(0,2fr) minmax(0,1fr) minmax(0,1fr)';
}

function ColumnHeader({ canAdmin }: { canAdmin: boolean }) {
  return (
    <div
      className='grid items-center gap-4 border-b border-divider px-5 py-3 text-[10.5px] font-semibold uppercase tracking-[0.1em] text-default-400'
      style={{ gridTemplateColumns: gridColumns(canAdmin) }}
    >
      <span>Member</span>
      <span>Role</span>
      <span>Status</span>
      {canAdmin && <span className='sr-only'>Actions</span>}
    </div>
  );
}

function MembersTable({
  items,
  canAdmin,
  roleNames,
  onChangeRole,
  onDeactivate,
}: {
  items: Member[];
  canAdmin: boolean;
  roleNames: Map<string, string>;
  onChangeRole: (member: Member) => void;
  onDeactivate: (member: Member) => void;
}) {
  return (
    <div className='surface-card overflow-hidden'>
      <div className='overflow-x-auto'>
        <div className='min-w-[640px]'>
          <ColumnHeader canAdmin={canAdmin} />
          {items.map((member) => (
            <div
              key={member.id}
              className='grid items-center gap-4 border-b border-divider px-5 py-3.5 transition-colors last:border-b-0 hover:bg-content2/60'
              style={{ gridTemplateColumns: gridColumns(canAdmin) }}
            >
              <div className='flex min-w-0 items-center gap-3'>
                <InitialTile
                  name={getDisplayName(member)}
                  seed={getSeed(member)}
                  size={34}
                />
                <div className='flex min-w-0 flex-col'>
                  <span className='truncate text-[13.5px] font-semibold text-foreground'>
                    {getDisplayName(member)}
                  </span>
                  <span className='truncate font-mono text-[11.5px] text-default-400'>
                    {getPrimaryIdentity(member)}
                  </span>
                </div>
              </div>
              <div className='min-w-0'>
                <span
                  className={`inline-flex max-w-full items-center truncate rounded-[7px] px-2.5 py-1 text-[11.5px] font-medium ${rolePillClass(member.role)}`}
                >
                  {member.role_name ?? roleNames.get(member.role) ?? humanizeRole(member.role)}
                </span>
              </div>
              <div>
                {member.status ? (
                  <Chip
                    size='sm'
                    variant='flat'
                    color={statusChipColor(member.status)}
                  >
                    {statusLabel(member.status)}
                  </Chip>
                ) : (
                  <span className='text-[12px] text-default-500'>
                    {member.active ? 'Active' : 'Inactive'}
                  </span>
                )}
              </div>
              {canAdmin && (
                <div className='flex items-center justify-end gap-2'>
                  <Button
                    size='sm'
                    variant='flat'
                    startContent={<UserCog className='w-3.5 h-3.5' />}
                    onPress={() => onChangeRole(member)}
                  >
                    Change role
                  </Button>
                  <Button
                    size='sm'
                    color='danger'
                    variant='flat'
                    startContent={<UserMinus className='w-3.5 h-3.5' />}
                    onPress={() => onDeactivate(member)}
                    isDisabled={!member.active}
                  >
                    Deactivate
                  </Button>
                </div>
              )}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function MembersTableSkeleton({ canAdmin }: { canAdmin: boolean }) {
  return (
    <div className='surface-card overflow-hidden'>
      <div className='overflow-x-auto'>
        <div className='min-w-[640px]'>
          <ColumnHeader canAdmin={canAdmin} />
          {Array.from({ length: 6 }).map((_, index) => (
            <div
              key={index}
              className='grid items-center gap-4 border-b border-divider px-5 py-3.5 last:border-b-0'
              style={{ gridTemplateColumns: gridColumns(canAdmin) }}
            >
              <div className='flex min-w-0 items-center gap-3'>
                <span className='h-[34px] w-[34px] shrink-0 rounded-[10px] bg-content2' />
                <div className='flex flex-col gap-1.5'>
                  <span className='h-3.5 w-28 rounded-medium bg-content2' />
                  <span className='h-3 w-36 rounded-medium bg-content2' />
                </div>
              </div>
              <span className='h-5 w-16 rounded-[7px] bg-content2' />
              <span className='h-3 w-14 rounded-medium bg-content2' />
              {canAdmin && (
                <div className='flex items-center justify-end gap-2'>
                  <span className='h-8 w-20 rounded-medium bg-content2' />
                  <span className='h-8 w-24 rounded-medium bg-content2' />
                </div>
              )}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

type MemberModalMode = 'invite' | 'role';

interface MemberModalProps {
  isOpen: boolean;
  mode: MemberModalMode;
  initialMember: Member | null;
  roles: Role[];
  isPending: boolean;
  error: string | null;
  onClose: () => void;
  onInvite: (input: MemberInviteInput) => Promise<boolean>;
  onRoleChange: (input: MemberRoleInput) => Promise<boolean>;
}

function MemberModal({
  isOpen,
  mode,
  initialMember,
  roles,
  isPending,
  error,
  onClose,
  onInvite,
  onRoleChange,
}: MemberModalProps) {
  const [externalId, setExternalId] = React.useState('');
  const [displayName, setDisplayName] = React.useState('');
  const [email, setEmail] = React.useState('');
  const [role, setRole] = React.useState('');

  React.useEffect(() => {
    if (!isOpen) {
      setExternalId('');
      setDisplayName('');
      setEmail('');
      setRole('');

      return;
    }

    if (mode === 'role' && initialMember) {
      setRole(initialMember.role);
    } else if (roles.length > 0) {
      setRole(roles[0].code);
    } else {
      setRole('');
    }
  }, [isOpen, mode, initialMember, roles]);

  const isInvite = mode === 'invite';
  const canSubmit = isInvite
    ? externalId.trim().length > 0 &&
      displayName.trim().length > 0 &&
      role.length > 0 &&
      !isPending
    : role.length > 0 && !isPending;

  async function handleSubmit() {
    if (!canSubmit) {

      return;
    }

    let ok = false;

    if (isInvite) {
      ok = await onInvite({
        external_id: externalId.trim(),
        display_name: displayName.trim(),
        email: email.trim(),
        role,
      });
    } else {
      ok = await onRoleChange({ role });
    }

    if (ok) {
      onClose();
    }
  }

  const title = isInvite ? 'Invite member' : 'Change role';

  const roleItems = React.useMemo(() => roles, [roles]);

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
                {isInvite && (
                  <>
                    <Input
                      label='External ID'
                      labelPlacement='outside'
                      placeholder='user-123'
                      value={externalId}
                      onValueChange={setExternalId}
                      maxLength={255}
                      isDisabled={isPending}
                    />
                    <Input
                      label='Display name'
                      labelPlacement='outside'
                      placeholder='Ada Lovelace'
                      value={displayName}
                      onValueChange={setDisplayName}
                      maxLength={255}
                      isDisabled={isPending}
                    />
                    <Input
                      label='Email'
                      labelPlacement='outside'
                      placeholder='ada@example.com'
                      type='email'
                      value={email}
                      onValueChange={setEmail}
                      isDisabled={isPending}
                    />
                  </>
                )}
                {!isInvite && initialMember && (
                  <div className='rounded-medium bg-content2/60 p-3 text-sm'>
                    <p className='text-default-500'>Member</p>
                    <p className='text-foreground font-medium'>
                      {getDisplayName(initialMember)}
                    </p>
                    <p className='text-default-500 font-mono text-xs mt-1'>
                      {getPrimaryIdentity(initialMember)}
                    </p>
                  </div>
                )}
                <Select
                  label='Role'
                  labelPlacement='outside'
                  placeholder='Select a role'
                  items={roleItems}
                  selectedKeys={role ? new Set([role]) : new Set()}
                  isDisabled={isPending || roleItems.length === 0}
                  description={
                    roleItems.length === 0
                      ? 'No roles available.'
                      : undefined
                  }
                  onSelectionChange={(keys) => {
                    const next = Array.from(keys)[0];

                    if (typeof next === 'string') {
                      setRole(next);
                    }
                  }}
                >
                  {(roleItem: Role) => (
                    <SelectItem key={roleItem.code}>
                      {roleItem.name}
                    </SelectItem>
                  )}
                </Select>
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
                {isInvite ? 'Invite' : 'Save'}
              </Button>
            </ModalFooter>
          </>
        )}
      </ModalContent>
    </Modal>
  );
}

export default function MembersPage() {
  const activeOrgId = useOrgStore((state) => state.activeOrgId);
  const meQuery = useQuery<MeResponse>({
    queryKey: ['auth', 'me'],
    queryFn: fetchMe,
  });

  const capabilities = React.useMemo(
    () => meQuery.data?.capabilities ?? [],
    [meQuery.data?.capabilities],
  );

  const [memberPage, setMemberPage] = React.useState(1);

  React.useEffect(() => {
    setMemberPage(1);
  }, [activeOrgId]);

  const params = React.useMemo(
    () => ({ page: memberPage, pageSize: 50 }),
    [memberPage],
  );
  const roleParams = React.useMemo(() => ({ page: 1, pageSize: 100 }), []);
  const membersQuery = useMembers(activeOrgId, params);
  const rolesQuery = useRoles(activeOrgId, roleParams);

  const inviteMutation = useInviteMember(activeOrgId);
  const roleMutation = useUpdateMemberRole(activeOrgId);
  const deactivateMutation = useDeactivateMember(activeOrgId);

  const [modalMode, setModalMode] = React.useState<MemberModalMode>('invite');
  const [modalOpen, setModalOpen] = React.useState(false);
  const [memberTarget, setMemberTarget] = React.useState<Member | null>(null);
  const [modalError, setModalError] = React.useState<string | null>(null);
  const [deactivateTarget, setDeactivateTarget] =
    React.useState<Member | null>(null);

  const canAdmin = hasCapability(capabilities, 'members:admin');

  const roles = React.useMemo(
    () => rolesQuery.data?.results ?? [],
    [rolesQuery.data?.results],
  );

  const roleNames = React.useMemo(() => {
    const map = new Map<string, string>();

    for (const role of roles) {
      map.set(role.code, role.name);
    }

    return map;
  }, [roles]);

  function openInvite() {
    setModalMode('invite');
    setMemberTarget(null);
    setModalError(null);
    setModalOpen(true);
  }

  function openChangeRole(member: Member) {
    setModalMode('role');
    setMemberTarget(member);
    setModalError(null);
    setModalOpen(true);
  }

  async function handleInvite(
    input: MemberInviteInput,
  ): Promise<boolean> {
    setModalError(null);

    try {
      await inviteMutation.mutateAsync(input);

      return true;
    } catch (error) {
      const detail = memberInitialError(error);

      setModalError(detail ?? 'Failed to invite member.');

      return false;
    }
  }

  async function handleRoleChange(
    input: MemberRoleInput,
  ): Promise<boolean> {
    setModalError(null);

    if (!memberTarget) {

      return false;
    }

    try {
      await roleMutation.mutateAsync({
        id: memberTarget.id,
        input,
      });

      return true;
    } catch (error) {
      const detail = memberInitialError(error);

      setModalError(detail ?? 'Failed to change role.');

      return false;
    }
  }

  async function handleDeactivate() {
    if (!deactivateTarget) {

      return;
    }

    try {
      await deactivateMutation.mutateAsync(deactivateTarget.id);
      setDeactivateTarget(null);
    } catch (error) {
      if (isLastOwnerError(error)) {
        addToast({
          title: 'Action not allowed',
          description: LAST_OWNER_MESSAGE,
          color: 'danger',
        });
      } else {
        const detail = memberInitialError(error);

        addToast({
          title: 'Failed to deactivate member',
          description: detail ?? 'Unexpected error.',
          color: 'danger',
        });
      }
      setDeactivateTarget(null);
    }
  }

  const mutationPending =
    modalMode === 'invite'
      ? inviteMutation.isPending
      : roleMutation.isPending;

  const isLoading =
    meQuery.isLoading || membersQuery.isLoading;
  const items = membersQuery.data?.results ?? [];
  const total = membersQuery.data?.count ?? 0;
  const meLoaded = meQuery.data !== undefined;

  return (
    <CapabilityGate capabilities={capabilities} required='members:read'>
      <section className='space-y-6'>
        <PageHeader
          title='Members'
          subtitle='Invite members, assign roles, and deactivate access within this organization.'
          actions={
            canAdmin ? (
              <PrimaryButton
                startContent={<UserPlus className='w-4 h-4' />}
                onPress={openInvite}
                isDisabled={!meLoaded}
              >
                Invite member
              </PrimaryButton>
            ) : null
          }
        />

        {isLoading ? (
          <MembersTableSkeleton canAdmin={canAdmin} />
        ) : items.length === 0 ? (
          <EmptyState
            title='No members yet'
            description='Invite the first member to grant them access to this organization.'
            icon={<Users className='w-6 h-6' />}
            action={
              canAdmin ? (
                <PrimaryButton
                  startContent={<UserPlus className='w-4 h-4' />}
                  onPress={openInvite}
                >
                  Invite member
                </PrimaryButton>
              ) : undefined
            }
          />
        ) : (
          <MembersTable
            items={items}
            canAdmin={canAdmin}
            roleNames={roleNames}
            onChangeRole={openChangeRole}
            onDeactivate={setDeactivateTarget}
          />
        )}

        {items.length > 0 && (
          <div className='flex items-center justify-between text-[12px] text-default-400'>
            <div className='flex items-center gap-3'>
              <p>
                {total > 0
                  ? `Showing ${(memberPage - 1) * 50 + 1}-${(memberPage - 1) * 50 + items.length} of ${total}`
                  : `Showing ${items.length} member${items.length === 1 ? '' : 's'}`}
              </p>
              <button
                type='button'
                onClick={() => setMemberPage((p) => Math.max(1, p - 1))}
                disabled={memberPage === 1}
                className='rounded-[8px] border border-divider bg-content1 px-2.5 py-1 font-medium text-default-600 transition-colors hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50'
              >
                Previous
              </button>
              <button
                type='button'
                onClick={() => setMemberPage((p) => p + 1)}
                disabled={memberPage * 50 >= total}
                className='rounded-[8px] border border-divider bg-content1 px-2.5 py-1 font-medium text-default-600 transition-colors hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50'
              >
                Next
              </button>
            </div>
            {canAdmin && (
              <p className='flex items-center gap-1.5'>
                <ShieldCheck className='w-3.5 h-3.5' />
                Deactivating is restricted to administrators.
              </p>
            )}
          </div>
        )}

        {membersQuery.isError && (
          <pre className='rounded-[10px] border border-danger-500/30 bg-danger-500/10 p-3 text-sm text-danger-500'>
            {membersQuery.error instanceof Error
              ? membersQuery.error.message
              : 'Failed to load members.'}
          </pre>
        )}

        <MemberModal
          isOpen={modalOpen}
          mode={modalMode}
          initialMember={memberTarget}
          roles={roles}
          isPending={mutationPending}
          error={modalError}
          onClose={() => setModalOpen(false)}
          onInvite={handleInvite}
          onRoleChange={handleRoleChange}
        />

        <ConfirmDialog
          isOpen={deactivateTarget !== null}
          title='Deactivate member'
          description={
            deactivateTarget
              ? `Deactivate "${getDisplayName(deactivateTarget)}"? They will lose access to this organization immediately.`
              : undefined
          }
          confirmLabel='Deactivate'
          confirmColor='danger'
          isLoading={deactivateMutation.isPending}
          onClose={() => setDeactivateTarget(null)}
          onConfirm={handleDeactivate}
        />
      </section>
    </CapabilityGate>
  );
}
