// Runtime Sentry config resolution.
//
// NEXT_PUBLIC_* values are inlined at build time, so the browser bundle ships the literal
// `APP_NEXT_PUBLIC_<X>` placeholder that apps/frontend/entrypoint.sh replaces at container
// start from the real env. A placeholder that was never substituted means "not configured"
// — treat it as absent rather than feeding a bogus value to the SDK.
const RUNTIME_PLACEHOLDER_PREFIX = 'APP_';

export function resolveRuntimeValue(raw: string | undefined): string | undefined {
  const value = raw?.trim();

  if (!value || value.startsWith(RUNTIME_PLACEHOLDER_PREFIX)) {
    return undefined;
  }

  return value;
}

export function resolveSentryDsn(
  ...candidates: Array<string | undefined>
): string | undefined {
  for (const candidate of candidates) {
    const value = resolveRuntimeValue(candidate);

    if (value) {
      return value;
    }
  }

  return undefined;
}

export function resolveSampleRate(raw: string | undefined): number {
  const value = resolveRuntimeValue(raw);

  if (value === undefined) {
    return 0;
  }

  const parsed = Number(value);

  if (!Number.isFinite(parsed) || parsed < 0 || parsed > 1) {
    return 0;
  }

  return parsed;
}
