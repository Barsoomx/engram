import axios from 'axios';

export function extractApiError(
  error: unknown,
  fallback = 'Something went wrong.',
): string {
  if (axios.isAxiosError(error)) {
    const data = error.response?.data;

    if (typeof data === 'string' && data.trim()) {
      return data;
    }

    if (data && typeof data === 'object') {
      const record = data as Record<string, unknown>;

      if (typeof record.detail === 'string' && record.detail) {
        return record.detail;
      }

      const messages: string[] = [];

      for (const [key, value] of Object.entries(record)) {
        if (key === 'code' || key === 'error_code') {
          continue;
        }

        const parts = Array.isArray(value) ? value : [value];
        const text = parts.filter((part) => typeof part === 'string').join(' ');

        if (text) {
          messages.push(key === 'non_field_errors' ? text : `${key}: ${text}`);
        }
      }

      if (messages.length > 0) {
        return messages.join('; ');
      }
    }

    return error.message || fallback;
  }

  if (error instanceof Error && error.message) {
    return error.message;
  }

  return fallback;
}
