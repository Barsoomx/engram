import { apiClient } from '@/lib/auth';
import type { ListParams } from '@/lib/query-keys';

export type Organization = {
  id: string;
  name: string;
  slug: string;
  created_at: string;
  updated_at: string;
};

export type Paginated<T> = {
  count: number;
  next: string | null;
  previous: string | null;
  results: T[];
};

export async function listOrganizations(
  params?: ListParams,
): Promise<Paginated<Organization>> {
  const client = apiClient();
  const response = await client.get<Paginated<Organization>>(
    '/v1/admin/organizations/',
    { params },
  );

  return response.data;
}

export type ApiKeyOwner = {
  id: string;
  display_name: string;
};

export type ApiKey = {
  id: string;
  name: string;
  key_prefix: string;
  key_fingerprint: string;
  owner_identity: ApiKeyOwner;
  capabilities: string[];
  created_at: string;
  expires_at: string | null;
  last_used_at: string | null;
  active: boolean;
  revoked_at: string | null;
};

export type ApiKeyIssueInput = {
  name: string;
  capabilities: string[];
  expires_at?: string | null;
};

export type ApiKeyIssueResult = {
  id: string;
  name: string;
  key_prefix: string;
  key_fingerprint: string;
  plaintext: string;
  capabilities: string[];
  created_at: string;
};

export type Team = {
  id: string;
  name: string;
  slug: string;
  organization: string;
  created_at: string;
  updated_at: string;
  archived_at: string | null;
};

export type TeamWriteInput = {
  name: string;
  slug: string;
};

export async function listTeams(
  params?: ListParams,
): Promise<Paginated<Team>> {
  const client = apiClient();
  const response = await client.get<Paginated<Team>>(
    '/v1/admin/teams/',
    { params },
  );

  return response.data;
}

export async function createTeam(input: TeamWriteInput): Promise<Team> {
  const client = apiClient();
  const response = await client.post<Team>('/v1/admin/teams/', input);

  return response.data;
}

export async function updateTeam(
  id: string,
  input: TeamWriteInput,
): Promise<Team> {
  const client = apiClient();
  const response = await client.patch<Team>(
    `/v1/admin/teams/${id}/`,
    input,
  );

  return response.data;
}

export async function archiveTeam(id: string): Promise<void> {
  const client = apiClient();

  await client.delete(`/v1/admin/teams/${id}/`);
}

export async function listApiKeys(
  params?: ListParams,
): Promise<Paginated<ApiKey>> {
  const client = apiClient();
  const response = await client.get<Paginated<ApiKey>>(
    '/v1/admin/api-keys/',
    { params },
  );

  return response.data;
}

export async function issueApiKey(
  input: ApiKeyIssueInput,
): Promise<ApiKeyIssueResult> {
  const client = apiClient();
  const response = await client.post<ApiKeyIssueResult>(
    '/v1/admin/api-keys/',
    input,
  );

  return response.data;
}

export async function revokeApiKey(id: string): Promise<void> {
  const client = apiClient();

  await client.post(`/v1/admin/api-keys/${id}/revoke/`);
}
