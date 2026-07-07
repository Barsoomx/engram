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
  type MetricsScopeParams,
  type MetricsSession,
  type OpsOverview,
} from '@/lib/metrics-api';
import { adminQueryKeys } from '@/lib/query-keys';

const POLL_INTERVAL_MS = 30_000;

export function useMetricsOverview(
  orgId: string | null,
  scope?: MetricsScopeParams,
  options?: Partial<UseQueryOptions<MetricsOverview>>,
) {
  return useQuery<MetricsOverview>({
    queryKey: adminQueryKeys.metricsOverview(orgId, scope),
    queryFn: () => getMetricsOverview(scope),
    enabled: Boolean(orgId),
    refetchInterval: POLL_INTERVAL_MS,
    ...options,
  });
}

export function useMemoryIngest(
  orgId: string | null,
  scope?: MetricsScopeParams,
  options?: Partial<UseQueryOptions<MemoryIngestPoint[]>>,
) {
  return useQuery<MemoryIngestPoint[]>({
    queryKey: adminQueryKeys.memoryIngest(orgId, scope),
    queryFn: () => getMemoryIngest(scope),
    enabled: Boolean(orgId),
    ...options,
  });
}

export function useSessions(
  orgId: string | null,
  scope?: MetricsScopeParams,
  options?: Partial<UseQueryOptions<MetricsSession[]>>,
) {
  return useQuery<MetricsSession[]>({
    queryKey: adminQueryKeys.sessions(orgId, scope),
    queryFn: () => getSessions(scope),
    enabled: Boolean(orgId),
    refetchInterval: POLL_INTERVAL_MS,
    ...options,
  });
}

export function useActivity(
  orgId: string | null,
  scope?: MetricsScopeParams,
  options?: Partial<UseQueryOptions<ActivityEvent[]>>,
) {
  return useQuery<ActivityEvent[]>({
    queryKey: adminQueryKeys.activity(orgId, scope),
    queryFn: () => getActivity(scope),
    enabled: Boolean(orgId),
    refetchInterval: POLL_INTERVAL_MS,
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
    refetchInterval: POLL_INTERVAL_MS,
    ...options,
  });
}
