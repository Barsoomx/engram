import { apiClient } from '@/lib/auth';

export type MetricsOverview = {
  memories_indexed: number;
  memories_indexed_delta: number;
  context_bundles_7d: number;
  context_bundles_7d_delta: number;
  connected_agents: number;
  avg_retrieval_latency_ms: number | null;
  avg_retrieval_latency_measured: boolean;
};

export type MemoryIngestPoint = {
  date: string;
  count: number;
};

export type MetricsSession = {
  session_id: string;
  agent_name: string;
  model_id: string;
  status: 'active' | 'idle';
  last_seen: string;
};

export type ActivityEvent = {
  event_type: string;
  actor_type: string;
  actor_id: string;
  target_type: string;
  target_id: string;
  result: 'allowed' | 'denied' | 'recorded';
  created_at: string;
};

export type OpsOverview = {
  outbox_backlog_count: number;
  outbox_oldest_age_seconds: number | null;
  dead_letter_count: number;
  failed_workflow_runs: number;
  pending_embedding_count: number;
};

export type MetricsScopeParams = {
  project_id?: string;
  team_id?: string;
};

function metricsParams(
  params?: MetricsScopeParams,
): Record<string, string> | undefined {
  if (!params) {
    return undefined;
  }

  const query: Record<string, string> = {};

  if (params.project_id) {
    query.project_id = params.project_id;
  }

  if (params.team_id) {
    query.team_id = params.team_id;
  }

  return Object.keys(query).length > 0 ? query : undefined;
}

export async function getMetricsOverview(
  params?: MetricsScopeParams,
): Promise<MetricsOverview> {
  const client = apiClient();
  const response = await client.get<MetricsOverview>(
    '/v1/admin/metrics/overview',
    { params: metricsParams(params) },
  );

  return response.data;
}

export async function getMemoryIngest(
  params?: MetricsScopeParams,
): Promise<MemoryIngestPoint[]> {
  const client = apiClient();
  const response = await client.get<MemoryIngestPoint[]>(
    '/v1/admin/metrics/memory-ingest',
    { params: metricsParams(params) },
  );

  return response.data;
}

export async function getSessions(
  params?: MetricsScopeParams,
): Promise<MetricsSession[]> {
  const client = apiClient();
  const response = await client.get<MetricsSession[]>(
    '/v1/admin/metrics/sessions',
    { params: metricsParams(params) },
  );

  return response.data;
}

export async function getActivity(
  params?: MetricsScopeParams,
): Promise<ActivityEvent[]> {
  const client = apiClient();
  const response = await client.get<ActivityEvent[]>(
    '/v1/admin/metrics/activity',
    { params: metricsParams(params) },
  );

  return response.data;
}

export async function getOpsOverview(): Promise<OpsOverview> {
  const client = apiClient();
  const response = await client.get<OpsOverview>('/v1/admin/ops/overview');

  return response.data;
}
