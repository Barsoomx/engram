'use client';

import { useQuery, type UseQueryOptions } from '@tanstack/react-query';

import {
  getActivity,
  getMemoryIngest,
  getMetricsOverview,
  getOpsOverview,
  getSessions,
  type ActivityEvent,
  type MemoryIngestPoint,
  type MetricsOverview,
  type MetricsSession,
  type OpsOverview,
} from '@/lib/metrics-api';
import { adminQueryKeys } from '@/lib/query-keys';

export function useMetricsOverview(
  orgId: string | null,
  options?: Partial<UseQueryOptions<MetricsOverview>>,
) {
  return useQuery<MetricsOverview>({
    queryKey: adminQueryKeys.metricsOverview(orgId),
    queryFn: () => getMetricsOverview(),
    enabled: Boolean(orgId),
    ...options,
  });
}

export function useMemoryIngest(
  orgId: string | null,
  options?: Partial<UseQueryOptions<MemoryIngestPoint[]>>,
) {
  return useQuery<MemoryIngestPoint[]>({
    queryKey: adminQueryKeys.memoryIngest(orgId),
    queryFn: () => getMemoryIngest(),
    enabled: Boolean(orgId),
    ...options,
  });
}

export function useSessions(
  orgId: string | null,
  options?: Partial<UseQueryOptions<MetricsSession[]>>,
) {
  return useQuery<MetricsSession[]>({
    queryKey: adminQueryKeys.sessions(orgId),
    queryFn: () => getSessions(),
    enabled: Boolean(orgId),
    ...options,
  });
}

export function useActivity(
  orgId: string | null,
  options?: Partial<UseQueryOptions<ActivityEvent[]>>,
) {
  return useQuery<ActivityEvent[]>({
    queryKey: adminQueryKeys.activity(orgId),
    queryFn: () => getActivity(),
    enabled: Boolean(orgId),
    ...options,
  });
}

export function useOpsOverview(
  orgId: string | null,
  options?: Partial<UseQueryOptions<OpsOverview>>,
) {
  return useQuery<OpsOverview>({
    queryKey: adminQueryKeys.opsOverview(orgId),
    queryFn: () => getOpsOverview(),
    enabled: Boolean(orgId),
    ...options,
  });
}
