'use client';

import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseQueryOptions,
} from '@tanstack/react-query';

import {
  listWorkflowRuns,
  rerunWorkflowRun,
  workflowRunDetail,
  type Paginated,
  type WorkflowRunDetail,
  type WorkflowRunListItem,
  type WorkflowRunListParams,
  type WorkflowRunRerunResult,
} from '@/lib/admin-api';
import { adminQueryKeys } from '@/lib/query-keys';

export function useWorkflowRuns(
  orgId: string | null,
  params?: WorkflowRunListParams,
  options?: Partial<UseQueryOptions<Paginated<WorkflowRunListItem>>>,
) {
  return useQuery<Paginated<WorkflowRunListItem>>({
    queryKey: adminQueryKeys.workflowRuns(orgId, params),
    queryFn: () => listWorkflowRuns(params),
    enabled: Boolean(orgId),
    ...options,
  });
}

export function useWorkflowRun(
  orgId: string | null,
  id: string | null,
  options?: Partial<UseQueryOptions<WorkflowRunDetail>>,
) {
  return useQuery<WorkflowRunDetail>({
    queryKey: adminQueryKeys.workflowRun(orgId, id),
    queryFn: () => workflowRunDetail(id as string),
    enabled: Boolean(orgId) && Boolean(id),
    ...options,
  });
}

export function useRerunWorkflowRun(orgId: string | null) {
  const queryClient = useQueryClient();

  return useMutation<WorkflowRunRerunResult, unknown, string>({
    mutationFn: (id: string) => rerunWorkflowRun(id),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ['admin', orgId, 'workflow-runs'],
      });
      queryClient.invalidateQueries({
        queryKey: ['admin', orgId, 'workflow-run'],
      });
    },
  });
}
