/** @type {import('next').NextConfig} */

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'
const API_HOST = (() => {
  try { return new URL(API_URL).host } catch { return 'localhost:8000' }
})()
const connectHosts = Array.from(new Set([
  API_HOST,
  'localhost:8000',
  '127.0.0.1:8000',
]))

// Build a strict CSP. In development we relax script-src for Next.js HMR.
const isDev = process.env.NODE_ENV === 'development'
const cspDirectives = [
  `default-src 'self'`,
  // Next.js requires 'unsafe-eval' in development for fast-refresh
  isDev
    ? `script-src 'self' 'unsafe-eval' 'unsafe-inline'`
    : `script-src 'self'`,
  `style-src 'self' 'unsafe-inline'`,   // Tailwind inlines critical CSS
  `img-src 'self' data: blob:`,
  `font-src 'self'`,
  // Allow API + WebSocket connections to the configured backend.
  // Also allow sentry.io when a DSN is configured.
  [
    `connect-src 'self'`,
    ...connectHosts.flatMap((host) => [
      `http://${host}`,
      `https://${host}`,
      `ws://${host}`,
      `wss://${host}`,
    ]),
    ...(process.env.NEXT_PUBLIC_SENTRY_DSN ? ['https://*.sentry.io'] : []),
  ].join(' '),
  `frame-ancestors 'none'`,
  `base-uri 'self'`,
  `form-action 'self'`,
  `object-src 'none'`,
].filter(Boolean).join('; ')

const securityHeaders = [
  { key: 'X-Content-Type-Options',  value: 'nosniff' },
  { key: 'X-Frame-Options',         value: 'DENY' },
  { key: 'X-XSS-Protection',        value: '1; mode=block' },
  { key: 'Referrer-Policy',         value: 'no-referrer' },
  { key: 'Permissions-Policy',      value: 'geolocation=(), microphone=(), camera=()' },
  { key: 'Content-Security-Policy', value: cspDirectives },
]

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  output: 'standalone',

  async headers() {
    return [
      {
        source: '/(.*)',
        headers: securityHeaders,
      },
    ]
  },

  async rewrites() {
    return [
      {
        source: '/api/v1/:path*',
        destination: `${API_URL}/v1/:path*`,
      },
    ]
  },
}

// ── Sentry — wrap only when package is installed ──────────────────────────────
// @sentry/nextjs is an optional dependency; next.config.js must remain loadable
// even before `npm install` has been run (e.g. in a fresh checkout without the
// lock file).  The try/catch ensures a missing package never breaks the build.
let exportedConfig = nextConfig
if (process.env.SENTRY_DSN || process.env.NEXT_PUBLIC_SENTRY_DSN) {
  try {
    const { withSentryConfig } = require('@sentry/nextjs')
    exportedConfig = withSentryConfig(nextConfig, {
      // Sentry webpack plugin options
      silent: !isDev,             // Suppress upload logs in production
      hideSourceMaps: true,       // Don't expose source maps to the browser

      // Tunnel Sentry requests through our own Next.js server to avoid ad-blockers
      // (requests go to /monitoring instead of directly to sentry.io)
      tunnelRoute: '/monitoring',

      // Automatically tree-shake Sentry logger statements in production
      disableLogger: !isDev,

      // Don't widen the CSP automatically — we manage it ourselves above
      autoInstrumentServerFunctions: true,
      autoInstrumentMiddleware: true,
    })
  } catch {
    // @sentry/nextjs not installed — continue without it
  }
}

module.exports = exportedConfig
