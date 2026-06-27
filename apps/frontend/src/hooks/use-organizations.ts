'use client';

import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseQueryOptions,
} from '@tanstack/react-query';

import {
  listOrganizations,
  updateOrganization,
  type Organization,
  type OrganizationWriteInput,
  type Paginated,
} from '@/lib/admin-api';
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

export function useUpdateOrganization(orgId: string | null) {
  const queryClient = useQueryClient();

  return useMutation<Organization, Error, { id: string; input: OrganizationWriteInput }>({
    mutationFn: ({ id, input }) => updateOrganization(id, input),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: adminQueryKeys.organizations(orgId),
      });
    },
  });
}
