import { codecovNextJSWebpackPlugin } from '@codecov/nextjs-webpack-plugin'
import { withSentryConfig } from '@sentry/nextjs'

/** @type {import('next').NextConfig} */

const isDev = process.env.NODE_ENV !== 'production'

function resolveApiOrigin() {
  const raw = process.env.NEXT_PUBLIC_ENGRAM_API_URL ?? 'http://localhost:8000'

  // Runtime-config placeholder (apps/frontend/entrypoint.sh): pass it through unparsed so
  // the CSP connect-src carries the APP_* token, replaced with the real origin at
  // container start — otherwise the placeholder would fall back to localhost and the CSP
  // would block the real API in production.
  if (raw.startsWith('APP_')) {
    return raw
  }

  try {
    return new URL(raw).origin
  } catch {
    return 'http://localhost:8000'
  }
}

// The browser SDK POSTs events straight to the Sentry ingest host, so that origin must be
// in connect-src or every event is silently blocked by the CSP. The DSN itself can't be
// used here (CSP host-sources reject the `key@` userinfo), and it is a runtime placeholder
// at build time anyway — hence a dedicated origin-only variable, passed through with the
// same APP_* token mechanism as the API origin. Unset => Sentry stays out of the CSP.
function resolveSentryCspOrigin() {
  const raw = process.env.NEXT_PUBLIC_SENTRY_CSP_ORIGIN

  if (!raw) {
    return null
  }

  if (raw.startsWith('APP_')) {
    return raw
  }

  try {
    return new URL(raw).origin
  } catch {
    return null
  }
}

const apiOrigin = resolveApiOrigin()
const sentryCspOrigin = resolveSentryCspOrigin()

const connectSrc = [
  "'self'",
  apiOrigin,
  sentryCspOrigin,
  ...(isDev ? ['ws:', 'wss:'] : []),
]
  .filter(Boolean)
  .join(' ')

const cspHeader = [
  "default-src 'self'",
  "script-src 'self' 'unsafe-eval' 'unsafe-inline'",
  "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
  "font-src 'self' https://fonts.gstatic.com",
  "img-src 'self' blob: data:",
  `connect-src ${connectSrc}`,
  "frame-ancestors 'none'",
  "base-uri 'self'",
  "form-action 'self'",
].join('; ')

const nextConfig = {
  reactStrictMode: true,
  webpack: (config, options) => {
    // Codecov bundle analysis — only active when CODECOV_TOKEN is present (CI build),
    // so the tokenless Docker image build is a no-op.
    config.plugins.push(
      codecovNextJSWebpackPlugin({
        enableBundleAnalysis: Boolean(process.env.CODECOV_TOKEN),
        bundleName: 'engram-frontend',
        uploadToken: process.env.CODECOV_TOKEN,
        webpack: options.webpack,
      }),
    )

    return config
  },
  async headers() {
    return [
      {
        source: '/(.*)',
        headers: [
          { key: 'Content-Security-Policy', value: cspHeader },
          { key: 'X-Frame-Options', value: 'DENY' },
          { key: 'X-Content-Type-Options', value: 'nosniff' },
          { key: 'Referrer-Policy', value: 'strict-origin-when-cross-origin' },
        ],
      },
    ]
  },
}

// Sourcemap upload and release creation are deliberately off: both would require
// SENTRY_AUTH_TOKEN at build time, and the image is built generically in CI with no Sentry
// credentials. Stack traces arrive minified — accepted for now.
export default withSentryConfig(nextConfig, {
  silent: true,
  telemetry: false,
  sourcemaps: { disable: true },
  release: { create: false, finalize: false },
})
