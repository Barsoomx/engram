import * as Sentry from '@sentry/nextjs';

import {
  resolveRuntimeValue,
  resolveSampleRate,
  resolveSentryDsn,
} from '@/lib/sentry-runtime';

const dsn = resolveSentryDsn(process.env.NEXT_PUBLIC_SENTRY_DSN);

if (dsn) {
  Sentry.init({
    dsn,
    environment:
      resolveRuntimeValue(process.env.NEXT_PUBLIC_SENTRY_ENVIRONMENT) ??
      'production',
    release: resolveRuntimeValue(process.env.NEXT_PUBLIC_SENTRY_RELEASE),
    tracesSampleRate: resolveSampleRate(
      process.env.NEXT_PUBLIC_SENTRY_TRACES_SAMPLE_RATE,
    ),
    sendDefaultPii: false,
  });
}

export const onRouterTransitionStart = Sentry.captureRouterTransitionStart;
