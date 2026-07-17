import * as Sentry from '@sentry/nextjs';

import {
  resolveRuntimeValue,
  resolveSampleRate,
  resolveSentryDsn,
} from '@/lib/sentry-runtime';

const dsn = resolveSentryDsn(
  process.env.SENTRY_DSN,
  process.env.NEXT_PUBLIC_SENTRY_DSN,
);

if (dsn) {
  Sentry.init({
    dsn,
    environment: resolveRuntimeValue(process.env.SENTRY_ENVIRONMENT) ?? 'production',
    release: resolveRuntimeValue(process.env.ENGRAM_RELEASE),
    tracesSampleRate: resolveSampleRate(process.env.SENTRY_TRACES_SAMPLE_RATE),
    sendDefaultPii: false,
  });
}
