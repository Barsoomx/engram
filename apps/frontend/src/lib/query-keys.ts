export type ListParams = {
  search?: string;
  page?: number;
  pageSize?: number;
  [key: string]: unknown;
};

export type MetricsScope = {
  project_id?: string;
  team_id?: string;
};

export const adminQueryKeys = {
  all: (orgId: string | null) => ['admin', orgId] as const,

  organizations: (orgId: string | null, params?: ListParams) =>
    ['admin', orgId, 'organizations', params ?? {}] as const,

  teams: (orgId: string | null, params?: ListParams) =>
    ['admin', orgId, 'teams', params ?? {}] as const,

  projects: (orgId: string | null, params?: ListParams) =>
    ['admin', orgId, 'projects', params ?? {}] as const,

  members: (orgId: string | null, params?: ListParams) =>
    ['admin', orgId, 'members', params ?? {}] as const,

  roles: (orgId: string | null, params?: ListParams) =>
    ['admin', orgId, 'roles', params ?? {}] as const,

  apiKeys: (orgId: string | null, params?: ListParams) =>
    ['admin', orgId, 'api-keys', params ?? {}] as const,

  workflowRuns: (orgId: string | null, params?: ListParams) =>
    ['admin', orgId, 'workflow-runs', params ?? {}] as const,

  workflowRun: (orgId: string | null, id: string | null) =>
    ['admin', orgId, 'workflow-run', id] as const,

  imports: (orgId: string | null, params?: ListParams) =>
    ['admin', orgId, 'imports', params ?? {}] as const,

  importJob: (orgId: string | null, id: string | null) =>
    ['admin', orgId, 'import', id] as const,

  memoryReview: (orgId: string | null, params?: ListParams) =>
    ['admin', orgId, 'memory-review', params ?? {}] as const,

  memoryConflict: (orgId: string | null, candidateId: string | null) =>
    ['admin', orgId, 'memory-review', 'conflict', candidateId] as const,

  metricsOverview: (orgId: string | null, scope?: MetricsScope) =>
    ['admin', orgId, 'metrics', 'overview', scope?.project_id ?? null, scope?.team_id ?? null] as const,

  memoryIngest: (orgId: string | null, scope?: MetricsScope) =>
    ['admin', orgId, 'metrics', 'memory-ingest', scope?.project_id ?? null, scope?.team_id ?? null] as const,

  sessions: (orgId: string | null, scope?: MetricsScope) =>
    ['admin', orgId, 'metrics', 'sessions', scope?.project_id ?? null, scope?.team_id ?? null] as const,

  activity: (orgId: string | null, scope?: MetricsScope) =>
    ['admin', orgId, 'metrics', 'activity', scope?.project_id ?? null, scope?.team_id ?? null] as const,

  opsOverview: (orgId: string | null) =>
    ['admin', orgId, 'ops', 'overview'] as const,

  settingsRetrieval: (orgId: string | null) =>
    ['admin', orgId, 'settings', 'retrieval'] as const,

  settingsEmbedding: (orgId: string | null) =>
    ['admin', orgId, 'settings', 'embedding'] as const,

  auditEvents: (orgId: string | null, params?: ListParams) =>
    ['admin', orgId, 'audit-events', params ?? {}] as const,

  modelSetupStatus: (
    orgId: string | null,
    projectId: string | null,
    teamId: string | null,
  ) =>
    ['admin', orgId, 'model-setup', 'status', projectId, teamId] as const,

  modelPresets: (orgId: string | null) =>
    ['admin', orgId, 'model-setup', 'presets'] as const,
};
