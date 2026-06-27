import axios, { AxiosInstance, AxiosResponse } from 'axios';

const TOKEN_STORAGE_KEY = 'engram_token';

const API_URL = process.env.NEXT_PUBLIC_ENGRAM_API_URL ?? 'http://localhost:8000';

export type LoginResponse = {
  token: string;
  user_id: number;
  username: string;
  identity_id: string;
  organization_id: string;
  capabilities: string[];
};

export type MeResponse = {
  user_id: number;
  username: string;
  identity_id: string;
  organization_id: string;
  capabilities: string[];
};

export type AuthErrorDetail = string;

export function getToken(): string | null {
  if (typeof window === 'undefined') {

    return null;
  }

  return window.localStorage.getItem(TOKEN_STORAGE_KEY);
}

export function setToken(token: string): void {
  if (typeof window === 'undefined') {

    return;
  }

  window.localStorage.setItem(TOKEN_STORAGE_KEY, token);
}

export function clearToken(): void {
  if (typeof window === 'undefined') {

    return;
  }

  window.localStorage.removeItem(TOKEN_STORAGE_KEY);
}

export function apiClient(): AxiosInstance {
  const instance = axios.create({
    baseURL: API_URL.replace(/\/$/, ''),
    headers: {
      Accept: 'application/json',
      'Content-Type': 'application/json',
    },
    timeout: 15000,
  });

  const token = getToken();

  if (token) {
    instance.defaults.headers.common.Authorization = `Token ${token}`;
  }

  return instance;
}

export async function login(
  username: string,
  password: string,
): Promise<LoginResponse> {
  const client = apiClient();
  const response: AxiosResponse<LoginResponse> = await client.post(
    '/v1/auth/login',
    { username, password },
  );
  const payload = response.data;

  setToken(payload.token);

  return payload;
}

export async function fetchMe(): Promise<MeResponse> {
  const client = apiClient();
  const response: AxiosResponse<MeResponse> = await client.get('/v1/auth/me');

  return response.data;
}

export async function logout(): Promise<void> {
  const client = apiClient();

  try {
    await client.post('/v1/auth/logout');
  } finally {
    clearToken();
  }
}

export function extractAuthError(error: unknown): AuthErrorDetail {
  if (axios.isAxiosError(error)) {
    const data = error.response?.data as { detail?: string } | undefined;

    if (data?.detail) {

      return data.detail;
    }

    if (error.response?.status === 401 || error.response?.status === 403) {

      return 'Invalid username or password.';
    }

    return error.message;
  }

  if (error instanceof Error) {

    return error.message;
  }

  return 'Login failed.';
}
