export type ListParams = {
  search?: string;
  page?: number;
  pageSize?: number;
  [key: string]: unknown;
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

  memoryReview: (orgId: string | null, params?: ListParams) =>
    ['admin', orgId, 'memory-review', params ?? {}] as const,

  memoryReviewDiff: (
    orgId: string | null,
    memoryId: string | null,
    fromVersion: number | null,
    toVersion: number | null,
  ) =>
    [
      'admin',
      orgId,
      'memory-review',
      'diff',
      memoryId,
      fromVersion,
      toVersion,
    ] as const,

  metricsOverview: (orgId: string | null) =>
    ['admin', orgId, 'metrics', 'overview'] as const,

  memoryIngest: (orgId: string | null) =>
    ['admin', orgId, 'metrics', 'memory-ingest'] as const,

  sessions: (orgId: string | null) =>
    ['admin', orgId, 'metrics', 'sessions'] as const,

  activity: (orgId: string | null) =>
    ['admin', orgId, 'metrics', 'activity'] as const,

  opsOverview: (orgId: string | null) =>
    ['admin', orgId, 'ops', 'overview'] as const,

  settingsRetrieval: (orgId: string | null) =>
    ['admin', orgId, 'settings', 'retrieval'] as const,

  settingsEmbedding: (orgId: string | null) =>
    ['admin', orgId, 'settings', 'embedding'] as const,

  auditEvents: (orgId: string | null, params?: ListParams) =>
    ['admin', orgId, 'audit-events', params ?? {}] as const,

  modelSetupStatus: (orgId: string | null, projectId: string | null) =>
    ['admin', orgId, 'model-setup', 'status', projectId] as const,

  modelPresets: (orgId: string | null) =>
    ['admin', orgId, 'model-setup', 'presets'] as const,
};
