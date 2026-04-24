'use client'
import { AlertTriangle, RefreshCw } from 'lucide-react'
import { Button } from '@/components/ui'
import { cn } from '@/lib/utils'
import type { AxiosError } from 'axios'

function extractMessage(error: unknown, fallback: string): string {
  const axErr = error as AxiosError<{ detail?: string | { message?: string } }>
  const detail = axErr?.response?.data?.detail
  if (typeof detail === 'string' && detail.trim()) return detail
  if (detail && typeof detail === 'object' && typeof detail.message === 'string') return detail.message
  if (axErr?.message) return axErr.message
  return fallback
}

interface QueryErrorProps {
  /** The error returned from useQuery */
  error: unknown
  /** Called when the user clicks Retry */
  onRetry?: () => void
  /** Human-readable label for what failed (e.g. "positions") */
  label?: string
  /** Compact single-line banner instead of centred card */
  inline?: boolean
  className?: string
}

/**
 * Consistent error state for failed useQuery calls.
 * Renders either a centred card (default) or a slim inline banner.
 *
 * Usage:
 *   const { data, isLoading, isError, error, refetch } = usePositions()
 *   if (isError) return <QueryError error={error} onRetry={refetch} label="positions" />
 */
export function QueryError({ error, onRetry, label = 'data', inline = false, className }: QueryErrorProps) {
  const message = extractMessage(error, `Failed to load ${label}`)

  if (inline) {
    return (
      <div className={cn(
        'flex items-center gap-2.5 rounded-lg border border-red-500/20 bg-red-500/5 px-3 py-2 text-xs text-red-400',
        className
      )}>
        <AlertTriangle className="w-3.5 h-3.5 shrink-0" />
        <span className="flex-1 truncate">{message}</span>
        {onRetry && (
          <button
            onClick={onRetry}
            className="shrink-0 text-red-400/70 hover:text-red-400 transition-colors"
            title="Retry"
          >
            <RefreshCw className="w-3.5 h-3.5" />
          </button>
        )}
      </div>
    )
  }

  return (
    <div className={cn(
      'flex flex-col items-center justify-center gap-4 py-16 text-center px-6',
      className
    )}>
      <div className="w-11 h-11 rounded-xl bg-red-500/10 border border-red-500/20 flex items-center justify-center">
        <AlertTriangle className="w-5 h-5 text-red-400" />
      </div>
      <div className="space-y-1 max-w-sm">
        <p className="text-sm font-semibold text-foreground">
          Failed to load {label}
        </p>
        <p className="text-xs text-muted-foreground leading-relaxed">{message}</p>
      </div>
      {onRetry && (
        <Button variant="outline" size="sm" onClick={onRetry}>
          <RefreshCw className="w-3.5 h-3.5" />
          Try again
        </Button>
      )}
    </div>
  )
}
