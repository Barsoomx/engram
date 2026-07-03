import { apiClient } from '@/lib/auth';

export type RetrievalSettings = {
  hybrid_retrieval_enabled: boolean;
  require_provenance: boolean;
  distillation_auto_approve_threshold: number | null;
  near_dup_threshold: number;
};

export type RetrievalSettingsUpdateResponse = RetrievalSettings & {
  advisory?: string;
};

export type EmbeddingSettings = {
  provider: string | null;
  model: string | null;
};

export type EmbeddingSettingsInput = {
  provider: string;
  model: string;
  secret_id: string;
};

export type PurgeResult = {
  deleted: {
    memories: number;
    memory_candidates: number;
    retrieval_documents: number;
  };
};

export async function getRetrievalSettings(): Promise<RetrievalSettings> {
  const client = apiClient();
  const response = await client.get<RetrievalSettings>(
    '/v1/admin/settings/retrieval',
  );

  return response.data;
}

export async function updateRetrievalSettings(
  input: RetrievalSettings,
): Promise<RetrievalSettingsUpdateResponse> {
  const client = apiClient();
  const response = await client.put<RetrievalSettingsUpdateResponse>(
    '/v1/admin/settings/retrieval',
    input,
  );

  return response.data;
}

export async function getEmbeddingSettings(): Promise<EmbeddingSettings> {
  const client = apiClient();
  const response = await client.get<EmbeddingSettings>(
    '/v1/admin/settings/embedding',
  );

  return response.data;
}

export async function updateEmbeddingSettings(
  input: EmbeddingSettingsInput,
): Promise<EmbeddingSettings> {
  const client = apiClient();
  const response = await client.put<EmbeddingSettings>(
    '/v1/admin/settings/embedding',
    input,
  );

  return response.data;
}

export async function purgeOrganizationMemory(
  confirmation: string,
): Promise<PurgeResult> {
  const client = apiClient();
  const response = await client.post<PurgeResult>(
    '/v1/admin/settings/purge',
    { confirmation },
  );

  return response.data;
}
