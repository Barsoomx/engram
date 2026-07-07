import { apiClient } from '@/lib/auth';
import type { ListParams } from '@/lib/query-keys';

export type Organization = {
  id: string;
  name: string;
  slug: string;
  status?: string;
  created_at: string;
  updated_at: string;
  member_count?: number | null;
  viewer_role?: string | null;
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

export type OrganizationWriteInput = {
  name: string;
};

export async function updateOrganization(
  id: string,
  input: OrganizationWriteInput,
): Promise<Organization> {
  const client = apiClient();
  const response = await client.patch<Organization>(
    `/v1/admin/organizations/${id}/`,
    input,
  );

  return response.data;
}

export type ApiKeyOwner = {
  id: string;
  display_name: string;
};

export type ApiKey = {
  id: string;
  name: string;
  key_prefix: string;
  key_fingerprint: string;
  owner_identity: ApiKeyOwner;
  capabilities: string[];
  created_at: string;
  expires_at: string | null;
  last_used_at: string | null;
  active: boolean;
  revoked_at: string | null;
};

export type ApiKeyIssueInput = {
  name: string;
  capabilities: string[];
  expires_at?: string | null;
};

export type ApiKeyIssueResult = {
  id: string;
  name: string;
  key_prefix: string;
  key_fingerprint: string;
  plaintext: string;
  capabilities: string[];
  created_at: string;
};

export type Team = {
  id: string;
  name: string;
  slug: string;
  organization: string;
  created_at: string;
  updated_at: string;
  archived_at: string | null;
};

export type TeamWriteInput = {
  name: string;
  slug: string;
};

export async function listTeams(
  params?: ListParams,
): Promise<Paginated<Team>> {
  const client = apiClient();
  const response = await client.get<Paginated<Team>>(
    '/v1/admin/teams/',
    { params },
  );

  return response.data;
}

export async function createTeam(input: TeamWriteInput): Promise<Team> {
  const client = apiClient();
  const response = await client.post<Team>('/v1/admin/teams/', input);

  return response.data;
}

export async function updateTeam(
  id: string,
  input: TeamWriteInput,
): Promise<Team> {
  const client = apiClient();
  const response = await client.patch<Team>(
    `/v1/admin/teams/${id}/`,
    input,
  );

  return response.data;
}

export async function archiveTeam(id: string): Promise<void> {
  const client = apiClient();

  await client.delete(`/v1/admin/teams/${id}/`);
}

export type Project = {
  id: string;
  name: string;
  slug: string;
  organization: string;
  repository_url: string;
  default_branch: string;
  memory_count?: number;
  created_at: string;
  updated_at: string;
  archived_at: string | null;
};

export type ProjectWriteInput = {
  name: string;
  slug: string;
  repository_url: string;
  default_branch: string;
};

export type ProjectOrdering = '-created_at' | 'name';

export type ProjectListParams = ListParams & {
  ordering?: ProjectOrdering;
};

export async function listProjects(
  params?: ProjectListParams,
): Promise<Paginated<Project>> {
  const client = apiClient();
  const response = await client.get<Paginated<Project>>(
    '/v1/admin/projects/',
    { params },
  );

  return response.data;
}

export async function createProject(
  input: ProjectWriteInput,
): Promise<Project> {
  const client = apiClient();
  const response = await client.post<Project>('/v1/admin/projects/', input);

  return response.data;
}

export async function updateProject(
  id: string,
  input: ProjectWriteInput,
): Promise<Project> {
  const client = apiClient();
  const response = await client.patch<Project>(
    `/v1/admin/projects/${id}/`,
    input,
  );

  return response.data;
}

export async function archiveProject(id: string): Promise<void> {
  const client = apiClient();

  await client.delete(`/v1/admin/projects/${id}/`);
}

export type Role = {
  id: string;
  code: string;
  name: string;
  description?: string;
  built_in: boolean;
  capabilities: string[];
};

export async function listRoles(
  params?: ListParams,
): Promise<Paginated<Role>> {
  const client = apiClient();
  const response = await client.get<Paginated<Role>>(
    '/v1/admin/roles/',
    { params },
  );

  return response.data;
}

export type MembershipStatus = 'active' | 'invited' | 'suspended';

export type Member = {
  id: string;
  external_id: string;
  display_name: string;
  email: string;
  identity_type: string;
  active: boolean;
  status?: MembershipStatus;
  role: string;
  role_name?: string;
};

export type MemberInviteInput = {
  external_id: string;
  display_name: string;
  email?: string;
  role: string;
};

export type MemberRoleInput = {
  role: string;
};

export type MemberListParams = ListParams & {
  role?: string;
  active?: boolean;
};

export async function listMembers(
  params?: MemberListParams,
): Promise<Paginated<Member>> {
  const client = apiClient();
  const response = await client.get<Paginated<Member>>(
    '/v1/admin/members/',
    { params },
  );

  return response.data;
}

export async function inviteMember(
  input: MemberInviteInput,
): Promise<Member> {
  const client = apiClient();
  const response = await client.post<Member>('/v1/admin/members/', input);

  return response.data;
}

export async function updateMemberRole(
  id: string,
  input: MemberRoleInput,
): Promise<Member> {
  const client = apiClient();
  const response = await client.patch<Member>(
    `/v1/admin/members/${id}/`,
    input,
  );

  return response.data;
}

export async function deactivateMember(id: string): Promise<void> {
  const client = apiClient();

  await client.delete(`/v1/admin/members/${id}/`);
}

export async function reactivateMember(id: string): Promise<void> {
  const client = apiClient();

  await client.post(`/v1/admin/members/${id}/reactivate/`);
}

export type ApiKeyStatus = 'active' | 'expired' | 'revoked';

export type ApiKeyListParams = ListParams & {
  status?: ApiKeyStatus;
};

export async function listApiKeys(
  params?: ApiKeyListParams,
): Promise<Paginated<ApiKey>> {
  const client = apiClient();
  const response = await client.get<Paginated<ApiKey>>(
    '/v1/admin/api-keys/',
    { params },
  );

  return response.data;
}

export async function issueApiKey(
  input: ApiKeyIssueInput,
): Promise<ApiKeyIssueResult> {
  const client = apiClient();
  const response = await client.post<ApiKeyIssueResult>(
    '/v1/admin/api-keys/',
    input,
  );

  return response.data;
}

export async function revokeApiKey(id: string): Promise<void> {
  const client = apiClient();

  await client.post(`/v1/admin/api-keys/${id}/revoke/`);
}

export type WorkflowRunStatus = 'queued' | 'running' | 'succeeded' | 'failed';

export type WorkflowRunType =
  | 'daily_digest'
  | 'observation_processing'
  | 'session_distillation'
  | 'weekly_digest';

export type WorkflowRunListItem = {
  id: string;
  organization_id: string;
  project_id: string;
  team_id: string | null;
  run_type: WorkflowRunType;
  status: WorkflowRunStatus;
  escalation: boolean;
  request_id: string;
  correlation_id: string;
  result_memory_id: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
};

export type WorkflowRunCuratorAction = {
  id: string;
  event_type: string;
  actor_type: string;
  target_type: string | null;
  target_id: string | null;
  result: string;
  created_at: string;
};

export type WorkflowRunProviderCall = {
  id: string;
  provider: string;
  model: string;
  task_type: string;
  result: string;
  latency_ms: number | null;
};

export type WorkflowRunResultMemory = {
  id: string;
  title: string;
  status: string;
};

export type WorkflowRunDetail = {
  id: string;
  organization_id: string;
  project_id: string;
  team_id: string | null;
  run_type: WorkflowRunType;
  status: WorkflowRunStatus;
  input_snapshot: Record<string, unknown>;
  provider_call_ids: string[];
  result_memory: WorkflowRunResultMemory | null;
  curator_actions: WorkflowRunCuratorAction[];
  provider_calls: WorkflowRunProviderCall[];
  escalation: boolean;
  failure_reason: string;
  request_id: string;
  correlation_id: string;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
  rerun_of_id: string | null;
};

export type WorkflowRunListParams = ListParams & {
  run_type?: WorkflowRunType;
  status?: WorkflowRunStatus;
  project_id?: string;
  team_id?: string;
  escalation?: boolean;
  request_id?: string;
  correlation_id?: string;
  created_at__gte?: string;
  created_at__lte?: string;
};

export type WorkflowRunRerunResult = {
  run_id: string | null;
  result_memory_id: string;
};

export async function listWorkflowRuns(
  params?: WorkflowRunListParams,
): Promise<Paginated<WorkflowRunListItem>> {
  const client = apiClient();
  const response = await client.get<Paginated<WorkflowRunListItem>>(
    '/v1/admin/workflow-runs/',
    { params },
  );

  return response.data;
}

export async function workflowRunDetail(
  id: string,
): Promise<WorkflowRunDetail> {
  const client = apiClient();
  const response = await client.get<WorkflowRunDetail>(
    `/v1/admin/workflow-runs/${id}/`,
  );

  return response.data;
}

export async function rerunWorkflowRun(
  id: string,
): Promise<WorkflowRunRerunResult> {
  const client = apiClient();
  const response = await client.post<WorkflowRunRerunResult>(
    `/v1/admin/workflow-runs/${id}/rerun/`,
  );

  return response.data;
}

export type ImportJobStatus =
  | 'created'
  | 'receiving'
  | 'succeeded'
  | 'failed'
  | 'expired';

export type ImportJobReport = {
  created?: Record<string, number>;
  duplicates?: Record<string, number>;
  counts?: Record<string, { client_rows?: number }>;
  unsupported?: unknown[];
  warnings?: unknown[];
  redactions?: { redacted?: boolean };
  truncations?: { truncated?: boolean };
  source_store_id?: string;
  [key: string]: unknown;
};

export type ImportJob = {
  id: string;
  source_store_id: string;
  status: ImportJobStatus;
  project: string | null;
  project_name: string;
  team: string | null;
  manifest: Record<string, unknown>;
  batches_applied: number;
  rows_created: number;
  rows_duplicate: number;
  report: ImportJobReport;
  failure_reason: string;
  created_at: string;
  updated_at: string;
};

export const TERMINAL_IMPORT_STATUSES: ReadonlySet<ImportJobStatus> = new Set<ImportJobStatus>([
  'succeeded',
  'failed',
  'expired',
]);

export function isTerminalImportStatus(status: ImportJobStatus): boolean {
  return TERMINAL_IMPORT_STATUSES.has(status);
}

export async function listImports(
  params?: ListParams,
): Promise<Paginated<ImportJob>> {
  const client = apiClient();
  const response = await client.get<Paginated<ImportJob>>(
    '/v1/admin/imports/',
    { params },
  );

  return response.data;
}

export async function importDetail(id: string): Promise<ImportJob> {
  const client = apiClient();
  const response = await client.get<ImportJob>(`/v1/admin/imports/${id}/`);

  return response.data;
}

export type CancelImportResult = {
  status: ImportJobStatus;
  failure_reason: string;
};

export async function cancelImport(id: string): Promise<CancelImportResult> {
  const client = apiClient();
  const response = await client.post<CancelImportResult>(
    `/v1/admin/imports/${id}/cancel`,
  );

  return response.data;
}

export type MemoryReviewItemType = 'candidate' | 'memory';

export type MemoryReviewSourceSummary = {
  id: string;
  title: string;
  files_read: string[] | null;
  files_modified: string[] | null;
};

export type MemoryReviewCitation = {
  id: string;
  link_type: string;
  target: string;
  label: string;
};

export type MemoryReviewItem = {
  id: string;
  type: MemoryReviewItemType;
  title: string;
  body: string;
  status: string;
  current_version: number;
  confidence: string | null;
  visibility_scope: string;
  team_id: string | null;
  project_id: string;
  evidence: unknown;
  source_observation: MemoryReviewSourceSummary | null;
  citations: MemoryReviewCitation[];
  created_at: string;
};

export type MemoryReviewOrdering =
  | 'confidence'
  | '-confidence'
  | 'created_at'
  | '-created_at';

export type MemoryReviewListParams = ListParams & {
  team_id?: string;
  project_id?: string;
  visibility_scope?: string;
  confidence__gte?: string;
  confidence__lte?: string;
  status?: string;
  age_days__gte?: number;
  source_type?: string;
  ordering?: MemoryReviewOrdering;
  page?: number;
};

export type MemoryReviewDiffSlice = {
  version: number;
  body: string;
  created_at: string;
};

export type MemoryReviewDiff = {
  from: MemoryReviewDiffSlice;
  to: MemoryReviewDiffSlice;
};

export type MemoryReviewActionName =
  | 'approve'
  | 'edit'
  | 'narrow'
  | 'supersede'
  | 'reject'
  | 'archive'
  | 'restore';

export type MemoryReviewActionPayload = {
  action: MemoryReviewActionName;
  reason: string;
  body?: string;
  target_memory_id?: string;
};

export type MemoryReviewActionResult = {
  action: MemoryReviewActionName;
  candidate_id?: string;
  memory_id?: string;
  version?: number;
  link_id?: string;
};

export type BulkArchiveMemoryReviewPayload = {
  ids?: string[];
  confidence__lte?: string;
  project_id?: string;
  team_id?: string;
  reason: string;
};

export type BulkArchiveMemoryReviewResult = {
  archived_count: number;
  archived_ids: string[];
};

export async function listMemoryReview(
  params?: MemoryReviewListParams,
): Promise<Paginated<MemoryReviewItem>> {
  const client = apiClient();
  const response = await client.get<Paginated<MemoryReviewItem>>(
    '/v1/admin/memory-review/',
    { params },
  );

  return response.data;
}

export async function memoryReviewDiff(
  id: string,
  fromVersion: number,
  toVersion: number,
): Promise<MemoryReviewDiff> {
  const client = apiClient();
  const response = await client.get<MemoryReviewDiff>(
    `/v1/admin/memory-review/${id}/diff/`,
    { params: { from_version: fromVersion, to_version: toVersion } },
  );

  return response.data;
}

export async function memoryReviewAction(
  id: string,
  payload: MemoryReviewActionPayload,
): Promise<MemoryReviewActionResult> {
  const client = apiClient();
  const response = await client.post<MemoryReviewActionResult>(
    `/v1/admin/memory-review/${id}/action/`,
    payload,
  );

  return response.data;
}

export async function bulkArchiveMemoryReview(
  payload: BulkArchiveMemoryReviewPayload,
): Promise<BulkArchiveMemoryReviewResult> {
  const client = apiClient();
  const response = await client.post<BulkArchiveMemoryReviewResult>(
    '/v1/admin/memory-review/bulk-archive/',
    payload,
  );

  return response.data;
}

export type AuditEvent = {
  id: string;
  event_type: string;
  actor_type: string;
  actor_id: string;
  actor_display: string | null;
  target_type: string;
  target_id: string;
  target_display: string | null;
  capability: string;
  result: string;
  request_id: string;
  metadata: Record<string, unknown> | null;
  project_id: string | null;
  team_id: string | null;
  created_at: string;
};

export type AuditEventListParams = ListParams & {
  event_type?: string;
  result?: string;
  actor_id?: string;
  target_type?: string;
  project_id?: string;
  team_id?: string;
  created_at__gte?: string;
  created_at__lt?: string;
};

export async function listAuditEvents(
  params?: AuditEventListParams,
): Promise<Paginated<AuditEvent>> {
  const client = apiClient();
  const response = await client.get<Paginated<AuditEvent>>(
    '/v1/admin/audit-events/',
    { params },
  );

  return response.data;
}
