'use client'

import React from 'react'
import { AlertTriangle, RefreshCw } from 'lucide-react'
import { Button } from '@/components/ui'

interface Props {
  children: React.ReactNode
  /** Shown in the fallback heading — e.g. "Dashboard" */
  label?: string
  /** If true, renders a compact inline error instead of a full card */
  inline?: boolean
}

interface State {
  error: Error | null
}

export class ErrorBoundary extends React.Component<Props, State> {
  constructor(props: Props) {
    super(props)
    this.state = { error: null }
  }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    // Surface to console so it's visible in dev
    console.error('[ErrorBoundary]', error, info.componentStack)
  }

  reset = () => this.setState({ error: null })

  render() {
    const { error } = this.state
    const { children, label, inline } = this.props

    if (!error) return children

    if (inline) {
      return (
        <div className="flex items-center gap-2 rounded-lg border border-red-500/20 bg-red-500/5 px-3 py-2 text-xs text-red-400">
          <AlertTriangle className="h-3.5 w-3.5 flex-shrink-0" />
          <span className="flex-1 truncate">{label ?? 'Component'} failed to render</span>
          <button
            onClick={this.reset}
            className="flex-shrink-0 text-red-400/60 underline-offset-2 hover:text-red-400 hover:underline"
          >
            retry
          </button>
        </div>
      )
    }

    return (
      <div className="flex min-h-[200px] flex-col items-center justify-center gap-4 rounded-xl border border-red-500/20 bg-red-500/5 p-8 text-center">
        <div className="flex h-12 w-12 items-center justify-center rounded-xl border border-red-500/25 bg-red-500/10">
          <AlertTriangle className="h-6 w-6 text-red-400" />
        </div>
        <div>
          <p className="text-sm font-semibold text-red-400">
            {label ? `${label} failed to load` : 'Something went wrong'}
          </p>
          <p className="mt-1 max-w-sm text-xs text-muted-foreground">
            {error.message || 'An unexpected error occurred. Your positions and orders are unaffected.'}
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={this.reset}>
          <RefreshCw className="h-3.5 w-3.5" />
          Try again
        </Button>
      </div>
    )
  }
}

/**
 * Lightweight function-component wrapper for use in RSC trees where you
 * can't instantiate a class component directly.
 */
export function withErrorBoundary<P extends object>(
  Component: React.ComponentType<P>,
  label?: string,
) {
  const Wrapped = (props: P) => (
    <ErrorBoundary label={label}>
      <Component {...props} />
    </ErrorBoundary>
  )
  Wrapped.displayName = `WithErrorBoundary(${Component.displayName ?? Component.name})`
  return Wrapped
}
