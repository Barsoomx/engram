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
import { PageHeader } from '@/components/ui/page-header';
import { TableRowSkeleton } from '@/components/ui/table-row-skeleton';
import {
  useDeactivateMember,
  useInviteMember,
  useMembers,
  useRoles,
  useUpdateMemberRole,
} from '@/hooks/use-members';
import { fetchMe, hasCapability, type MeResponse } from '@/lib/auth';
import type {
  Member,
  MemberInviteInput,
  MemberRoleInput,
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

  if (axios.isAxiosError(error)) {
    const data = error.response?.data as { detail?: string } | undefined;

    if (data?.detail) {

      return data.detail;
    }
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

function MembersTable({
  items,
  canAdmin,
  onChangeRole,
  onDeactivate,
}: {
  items: Member[];
  canAdmin: boolean;
  onChangeRole: (member: Member) => void;
  onDeactivate: (member: Member) => void;
}) {
  return (
    <div className='overflow-x-auto'>
      <table className='w-full border-collapse text-left text-sm'>
        <thead>
          <tr className='border-b border-divider'>
            <th className='py-2 px-3 text-default-500 font-medium'>Member</th>
            <th className='py-2 px-3 text-default-500 font-medium'>
              Identity
            </th>
            <th className='py-2 px-3 text-default-500 font-medium'>Role</th>
            <th className='py-2 px-3 text-default-500 font-medium'>Status</th>
            {canAdmin && (
              <th className='py-2 px-3 text-default-500 font-medium text-right'>
                Actions
              </th>
            )}
          </tr>
        </thead>
        <tbody>
          {items.map((member) => (
            <tr key={member.id} className='border-b border-divider/50'>
              <td className='py-2 px-3 text-foreground'>
                {getDisplayName(member)}
              </td>
              <td className='py-2 px-3 font-mono text-xs text-default-700'>
                {getPrimaryIdentity(member)}
              </td>
              <td className='py-2 px-3'>
                <Chip size='sm' variant='flat' className='font-mono'>
                  {member.role}
                </Chip>
              </td>
              <td className='py-2 px-3'>
                <Chip
                  size='sm'
                  color={member.active ? 'success' : 'default'}
                  variant='flat'
                >
                  {member.active ? 'Active' : 'Inactive'}
                </Chip>
              </td>
              {canAdmin && (
                <td className='py-2 px-3'>
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
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
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

  const params = React.useMemo(() => ({ page: 1, pageSize: 50 }), []);
  const membersQuery = useMembers(activeOrgId, params);
  const rolesQuery = useRoles(activeOrgId, params);

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
  const meLoaded = meQuery.data !== undefined;

  return (
    <CapabilityGate capabilities={capabilities} required='members:read'>
      <section className='space-y-6'>
        <PageHeader
          title='Members'
          subtitle='Invite members, assign roles, and deactivate access within this organization.'
          actions={
            canAdmin ? (
              <Button
                color='primary'
                startContent={<UserPlus className='w-4 h-4' />}
                onPress={openInvite}
                isDisabled={!meLoaded}
              >
                Invite member
              </Button>
            ) : null
          }
        />

        <div className='surface-card p-2'>
          {isLoading ? (
            <table className='w-full border-collapse text-left text-sm'>
              <thead>
                <tr className='border-b border-divider'>
                  {Array.from({ length: canAdmin ? 5 : 4 }).map((_, index) => (
                    <th
                      key={index}
                      className='py-2 px-3 text-default-500 font-medium'
                    >
                      <span className='inline-block w-16 h-3 rounded-medium bg-content2/60' />
                    </th>
                  ))}
                </tr>
              </thead>
              <TableRowSkeleton columns={canAdmin ? 5 : 4} />
            </table>
          ) : items.length === 0 ? (
            <EmptyState
              title='No members yet'
              description='Invite the first member to grant them access to this organization.'
              icon={<Users className='w-6 h-6' />}
              action={
                canAdmin ? (
                  <Button
                    color='primary'
                    startContent={<UserPlus className='w-4 h-4' />}
                    onPress={openInvite}
                  >
                    Invite member
                  </Button>
                ) : undefined
              }
            />
          ) : (
            <MembersTable
              items={items}
              canAdmin={canAdmin}
              onChangeRole={openChangeRole}
              onDeactivate={setDeactivateTarget}
            />
          )}
        </div>

        {items.length > 0 && (
          <div className='flex items-center justify-between text-xs text-default-500'>
            <p>
              Showing {items.length} member{items.length === 1 ? '' : 's'}.
            </p>
            {canAdmin && (
              <p className='flex items-center gap-1'>
                <ShieldCheck className='w-3.5 h-3.5' />
                Deactivating is restricted to administrators.
              </p>
            )}
          </div>
        )}

        {membersQuery.isError && (
          <pre className='text-sm text-danger-500 bg-danger-50 dark:bg-danger-500/10 rounded-medium p-3'>
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
