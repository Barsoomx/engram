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
