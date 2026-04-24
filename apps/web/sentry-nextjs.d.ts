declare module '@sentry/nextjs' {
  export interface Breadcrumb {
    category?: string
    data?: Record<string, any>
  }

  export function init(config: Record<string, any>): void

  export function replayIntegration(config?: Record<string, any>): unknown
}
