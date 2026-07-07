'use client';

import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseQueryOptions,
} from '@tanstack/react-query';

import {
  archiveProject,
  createProject,
  listProjects,
  updateProject,
  type Paginated,
  type Project,
  type ProjectListParams,
  type ProjectWriteInput,
} from '@/lib/admin-api';
import { adminQueryKeys } from '@/lib/query-keys';

export function useProjects(
  orgId: string | null,
  params?: Parameters<typeof adminQueryKeys.projects>[1],
  options?: Partial<UseQueryOptions<Paginated<Project>>>,
) {
  return useQuery<Paginated<Project>>({
    queryKey: adminQueryKeys.projects(orgId, params),
    queryFn: () => listProjects(params as ProjectListParams | undefined),
    enabled: Boolean(orgId),
    ...options,
  });
}

export function useCreateProject(orgId: string | null) {
  const queryClient = useQueryClient();

  return useMutation<Project, unknown, ProjectWriteInput>({
    mutationFn: (input: ProjectWriteInput) => createProject(input),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: adminQueryKeys.projects(orgId),
      });
    },
  });
}

export function useUpdateProject(orgId: string | null) {
  const queryClient = useQueryClient();

  return useMutation<Project, unknown, { id: string; input: ProjectWriteInput }>({
    mutationFn: ({ id, input }) => updateProject(id, input),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: adminQueryKeys.projects(orgId),
      });
    },
  });
}

export function useArchiveProject(orgId: string | null) {
  const queryClient = useQueryClient();

  return useMutation<void, unknown, string>({
    mutationFn: (id: string) => archiveProject(id),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: adminQueryKeys.projects(orgId),
      });
    },
  });
}
