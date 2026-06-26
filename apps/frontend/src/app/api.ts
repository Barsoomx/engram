const API_URL = process.env.NEXT_PUBLIC_ENGRAM_API_URL ?? 'http://localhost:8000';
const PROJECT_ID = process.env.NEXT_PUBLIC_ENGRAM_PROJECT_ID ?? '';
const TEAM_ID = process.env.NEXT_PUBLIC_ENGRAM_TEAM_ID ?? '';
const ADMIN_API_KEY = process.env.ENGRAM_ADMIN_API_KEY ?? '';

export type BackendHealth = {
  ok: boolean;
  detail: string;
};

export type AdminFetchError = {
  ok: false;
  reason: 'missing-config' | 'http-error' | 'network-error';
  detail: string;
  status?: number;
};

export type AdminFetchSuccess<T> = {
  ok: true;
  data: T;
};

export type AdminFetchResult<T> = AdminFetchSuccess<T> | AdminFetchError;

export type AdminRequestOptions = {
  projectId?: string;
  teamId?: string;
  searchParams?: Record<string, string>;
};

function buildAdminUrl(path: string, options: AdminRequestOptions): string {
  const normalizedPath = path.startsWith('/') ? path : `/${path}`;
  const url = new URL(`${API_URL.replace(/\/$/, '')}${normalizedPath}`);

  url.searchParams.set('project_id', options.projectId ?? PROJECT_ID);

  const teamId = options.teamId ?? TEAM_ID;

  if (teamId) {
    url.searchParams.set('team_id', teamId);
  }

  for (const [key, value] of Object.entries(options.searchParams ?? {})) {
    url.searchParams.set(key, value);
  }

  return url.toString();
}

export async function checkBackendHealth(
  path: string,
): Promise<BackendHealth> {
  const url = `${API_URL.replace(/\/$/, '')}${path}`;

  try {
    const response = await fetch(url, {
      cache: 'no-store',
      headers: { Accept: 'application/json' },
    });

    if (!response.ok) {
      return {
        ok: false,
        detail: `HTTP ${response.status} ${response.statusText}`,
      };
    }

    const text = await response.text();

    return { ok: true, detail: text };
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);

    return {
      ok: false,
      detail: `Fetch failed: ${message}. API_URL=${API_URL}`,
    };
  }
}

export async function fetchAdminJson<T>(
  path: string,
  options: AdminRequestOptions = {},
): Promise<AdminFetchResult<T>> {
  const projectId = options.projectId ?? PROJECT_ID;

  if (!projectId) {
    return {
      ok: false,
      reason: 'missing-config',
      detail: 'NEXT_PUBLIC_ENGRAM_PROJECT_ID is not set.',
    };
  }

  if (!ADMIN_API_KEY) {
    return {
      ok: false,
      reason: 'missing-config',
      detail: 'ENGRA_ADMIN_API_KEY is not set.',
    };
  }

  const url = buildAdminUrl(path, options);

  try {
    const response = await fetch(url, {
      cache: 'no-store',
      headers: {
        Accept: 'application/json',
        Authorization: `Bearer ${ADMIN_API_KEY}`,
      },
    });

    if (!response.ok) {
      const text = await response.text().catch(() => '');

      return {
        ok: false,
        reason: 'http-error',
        status: response.status,
        detail: `HTTP ${response.status} ${response.statusText}${text ? `: ${text}` : ''}`,
      };
    }

    const data = (await response.json()) as T;

    return { ok: true, data };
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);

    return {
      ok: false,
      reason: 'network-error',
      detail: `Fetch failed: ${message}. API_URL=${API_URL}`,
    };
  }
}
