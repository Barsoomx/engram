'use client';

import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseQueryOptions,
} from '@tanstack/react-query';

import {
  issueApiKey,
  listApiKeys,
  revokeApiKey,
  type ApiKey,
  type ApiKeyIssueInput,
  type ApiKeyIssueResult,
  type Paginated,
} from '@/lib/admin-api';
import { adminQueryKeys } from '@/lib/query-keys';

export function useApiKeys(
  orgId: string | null,
  params?: Parameters<typeof adminQueryKeys.apiKeys>[1],
  options?: Partial<UseQueryOptions<Paginated<ApiKey>>>,
) {
  return useQuery<Paginated<ApiKey>>({
    queryKey: adminQueryKeys.apiKeys(orgId, params),
    queryFn: () => listApiKeys(params),
    enabled: Boolean(orgId),
    ...options,
  });
}

export function useIssueApiKey(orgId: string | null) {
  const queryClient = useQueryClient();

  return useMutation<ApiKeyIssueResult, unknown, ApiKeyIssueInput>({
    mutationFn: (input: ApiKeyIssueInput) => issueApiKey(input),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: adminQueryKeys.apiKeys(orgId),
      });
    },
  });
}

export function useRevokeApiKey(orgId: string | null) {
  const queryClient = useQueryClient();

  return useMutation<void, unknown, string>({
    mutationFn: (id: string) => revokeApiKey(id),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: adminQueryKeys.apiKeys(orgId),
      });
    },
  });
}
