'use client';

import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseQueryOptions,
} from '@tanstack/react-query';

import {
  getMemoryConflict,
  listMemoryReview,
  resolveMemoryConflict,
  type ConflictResolvePayload,
  type ConflictResolveResult,
  type MemoryConflictDetail,
  type MemoryReviewItem,
  type MemoryReviewListParams,
  type Paginated,
} from '@/lib/admin-api';
import { adminQueryKeys } from '@/lib/query-keys';

export function useMemoryReview(
  orgId: string | null,
  params?: MemoryReviewListParams,
  options?: Partial<UseQueryOptions<Paginated<MemoryReviewItem>>>,
) {
  return useQuery<Paginated<MemoryReviewItem>>({
    queryKey: adminQueryKeys.memoryReview(orgId, params),
    queryFn: () => listMemoryReview(params),
    enabled: Boolean(orgId),
    ...options,
  });
}

export function useMemoryConflict(
  orgId: string | null,
  candidateId: string | null,
  options?: Partial<UseQueryOptions<MemoryConflictDetail>>,
) {
  return useQuery<MemoryConflictDetail>({
    queryKey: adminQueryKeys.memoryConflict(orgId, candidateId),
    queryFn: () => {
      if (!candidateId) {
        throw new Error('conflict detail requires a candidate id');
      }

      return getMemoryConflict(candidateId);
    },
    enabled: Boolean(orgId) && Boolean(candidateId),
    ...options,
  });
}

export function useResolveMemoryConflict(orgId: string | null) {
  const queryClient = useQueryClient();

  return useMutation<
    ConflictResolveResult,
    unknown,
    { id: string; payload: ConflictResolvePayload; ifMatch: string }
  >({
    mutationFn: ({ id, payload, ifMatch }) =>
      resolveMemoryConflict(id, payload, ifMatch),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: adminQueryKeys.memoryReview(orgId),
      });
    },
  });
}
