'use client';

import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseQueryOptions,
} from '@tanstack/react-query';

import {
  deactivateMember,
  inviteMember,
  listMembers,
  listRoles,
  updateMemberRole,
  type Member,
  type MemberInviteInput,
  type MemberRoleInput,
  type Paginated,
  type Role,
} from '@/lib/admin-api';
import { adminQueryKeys } from '@/lib/query-keys';

export function useMembers(
  orgId: string | null,
  params?: Parameters<typeof adminQueryKeys.members>[1],
  options?: Partial<UseQueryOptions<Paginated<Member>>>,
) {
  return useQuery<Paginated<Member>>({
    queryKey: adminQueryKeys.members(orgId, params),
    queryFn: () => listMembers(params),
    enabled: Boolean(orgId),
    ...options,
  });
}

export function useRoles(
  orgId: string | null,
  params?: Parameters<typeof adminQueryKeys.roles>[1],
  options?: Partial<UseQueryOptions<Paginated<Role>>>,
) {
  return useQuery<Paginated<Role>>({
    queryKey: adminQueryKeys.roles(orgId, params),
    queryFn: () => listRoles(params),
    enabled: Boolean(orgId),
    ...options,
  });
}

export function useInviteMember(orgId: string | null) {
  const queryClient = useQueryClient();

  return useMutation<Member, unknown, MemberInviteInput>({
    mutationFn: (input: MemberInviteInput) => inviteMember(input),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: adminQueryKeys.members(orgId),
      });
    },
  });
}

export function useUpdateMemberRole(orgId: string | null) {
  const queryClient = useQueryClient();

  return useMutation<Member, unknown, { id: string; input: MemberRoleInput }>({
    mutationFn: ({ id, input }) => updateMemberRole(id, input),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: adminQueryKeys.members(orgId),
      });
    },
  });
}

export function useDeactivateMember(orgId: string | null) {
  const queryClient = useQueryClient();

  return useMutation<void, unknown, string>({
    mutationFn: (id: string) => deactivateMember(id),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: adminQueryKeys.members(orgId),
      });
    },
  });
}
