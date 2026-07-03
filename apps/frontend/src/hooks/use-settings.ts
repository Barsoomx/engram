'use client';

import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseQueryOptions,
} from '@tanstack/react-query';

import { adminQueryKeys } from '@/lib/query-keys';
import {
  getEmbeddingSettings,
  getRetrievalSettings,
  purgeOrganizationMemory,
  updateEmbeddingSettings,
  updateRetrievalSettings,
  type EmbeddingSettings,
  type EmbeddingSettingsInput,
  type PurgeResult,
  type RetrievalSettings,
  type RetrievalSettingsUpdateResponse,
} from '@/lib/settings-api';

export function useRetrievalSettings(
  orgId: string | null,
  options?: Partial<UseQueryOptions<RetrievalSettings>>,
) {
  return useQuery<RetrievalSettings>({
    queryKey: adminQueryKeys.settingsRetrieval(orgId),
    queryFn: () => getRetrievalSettings(),
    enabled: Boolean(orgId),
    ...options,
  });
}

export function useUpdateRetrievalSettings(orgId: string | null) {
  const queryClient = useQueryClient();

  return useMutation<RetrievalSettingsUpdateResponse, unknown, RetrievalSettings>({
    mutationFn: (input: RetrievalSettings) => updateRetrievalSettings(input),
    onSuccess: (data) => {
      queryClient.setQueryData(adminQueryKeys.settingsRetrieval(orgId), data);
    },
  });
}

export function useEmbeddingSettings(
  orgId: string | null,
  options?: Partial<UseQueryOptions<EmbeddingSettings>>,
) {
  return useQuery<EmbeddingSettings>({
    queryKey: adminQueryKeys.settingsEmbedding(orgId),
    queryFn: () => getEmbeddingSettings(),
    enabled: Boolean(orgId),
    ...options,
  });
}

export function useUpdateEmbeddingSettings(orgId: string | null) {
  const queryClient = useQueryClient();

  return useMutation<EmbeddingSettings, unknown, EmbeddingSettingsInput>({
    mutationFn: (input: EmbeddingSettingsInput) =>
      updateEmbeddingSettings(input),
    onSuccess: (data) => {
      queryClient.setQueryData(adminQueryKeys.settingsEmbedding(orgId), data);
    },
  });
}

export function usePurgeOrganizationMemory(orgId: string | null) {
  const queryClient = useQueryClient();

  return useMutation<PurgeResult, unknown, string>({
    mutationFn: (confirmation: string) =>
      purgeOrganizationMemory(confirmation),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: adminQueryKeys.all(orgId) });
    },
  });
}
