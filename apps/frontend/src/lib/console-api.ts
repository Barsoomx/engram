import axios from 'axios';

import { apiClient } from '@/lib/auth';

function isMissingListEndpoint(error: unknown): boolean {
  return (
    axios.isAxiosError(error) &&
    (error.response?.status === 404 || error.response?.status === 405)
  );
}

export function genRequestId(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) {
    return crypto.randomUUID();
  }

  return `req-${Date.now()}-${Math.floor(Math.random() * 1e9)}`;
}

export interface ScopeParams {
  projectId: string;
  teamId?: string | null;
  limit?: number;
  offset?: number;
}

function scopeQuery({
  projectId,
  teamId,
  limit,
  offset,
}: ScopeParams): Record<string, string> {
  const params: Record<string, string> = { project_id: projectId };

  if (teamId) {
    params.team_id = teamId;
  }

  if (limit !== undefined) {
    params.limit = String(limit);
  }

  if (offset !== undefined) {
    params.offset = String(offset);
  }

  return params;
}

function listEnvelope<T>(data: unknown): { count: number; items: T[] } {
  if (data && typeof data === 'object') {
    const record = data as Record<string, unknown>;
    const items = (record.items ?? record.results ?? []) as T[];
    const count =
      typeof record.count === 'number' ? record.count : items.length;

    return { count, items };
  }

  return { count: 0, items: [] };
}

/* ---------------------------- Inspection memories ------------------------- */

export type InspectionMemoryOrdering = 'created_at' | '-created_at';

export interface InspectionMemoryListParams extends ScopeParams {
  search?: string;
  status?: string;
  kind?: string;
  ordering?: InspectionMemoryOrdering;
}

export interface InspectionMemory {
  id: string;
  project_id: string;
  team_id: string | null;
  title: string;
  body: string;
  status: string;
  visibility_scope: string;
  current_version: number;
  confidence: string | null;
  confidence_percent?: number | null;
  stale: boolean;
  refuted: boolean;
  created_at: string | null;
  updated_at: string | null;
  kind?: string | null;
  metadata?: Record<string, unknown> | null;
  tags?: string[];
  file_paths?: string[];
  project_name?: string;
  project_slug?: string;
  authorized_for_injection?: boolean;
}

export async function listInspectionMemories(
  params: InspectionMemoryListParams,
): Promise<{ count: number; items: InspectionMemory[] }> {
  const query: Record<string, string> = scopeQuery(params);

  if (params.search) {
    query.search = params.search;
  }

  if (params.status) {
    query.status = params.status;
  }

  if (params.kind) {
    query.kind = params.kind;
  }

  if (params.ordering) {
    query.ordering = params.ordering;
  }

  const response = await apiClient().get('/v1/inspection/memories', {
    params: query,
  });

  return listEnvelope<InspectionMemory>(response.data);
}

/* ----------------------------- Search debugger ---------------------------- */

export interface SearchDebugRequest {
  project_id: string;
  query: string;
  team_id?: string | null;
  file_paths?: string[];
  symbols?: string[];
}

export interface SearchDebugExactMatch {
  memory_id: string;
  title: string;
  score: number;
  matched_on: string;
  kind: string;
  confidence: string | null;
}

export interface SearchDebugSemanticCandidate {
  memory_id: string;
  title: string;
  score: number;
  kind: string;
  confidence: string | null;
}

export interface SearchDebugPackedItem {
  memory_id: string;
  title: string;
  kind: string;
  confidence: string | null;
}

export interface SearchDebugExcludedItem {
  memory_id: string;
  title: string;
  reason: string;
}

export interface SearchDebugResult {
  scope_filters: Record<string, unknown>;
  candidate_universe_count: number;
  exact_matches: SearchDebugExactMatch[];
  semantic_enabled: boolean;
  semantic_candidates: SearchDebugSemanticCandidate[];
  lexical_enabled: boolean;
  lexical_candidates: SearchDebugExactMatch[];
  packed_context: SearchDebugPackedItem[];
  excluded: SearchDebugExcludedItem[];
}

