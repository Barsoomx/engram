/** @type {import('next').NextConfig} */

const isDev = process.env.NODE_ENV !== 'production'

function resolveApiOrigin() {
  const raw = process.env.NEXT_PUBLIC_ENGRAM_API_URL ?? 'http://localhost:8000'

  try {
    return new URL(raw).origin
  } catch {
    return 'http://localhost:8000'
  }
}

const apiOrigin = resolveApiOrigin()

const connectSrc = [
  "'self'",
  apiOrigin,
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

export default nextConfig
