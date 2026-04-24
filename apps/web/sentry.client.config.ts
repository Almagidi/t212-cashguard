/**
 * Sentry browser-side initialisation.
 * This file is imported automatically by Next.js when @sentry/nextjs is installed.
 * It is a no-op when NEXT_PUBLIC_SENTRY_DSN is not set.
 */
import * as Sentry from '@sentry/nextjs'

const DSN = process.env.NEXT_PUBLIC_SENTRY_DSN

if (DSN) {
  Sentry.init({
    dsn: DSN,
    environment: process.env.NODE_ENV,
    release: process.env.NEXT_PUBLIC_APP_VERSION ?? 'cashguard@1.0.0',

    // Capture 100 % of errors, 10 % of performance transactions
    tracesSampleRate: 0.1,

    // Never send PII — auth tokens, cookies, user details
    sendDefaultPii: false,

    // Replay — record a short clip of what the user did before an error
    // 1 % of sessions, 100 % when an error occurs
    replaysSessionSampleRate: 0.01,
    replaysOnErrorSampleRate: 1.0,

    integrations: [
      Sentry.replayIntegration({
        // Mask all text and block all media to avoid capturing PII
        maskAllText: true,
        blockAllMedia: true,
      }),
    ],

    // Strip auth headers from outgoing fetch/XHR breadcrumbs
    beforeBreadcrumb(breadcrumb: Sentry.Breadcrumb) {
      if (breadcrumb.category === 'fetch' || breadcrumb.category === 'xhr') {
        const data = breadcrumb.data ?? {}
        if (data.headers?.authorization) data.headers.authorization = '[Filtered]'
      }
      return breadcrumb
    },
  })
}
