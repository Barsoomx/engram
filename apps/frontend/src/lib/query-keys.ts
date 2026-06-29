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
};
