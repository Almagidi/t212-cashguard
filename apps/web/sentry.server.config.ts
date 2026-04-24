/**
 * Sentry Node.js (server-side) initialisation.
 * Runs in the Next.js Node runtime — API routes, server components, etc.
 * No-op when SENTRY_DSN is not set.
 */
import * as Sentry from '@sentry/nextjs'

const DSN = process.env.SENTRY_DSN ?? process.env.NEXT_PUBLIC_SENTRY_DSN

if (DSN) {
  Sentry.init({
    dsn: DSN,
    environment: process.env.NODE_ENV,
    release: process.env.NEXT_PUBLIC_APP_VERSION ?? 'cashguard@1.0.0',
    tracesSampleRate: 0.1,
    sendDefaultPii: false,
  })
}
