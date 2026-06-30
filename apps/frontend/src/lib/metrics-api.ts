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
  result: 'success' | 'failure';
  created_at: string;
};

export type OpsOverview = {
  outbox_backlog_count: number;
  outbox_oldest_age_seconds: number | null;
  dead_letter_count: number;
  failed_workflow_runs: number;
  pending_embedding_count: number;
};

export async function getMetricsOverview(): Promise<MetricsOverview> {
  const client = apiClient();
  const response = await client.get<MetricsOverview>(
    '/v1/admin/metrics/overview',
  );

  return response.data;
}

export async function getMemoryIngest(): Promise<MemoryIngestPoint[]> {
  const client = apiClient();
  const response = await client.get<MemoryIngestPoint[]>(
    '/v1/admin/metrics/memory-ingest',
  );

  return response.data;
}

export async function getSessions(): Promise<MetricsSession[]> {
  const client = apiClient();
  const response = await client.get<MetricsSession[]>(
    '/v1/admin/metrics/sessions',
  );

  return response.data;
}

export async function getActivity(): Promise<ActivityEvent[]> {
  const client = apiClient();
  const response = await client.get<ActivityEvent[]>(
    '/v1/admin/metrics/activity',
  );

  return response.data;
}

export async function getOpsOverview(): Promise<OpsOverview> {
  const client = apiClient();
  const response = await client.get<OpsOverview>('/v1/admin/ops/overview');

  return response.data;
}