export async function replaySearchDebug(
  body: SearchDebugRequest,
): Promise<SearchDebugResult> {
  const response = await apiClient().post<SearchDebugResult>(
    '/v1/admin/search-debug/',
    body,
  );

  return response.data;
}

/* ----------------------------- Context bundles ---------------------------- */

export interface ContextBundleListItem {
  id: string;
  project_id: string;
  team_id: string | null;
  agent_id: string;
  session_id: string;
  request_id: string;
  purpose: string;
  query_text: string;
  token_budget: number | null;
  selected_count: number;
  status: string;
  created_at: string | null;
  updated_at: string | null;
}

export interface ContextBundleEntry {
  id: string;
  bundle_id: string;
  memory_id: string;
  retrieval_document_id: string;
  kind: string;
  confidence: string | null;
  rank: number;
  citation: string;
  inclusion_reason: string;
  scope_evidence: Record<string, unknown> | null;
  metadata: Record<string, unknown> | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface ContextBundleWarning {
  code: string;
  message: string;
  memory_id: string | null;
}

export interface ContextBundleDetail extends ContextBundleListItem {
  rendered_text: string;
  authorization_scope: Record<string, unknown> | null;
  retrieval_latency_ms?: number | null;
  warnings: ContextBundleWarning[];
  metadata: Record<string, unknown> | null;
  items: ContextBundleEntry[];
}

export interface ContextBundleListParams extends ScopeParams {
  session_id?: string;
  status?: string;
  since?: string;
  until?: string;
}

export async function listContextBundles(
  scope: ContextBundleListParams,
): Promise<{ count: number; items: ContextBundleListItem[] }> {
  const params: Record<string, string> = scopeQuery(scope);

  if (scope.session_id) {
    params.session_id = scope.session_id;
  }

  if (scope.status) {
    params.status = scope.status;
  }

  if (scope.since) {
    params.since = scope.since;
  }

  if (scope.until) {
    params.until = scope.until;
  }

  const response = await apiClient().get('/v1/inspection/context-bundles', {
    params,
  });

  return listEnvelope<ContextBundleListItem>(response.data);
}

export async function getContextBundle(
  bundleId: string,
  scope: ScopeParams,
): Promise<ContextBundleDetail> {
  const response = await apiClient().get<ContextBundleDetail>(
    `/v1/inspection/context-bundles/${bundleId}`,
    { params: scopeQuery(scope) },
  );

  return response.data;
}

/* ------------------------------- Memory links ----------------------------- */

export type MemoryLinkType = 'file' | 'symbol' | 'commit' | 'issue';

export interface MemoryLink {
  link_id: string;
  link_type: MemoryLinkType;
  target: string;
  label: string;
  created_at: string | null;
}

export interface MemoryLinkInput {
  project_id: string;
  team_id?: string | null;
  link_type: MemoryLinkType;
  target: string;
  label?: string;
  request_id: string;
  correlation_id?: string;
}

export async function listMemoryLinks(
  memoryId: string,
  scope: ScopeParams,
): Promise<MemoryLink[]> {
  const response = await apiClient().get(`/v1/memories/${memoryId}/links`, {
    params: scopeQuery(scope),
  });

  return listEnvelope<MemoryLink>(response.data).items;
}

export async function addMemoryLink(
  memoryId: string,
  body: MemoryLinkInput,
): Promise<MemoryLink> {
  const response = await apiClient().post<MemoryLink>(
    `/v1/memories/${memoryId}/links`,
    body,
  );

  return response.data;
}

/* ------------------------------ Memory feedback --------------------------- */

export type MemoryFeedbackAction = 'stale' | 'refuted';

export interface MemoryFeedbackInput {
  project_id: string;
  team_id?: string | null;
  action: MemoryFeedbackAction;
  reason: string;
  request_id: string;
  correlation_id?: string;
}

export interface MemoryFeedbackResult {
  memory_id: string;
  project_id: string;
  team_id: string | null;
  action: MemoryFeedbackAction;
  stale: boolean;
  refuted: boolean;
  retrieval_documents_updated: number;
  already_applied: boolean;
}

export async function recordMemoryFeedback(
  memoryId: string,
  body: MemoryFeedbackInput,
): Promise<MemoryFeedbackResult> {
  const response = await apiClient().post<MemoryFeedbackResult>(
    `/v1/memories/${memoryId}/feedback`,
    body,
  );

  return response.data;
}

/* ------------------------------ Provider secrets -------------------------- */

export type SecretProvider = 'anthropic' | 'openai' | 'deepseek';
export type SecretScope = 'organization' | 'team';

export interface ProviderSecret {
  id: string;
  organization_id: string;
  team_id: string | null;
  name: string;
  provider: SecretProvider;
  scope: SecretScope;
  storage_mode: string;
  current_version: number;
  active: boolean;
  rotation_state: string;
  secret_fingerprint: string;
  created_at: string | null;
  updated_at: string | null;
}

export interface ProviderSecretCreateInput {
  project_id: string;
  team_id?: string | null;
  name: string;
  provider: SecretProvider;
  scope: SecretScope;
  raw_secret: string;
  request_id: string;
}

export interface ProviderSecretRotateInput {
  project_id: string;
  team_id?: string | null;
  raw_secret: string;
  request_id: string;
}

export interface ProviderSecretDisableInput {
  project_id: string;
  team_id?: string | null;
  request_id: string;
}

export interface ProviderSecretListFilters {
  provider?: SecretProvider;
  scope?: SecretScope;
  active?: boolean;
}

export async function listProviderSecrets(
  scope: ScopeParams,
  filters?: ProviderSecretListFilters,
): Promise<ProviderSecret[]> {
  try {
    const params = scopeQuery(scope);

    if (filters?.provider) {
      params.provider = filters.provider;
    }

    if (filters?.scope) {
      params.scope = filters.scope;
    }

    if (filters?.active !== undefined) {
      params.active = String(filters.active);
    }

    const response = await apiClient().get('/v1/model-policy/secrets', {
      params,
    });

    return listEnvelope<ProviderSecret>(response.data).items;
  } catch (error) {
    if (isMissingListEndpoint(error)) {
      return [];
    }

    throw error;
  }
}

export async function createProviderSecret(
  body: ProviderSecretCreateInput,
): Promise<ProviderSecret> {
  const response = await apiClient().post<ProviderSecret>(
    '/v1/model-policy/secrets',
    body,
  );

  return response.data;
}

export async function rotateProviderSecret(
  secretId: string,
  body: ProviderSecretRotateInput,
): Promise<ProviderSecret> {
  const response = await apiClient().post<ProviderSecret>(
    `/v1/model-policy/secrets/${secretId}/rotate`,
    body,
  );

  return response.data;
}

export async function disableProviderSecret(
  secretId: string,
  body: ProviderSecretDisableInput,
): Promise<ProviderSecret> {
  const response = await apiClient().post<ProviderSecret>(
    `/v1/model-policy/secrets/${secretId}/disable`,
    body,
  );

  return response.data;
}

/* ------------------------------- Model policies --------------------------- */

export type PolicyScope = 'organization' | 'team' | 'project';
export type PolicyTaskType = 'generation' | 'embedding' | 'curation' | 'digest';

export interface ModelPolicy {
  id: string;
  policy_id: string;
  organization_id: string;
  team_id: string | null;
  project_id: string | null;
  secret_id: string;
  name: string;
  scope: PolicyScope;
  task_type: PolicyTaskType;
  provider: SecretProvider;
  model: string;
  version: number;
  active: boolean;
  fallback_enabled: boolean;
  base_url?: string | null;
  context_window_tokens?: number | null;
  json_mode?: boolean | null;
  last_success_at?: string | null;
  recent_error_count?: number;
}

export interface ModelPolicyCreateInput {
  project_id: string;
  team_id?: string | null;
  scope_team_id?: string | null;
  name: string;
  scope: PolicyScope;
  task_type: PolicyTaskType;
  provider: SecretProvider;
  model: string;
  secret_id: string;
  request_id: string;
  base_url?: string;
  context_window_tokens?: number;
  fallback_enabled?: boolean;
  json_mode?: boolean;
}

export interface ModelPolicyResolveParams {
  project_id: string;
  team_id?: string | null;
  task_type: PolicyTaskType;
}

export interface ModelPolicyListParams extends ScopeParams {
  task_type?: PolicyTaskType;
  provider?: SecretProvider;
  scope?: PolicyScope;
  active?: boolean;
}

export async function listModelPolicies(
  params: ModelPolicyListParams,
): Promise<{ count: number; items: ModelPolicy[] }> {
  const query: Record<string, string> = scopeQuery(params);

  if (params.task_type) {
    query.task_type = params.task_type;
  }

  if (params.provider) {
    query.provider = params.provider;
  }

  if (params.scope) {
    query.scope = params.scope;
  }

  if (params.active !== undefined) {
    query.active = String(params.active);
  }

  try {
    const response = await apiClient().get('/v1/model-policy/policies', {
      params: query,
    });

    return listEnvelope<ModelPolicy>(response.data);
  } catch (error) {
    if (isMissingListEndpoint(error)) {
      return { count: 0, items: [] };
    }

    throw error;
  }
}

export async function createModelPolicy(
  body: ModelPolicyCreateInput,
): Promise<ModelPolicy> {
  const response = await apiClient().post<ModelPolicy>(
    '/v1/model-policy/policies',
    body,
  );

  return response.data;
}

export async function resolveModelPolicy(
  params: ModelPolicyResolveParams,
): Promise<ModelPolicy> {
  const query: Record<string, string> = {
    project_id: params.project_id,
    task_type: params.task_type,
  };

  if (params.team_id) {
    query.team_id = params.team_id;
  }

  const response = await apiClient().get<ModelPolicy>(
    '/v1/model-policy/resolve',
    { params: query },
  );

  return response.data;
}

export const POLICY_TASK_TYPES: PolicyTaskType[] = [
  'generation',
  'embedding',
  'curation',
  'digest',
];

export const SECRET_PROVIDERS: SecretProvider[] = ['anthropic', 'openai', 'deepseek'];

export interface ModelPolicyActionInput {
  project_id: string;
  team_id?: string | null;
  request_id: string;
}

export async function getModelPolicy(
  policyId: string,
  scope: ScopeParams,
): Promise<ModelPolicy> {
  const response = await apiClient().get<ModelPolicy>(
    `/v1/model-policy/policies/${policyId}`,
    { params: scopeQuery(scope) },
  );

  return response.data;
}

export async function disableModelPolicy(
  policyId: string,
  body: ModelPolicyActionInput,
): Promise<ModelPolicy> {
  const response = await apiClient().post<ModelPolicy>(
    `/v1/model-policy/policies/${policyId}/disable`,
    body,
  );

  return response.data;
}

export async function enableProviderSecret(
  secretId: string,
  body: ProviderSecretDisableInput,
): Promise<ProviderSecret> {
  const response = await apiClient().post<ProviderSecret>(
    `/v1/model-policy/secrets/${secretId}/enable`,
    body,
  );

  return response.data;
}

/* ------------------------------- Weekly digest ---------------------------- */

export interface DigestBucketItem {
  id: string;
  title: string;
  at: string;
}

export interface DigestChangelogItem {
  id: string;
  title: string;
  bucket: string;
  at: string;
}

export interface DigestCounts {
  refuted: number;
  retired: number;
  superseded: number;
  merged: number;
  added: number;
}

export interface WeeklyDigest {
  digest_memory_id: string;
  window_start: string | null;
  window_end: string | null;
  window_days: number;
  counts: DigestCounts;
  memory_changes: Record<string, DigestBucketItem[]>;
  changelog: DigestChangelogItem[];
  ready: boolean;
}

export async function getWeeklyDigest(
  scope: ScopeParams,
  windowDays?: number,
  weeksBack?: number,
): Promise<WeeklyDigest> {
  const params: Record<string, string> = scopeQuery(scope);

  if (windowDays && windowDays > 0) {
    params.window_days = String(windowDays);
  }

  if (weeksBack && weeksBack > 0) {
    params.weeks_back = String(weeksBack);
  }

  const response = await apiClient().get<WeeklyDigest>(
    '/v1/admin/digests/weekly',
    { params },
  );

  return response.data;
}

export interface DigestReviewResult {
  memory_id: string;
  reviewed: boolean;
  ready: boolean;
}

export async function reviewDigest(
  memoryId: string,
): Promise<DigestReviewResult> {
  const response = await apiClient().post<DigestReviewResult>(
    `/v1/admin/digests/${memoryId}/review`,
    {},
  );

  return response.data;
}

export interface DigestRunWorkflow {
  run_type: string;
  project_id: string;
  request_id: string;
}

export interface DigestRunResult {
  enqueued: boolean;
  reason?: string;
  workflow?: DigestRunWorkflow;
}

export async function runProjectDigest(
  projectId: string,
): Promise<DigestRunResult> {
  const response = await apiClient().post<DigestRunResult>(
    `/v1/admin/projects/${projectId}/digest/run`,
    {},
  );

  return response.data;
}

/* -------------------------------- Hook dry-run ---------------------------- */

export interface HookDryRunInput {
  project_id: string | null;
  team_id?: string | null;
  request_id?: string;
}

export interface HookDryRunResult {
  status: string;
  request_id: string;
  resolved_actor: {
    type: string;
    id: string;
  };
  scope: {
    organization_id: string;
    project_ids: string[];
    team_ids: string[];
    capabilities: string[];
  };
  server: {
    health: string;
  };
}

export async function dryRunHook(
  body: HookDryRunInput,
  apiKey?: string,
): Promise<HookDryRunResult> {
  const config = apiKey
    ? { headers: { Authorization: `Bearer ${apiKey}` } }
    : undefined;

  const response = await apiClient().post<HookDryRunResult>(
    '/v1/hooks/dry-run',
    body,
    config,
  );

  return response.data;
}

/* ------------------------------- Model setup ------------------------------ */

export type TaskTypeStatus = {
  task_type: string;
  configured: boolean;
  policy_id: string | null;
  provider: string | null;
  model: string | null;
  secret_active: boolean;
};

export type ModelSetupStatus = {
  task_types: TaskTypeStatus[];
  ready: boolean;
  secrets: { id: string; name: string; provider: string; active: boolean }[];
};

export type PresetTaskModel = {
  task_type: string;
  provider: string;
  model: string;
  base_url: string;
  key_slot: string;
};

export type ModelPreset = {
  key: string;
  name: string;
  description: string;
  providers_needed: string[];
  task_models: PresetTaskModel[];
};

export type ApplyPresetRequest = {
  project_id: string;
  team_id?: string | null;
  scope: 'organization' | 'project' | 'team';
  preset_key: string;
  provider_keys: Record<string, string>;
  request_id: string;
  replace_existing?: boolean;
};

export type ApplyPresetResponse = {
  created_secret_ids: string[];
  created_policy_ids: string[];
  disabled_policy_ids: string[];
  status: ModelSetupStatus;
};

export type ExistingPoliciesConflict = {
  code: 'existing_policies';
  policies_to_replace: string[];
};

export function existingPoliciesConflict(
  error: unknown,
): ExistingPoliciesConflict | null {
  if (!axios.isAxiosError(error) || error.response?.status !== 409) {
    return null;
  }

  const data = error.response.data as Partial<ExistingPoliciesConflict> | undefined;

  if (data?.code === 'existing_policies' && Array.isArray(data.policies_to_replace)) {
    return { code: 'existing_policies', policies_to_replace: data.policies_to_replace };
  }

  return null;
}

export async function getModelSetupStatus(
  projectId: string,
  teamId?: string | null,
): Promise<ModelSetupStatus> {
  const params: Record<string, string> = { project_id: projectId };

  if (teamId) {
    params.team_id = teamId;
  }

  const response = await apiClient().get<ModelSetupStatus>(
    '/v1/admin/model-setup/status',
    { params },
  );

  return response.data;
}

export async function getModelPresets(): Promise<{ presets: ModelPreset[] }> {
  const response = await apiClient().get<{ presets: ModelPreset[] }>(
    '/v1/admin/model-setup/presets',
  );

  return response.data;
}

export async function applyPreset(
  req: ApplyPresetRequest,
): Promise<ApplyPresetResponse> {
  const response = await apiClient().post<ApplyPresetResponse>(
    '/v1/admin/model-setup/apply',
    req,
  );

  return response.data;
}
