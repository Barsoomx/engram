'use client';

import { useQuery, type UseQueryOptions } from '@tanstack/react-query';

import { listOrganizations, type Paginated, type Organization } from '@/lib/admin-api';
import { adminQueryKeys } from '@/lib/query-keys';

export function useOrganizations(
  orgId: string | null,
  options?: Partial<UseQueryOptions<Paginated<Organization>>>,
) {
  return useQuery<Paginated<Organization>>({
    queryKey: adminQueryKeys.organizations(orgId),
    queryFn: () => listOrganizations(),
    ...options,
  });
}
