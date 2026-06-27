'use client';

import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseQueryOptions,
} from '@tanstack/react-query';

import {
  archiveTeam,
  createTeam,
  listTeams,
  updateTeam,
  type Paginated,
  type Team,
  type TeamWriteInput,
} from '@/lib/admin-api';
import { adminQueryKeys } from '@/lib/query-keys';

export function useTeams(
  orgId: string | null,
  params?: Parameters<typeof adminQueryKeys.teams>[1],
  options?: Partial<UseQueryOptions<Paginated<Team>>>,
) {
  return useQuery<Paginated<Team>>({
    queryKey: adminQueryKeys.teams(orgId, params),
    queryFn: () => listTeams(params),
    enabled: Boolean(orgId),
    ...options,
  });
}

export function useCreateTeam(orgId: string | null) {
  const queryClient = useQueryClient();

  return useMutation<Team, unknown, TeamWriteInput>({
    mutationFn: (input: TeamWriteInput) => createTeam(input),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: adminQueryKeys.teams(orgId),
      });
    },
  });
}

export function useUpdateTeam(orgId: string | null) {
  const queryClient = useQueryClient();

  return useMutation<Team, unknown, { id: string; input: TeamWriteInput }>({
    mutationFn: ({ id, input }) => updateTeam(id, input),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: adminQueryKeys.teams(orgId),
      });
    },
  });
}

export function useArchiveTeam(orgId: string | null) {
  const queryClient = useQueryClient();

  return useMutation<void, unknown, string>({
    mutationFn: (id: string) => archiveTeam(id),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: adminQueryKeys.teams(orgId),
      });
    },
  });
}
