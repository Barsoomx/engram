'use client';

import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseQueryOptions,
} from '@tanstack/react-query';

import {
  bulkArchiveMemoryReview,
  listMemoryReview,
  memoryReviewAction,
  memoryReviewDiff,
  type BulkArchiveMemoryReviewPayload,
  type BulkArchiveMemoryReviewResult,
  type MemoryReviewActionPayload,
  type MemoryReviewActionResult,
  type MemoryReviewDiff,
  type MemoryReviewItem,
  type MemoryReviewListParams,
  type Paginated,
} from '@/lib/admin-api';
import { adminQueryKeys } from '@/lib/query-keys';

type ReviewParams = Parameters<typeof adminQueryKeys.memoryReview>[1];

export function useMemoryReview(
  orgId: string | null,
  params?: ReviewParams,
  options?: Partial<UseQueryOptions<Paginated<MemoryReviewItem>>>,
) {
  return useQuery<Paginated<MemoryReviewItem>>({
    queryKey: adminQueryKeys.memoryReview(orgId, params),
    queryFn: () =>
      listMemoryReview(params as MemoryReviewListParams | undefined),
    enabled: Boolean(orgId),
    ...options,
  });
}

export function useMemoryReviewDiff(
  orgId: string | null,
  memoryId: string | null,
  fromVersion: number | null,
  toVersion: number | null,
  options?: Partial<UseQueryOptions<MemoryReviewDiff>>,
) {
  return useQuery<MemoryReviewDiff>({
    queryKey: adminQueryKeys.memoryReviewDiff(
      orgId,
      memoryId,
      fromVersion,
      toVersion,
    ),
    queryFn: () => {
      if (!memoryId || fromVersion === null || toVersion === null) {
        throw new Error('memory diff requires id, from and to versions');
      }

      return memoryReviewDiff(memoryId, fromVersion, toVersion);
    },
    enabled:
      Boolean(orgId) &&
      Boolean(memoryId) &&
      fromVersion !== null &&
      toVersion !== null,
    ...options,
  });
}

export function useMemoryReviewAction(orgId: string | null) {
  const queryClient = useQueryClient();

  return useMutation<
    MemoryReviewActionResult,
    unknown,
    { id: string; payload: MemoryReviewActionPayload }
  >({
    mutationFn: ({ id, payload }) => memoryReviewAction(id, payload),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: adminQueryKeys.memoryReview(orgId),
      });
    },
  });
}

export function useBulkArchiveMemoryReview(orgId: string | null) {
  const queryClient = useQueryClient();

  return useMutation<
    BulkArchiveMemoryReviewResult,
    unknown,
    BulkArchiveMemoryReviewPayload
  >({
    mutationFn: (payload) => bulkArchiveMemoryReview(payload),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: adminQueryKeys.memoryReview(orgId),
      });
    },
  });
}
