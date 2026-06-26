const API_URL = process.env.NEXT_PUBLIC_ENGRAM_API_URL ?? 'http://localhost:8000';

export type BackendHealth = {
  ok: boolean;
  detail: string;
};

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
