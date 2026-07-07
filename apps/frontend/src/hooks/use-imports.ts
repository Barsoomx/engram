'use client';

import { useQuery, type UseQueryOptions } from '@tanstack/react-query';

import {
  importDetail,
  isTerminalImportStatus,
  listImports,
  type ImportJob,
  type Paginated,
} from '@/lib/admin-api';
import { adminQueryKeys, type ListParams } from '@/lib/query-keys';

const DETAIL_POLL_INTERVAL_MS = 4000;

export function useImports(
  orgId: string | null,
  params?: ListParams,
  options?: Partial<UseQueryOptions<Paginated<ImportJob>>>,
) {
  return useQuery<Paginated<ImportJob>>({
    queryKey: adminQueryKeys.imports(orgId, params),
    queryFn: () => listImports(params),
    enabled: Boolean(orgId),
    ...options,
  });
}

export function useImport(
  orgId: string | null,
  id: string | null,
  options?: Partial<UseQueryOptions<ImportJob>>,
) {
  return useQuery<ImportJob>({
    queryKey: adminQueryKeys.importJob(orgId, id),
    queryFn: () => importDetail(id as string),
    enabled: Boolean(orgId) && Boolean(id),
    refetchInterval: (query) => {
      const job = query.state.data;

      if (job && isTerminalImportStatus(job.status)) {
        return false;
      }

      return DETAIL_POLL_INTERVAL_MS;
    },
    ...options,
  });
}
