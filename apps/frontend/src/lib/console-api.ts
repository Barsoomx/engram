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
}

function scopeQuery({ projectId, teamId }: ScopeParams): Record<string, string> {
  const params: Record<string, string> = { project_id: projectId };

  if (teamId) {
    params.team_id = teamId;
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
}

export interface SearchDebugSemanticCandidate {
  memory_id: string;
  title: string;
  score: number;
}

export interface SearchDebugPackedItem {
  memory_id: string;
  title: string;
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
  token_budget: number;
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
  rank: number;
  citation: string;
  inclusion_reason: string;
  scope_evidence: Record<string, unknown> | null;
  metadata: Record<string, unknown> | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface ContextBundleDetail extends ContextBundleListItem {
  rendered_text: string;
  authorization_scope: Record<string, unknown> | null;
  metadata: Record<string, unknown> | null;
  items: ContextBundleEntry[];
}

export async function listContextBundles(
  scope: ScopeParams,
): Promise<{ count: number; items: ContextBundleListItem[] }> {
  const response = await apiClient().get('/v1/inspection/context-bundles', {
    params: scopeQuery(scope),
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

export type SecretProvider = 'anthropic' | 'openai';
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

export async function listProviderSecrets(
  scope: ScopeParams,
): Promise<ProviderSecret[]> {
  try {
    const response = await apiClient().get('/v1/model-policy/secrets', {
      params: scopeQuery(scope),
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
export type PolicyTaskType =
  | 'generation'
  | 'embedding'
  | 'curation'
  | 'digest'
  | 'rerank'
  | 'admin_assistant';

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
}

export interface ModelPolicyResolveParams {
  project_id: string;
  team_id?: string | null;
  task_type: PolicyTaskType;
}

export async function listModelPolicies(
  scope: ScopeParams,
): Promise<ModelPolicy[]> {
  try {
    const response = await apiClient().get('/v1/model-policy/policies', {
      params: scopeQuery(scope),
    });

    return listEnvelope<ModelPolicy>(response.data).items;
  } catch (error) {
    if (isMissingListEndpoint(error)) {
      return [];
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
  'rerank',
  'admin_assistant',
];

export const SECRET_PROVIDERS: SecretProvider[] = ['anthropic', 'openai'];
