/**
 * Sentry browser-side initialisation.
 * This file is imported automatically by Next.js when @sentry/nextjs is installed.
 * It is a no-op when NEXT_PUBLIC_SENTRY_DSN is not set.
 */
import * as Sentry from '@sentry/nextjs'
import {
  confidentialTelemetryIntegration,
  sanitizeTelemetryBreadcrumb,
  sanitizeTelemetryEvent,
  type TelemetryIntegration,
} from './lib/telemetry-privacy'

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

    // Replay and other unqualified channels stay withheld.
    replaysSessionSampleRate: 0,
    replaysOnErrorSampleRate: 0,
    enableLogs: false,
    enableMetrics: false,
    sendClientReports: false,
    integrations: (defaults: TelemetryIntegration[]) => [
      confidentialTelemetryIntegration(),
      ...defaults,
    ],
    beforeSend: sanitizeTelemetryEvent,
    beforeSendTransaction: sanitizeTelemetryEvent,
    beforeBreadcrumb: sanitizeTelemetryBreadcrumb,
  })
}
