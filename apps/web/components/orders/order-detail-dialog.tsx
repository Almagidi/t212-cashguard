'use client'

import * as React from 'react'
import { AlertTriangle, CheckSquare, ChevronDown, ChevronRight, Clock3, Pin, Square, X } from 'lucide-react'

import type { OrderDetail } from '@/types'
import { Badge, Button, Card } from '@/components/ui'
import { cn, executionQualityBadge, executionQualityClass, formatCurrency, formatDate, orderStatusBg } from '@/lib/utils'

interface OrderDetailDialogProps {
  open: boolean
  onClose: () => void
  order: OrderDetail | null
  loading?: boolean
}

type DiffKind = 'changed' | 'added' | 'removed' | 'unchanged'

interface DiffRow {
  path: string
  kind: DiffKind
  left: string
  right: string
}

interface CompareCandidate {
  id: string
  label: string
  subtitle: string
  payload: Record<string, unknown> | null
  sourceType: 'signal' | 'request' | 'response' | 'order' | 'event'
}

function formatDiffValue(value: unknown): string {
  if (value === undefined) return '—'
  if (value === null) return 'null'
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  return JSON.stringify(value)
}

function flattenPayload(value: unknown, prefix = '', rows = new Map<string, unknown>()) {
  if (value === null || value === undefined || typeof value !== 'object') {
    rows.set(prefix || 'value', value)
    return rows
  }

  if (Array.isArray(value)) {
    if (value.length === 0) {
      rows.set(prefix || 'value', [])
      return rows
    }
    value.forEach((item, index) => {
      const path = prefix ? `${prefix}[${index}]` : `[${index}]`
      flattenPayload(item, path, rows)
    })
    return rows
  }

  const entries = Object.entries(value as Record<string, unknown>)
  if (entries.length === 0) {
    rows.set(prefix || 'value', {})
    return rows
  }
  entries.forEach(([key, child]) => {
    const path = prefix ? `${prefix}.${key}` : key
    flattenPayload(child, path, rows)
  })
  return rows
}

function buildDiffRows(left: Record<string, unknown> | null, right: Record<string, unknown> | null): DiffRow[] {
  const leftMap = flattenPayload(left)
  const rightMap = flattenPayload(right)
  const paths = Array.from(new Set([...leftMap.keys(), ...rightMap.keys()])).sort()

  return paths.map((path) => {
    const leftValue = leftMap.get(path)
    const rightValue = rightMap.get(path)
    let kind: DiffKind = 'unchanged'
    if (!leftMap.has(path)) {
      kind = 'added'
    } else if (!rightMap.has(path)) {
      kind = 'removed'
    } else if (JSON.stringify(leftValue) !== JSON.stringify(rightValue)) {
      kind = 'changed'
    }
    return {
      path,
      kind,
      left: formatDiffValue(leftValue),
      right: formatDiffValue(rightValue),
    }
  })
}

function JsonBlock({ value }: { value: Record<string, unknown> | null }) {
  if (!value || Object.keys(value).length === 0) {
    return <p className="text-xs text-muted-foreground">No payload recorded.</p>
  }

  return (
    <pre className="overflow-x-auto rounded-lg border border-border/60 bg-muted/20 p-3 text-[11px] text-muted-foreground">
      {JSON.stringify(value, null, 2)}
    </pre>
  )
}

function JsonDiffView({
  leftLabel,
  rightLabel,
  leftPayload,
  rightPayload,
}: {
  leftLabel: string
  rightLabel: string
  leftPayload: Record<string, unknown> | null
  rightPayload: Record<string, unknown> | null
}) {
  const rows = React.useMemo(() => buildDiffRows(leftPayload, rightPayload), [leftPayload, rightPayload])
  const changedRows = rows.filter((row) => row.kind !== 'unchanged')

  const badgeClass = (kind: DiffKind) => {
    switch (kind) {
      case 'changed':
        return 'border-amber-500/25 bg-amber-500/10 text-amber-300'
      case 'added':
        return 'border-emerald-500/25 bg-emerald-500/10 text-emerald-300'
      case 'removed':
        return 'border-red-500/25 bg-red-500/10 text-red-300'
      default:
        return 'border-border/60 bg-muted/10 text-muted-foreground'
    }
  }

  return (
    <div className="space-y-3 rounded-xl border border-border/60 bg-card/30 p-4">
      <div className="flex items-center justify-between gap-3">
        <div>
          <p className="text-xs uppercase tracking-[0.16em] text-muted-foreground">JSON Diff</p>
          <p className="mt-1 text-[11px] text-muted-foreground">
            Highlighting changed payload paths between {leftLabel} and {rightLabel}.
          </p>
        </div>
        <div className="flex items-center gap-2 text-[11px]">
          <span className={cn('rounded-full border px-2 py-0.5', badgeClass('changed'))}>
            {rows.filter((row) => row.kind === 'changed').length} changed
          </span>
          <span className={cn('rounded-full border px-2 py-0.5', badgeClass('added'))}>
            {rows.filter((row) => row.kind === 'added').length} added
          </span>
          <span className={cn('rounded-full border px-2 py-0.5', badgeClass('removed'))}>
            {rows.filter((row) => row.kind === 'removed').length} removed
          </span>
        </div>
      </div>

      {changedRows.length === 0 ? (
        <div className="rounded-lg border border-border/50 bg-muted/10 px-3 py-3 text-sm text-muted-foreground">
          No payload differences were recorded between these two events.
        </div>
      ) : (
        <div className="space-y-2">
          {changedRows.map((row) => (
            <div key={row.path} className="rounded-lg border border-border/50 bg-muted/10 px-3 py-3">
              <div className="flex items-center justify-between gap-3">
                <p className="font-mono text-[11px] text-foreground">{row.path}</p>
                <span className={cn('rounded-full border px-2 py-0.5 text-[10px] uppercase tracking-[0.12em]', badgeClass(row.kind))}>
                  {row.kind}
                </span>
              </div>
              <div className="mt-2 grid gap-2 lg:grid-cols-2">
                <div className="rounded-md border border-border/40 bg-card/40 px-2.5 py-2">
                  <p className="text-[10px] uppercase tracking-[0.12em] text-muted-foreground">{leftLabel}</p>
                  <p className="mt-1 break-all font-mono text-[11px] text-foreground">{row.left}</p>
                </div>
                <div className="rounded-md border border-border/40 bg-card/40 px-2.5 py-2">
                  <p className="text-[10px] uppercase tracking-[0.12em] text-muted-foreground">{rightLabel}</p>
                  <p className="mt-1 break-all font-mono text-[11px] text-foreground">{row.right}</p>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function DetailRow({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-border/50 px-3 py-2">
      <p className="text-[11px] uppercase tracking-[0.16em] text-muted-foreground">{label}</p>
      <div className="mt-1 text-sm text-foreground">{value}</div>
    </div>
  )
}

function formatMs(value: number | null): string {
  if (value === null) return '—'
  if (value >= 1000) return `${(value / 1000).toFixed(2)}s`
  return `${value}ms`
}

export function OrderDetailDialog({ open, onClose, order, loading = false }: OrderDetailDialogProps) {
  const [expandedEventIds, setExpandedEventIds] = React.useState<string[]>([])
  const [baselineTargetId, setBaselineTargetId] = React.useState<string | null>(null)
  const [compareTargetId, setCompareTargetId] = React.useState<string | null>(null)

  React.useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && open) onClose()
    }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [open, onClose])

  React.useEffect(() => {
    React.startTransition(() => {
      setExpandedEventIds([])
      setBaselineTargetId(null)
      setCompareTargetId(null)
    })
  }, [order?.id, open])

  const compareCandidates = React.useMemo<CompareCandidate[]>(() => {
    if (!order) return []

    const signalSnapshot = order.signal_snapshot
      ? {
          ticker: order.signal_snapshot.ticker,
          side: order.signal_snapshot.side,
          signal_type: order.signal_snapshot.signal_type,
          status: order.signal_snapshot.status,
          entry_price: order.signal_snapshot.entry_price,
          stop_price: order.signal_snapshot.stop_price,
          take_profit_price: order.signal_snapshot.take_profit_price,
          suggested_quantity: order.signal_snapshot.suggested_quantity,
          confidence: order.signal_snapshot.confidence,
          reason: order.signal_snapshot.reason,
          risk_rejected: order.signal_snapshot.risk_rejected,
          risk_rejection_reason: order.signal_snapshot.risk_rejection_reason,
          generated_at: order.signal_snapshot.generated_at,
          expires_at: order.signal_snapshot.expires_at,
          executed_at: order.signal_snapshot.executed_at,
        }
      : null

    const orderSnapshot = {
      ticker: order.ticker,
      side: order.side,
      order_type: order.order_type,
      quantity: order.quantity,
      status: order.status,
      limit_price: order.limit_price,
      stop_price: order.stop_price,
      time_validity: order.time_validity,
      broker_order_id: order.broker_order_id,
      filled_quantity: order.filled_quantity,
      avg_fill_price: order.avg_fill_price,
      expected_fill_price: order.expected_fill_price,
      slippage_pct: order.slippage_pct,
      slippage_value: order.slippage_value,
      submitted_at: order.submitted_at,
      first_ack_at: order.first_ack_at,
      filled_at: order.filled_at,
      broker_latency_ms: order.broker_latency_ms,
      fill_latency_ms: order.fill_latency_ms,
      reconciliation_latency_ms: order.reconciliation_latency_ms,
      execution_quality_score: order.execution_quality_score,
      execution_quality_grade: order.execution_quality_grade,
      cash_used: order.cash_used,
      retry_count: order.retry_count,
      error_message: order.error_message,
      last_reconciled_at: order.last_reconciled_at,
      created_at: order.created_at,
      updated_at: order.updated_at,
    }

    return [
      signalSnapshot
        ? {
            id: 'signal-snapshot',
            label: 'Signal snapshot',
            subtitle: order.signal_snapshot?.generated_at ? `Generated ${formatDate(order.signal_snapshot.generated_at)}` : 'Strategy decision context',
            payload: signalSnapshot,
            sourceType: 'signal',
          }
        : null,
      {
        id: 'broker-request',
        label: 'Broker request',
        subtitle: 'Submission payload sent to the broker adapter',
        payload: order.broker_request,
        sourceType: 'request',
      },
      {
        id: 'broker-response',
        label: 'Broker response',
        subtitle: 'Latest broker acknowledgement payload',
        payload: order.broker_response,
        sourceType: 'response',
      },
      {
        id: 'final-order-snapshot',
        label: 'Final order snapshot',
        subtitle: 'Current normalized order state in CashGuard',
        payload: orderSnapshot,
        sourceType: 'order',
      },
      ...order.events.map((event) => ({
        id: `event:${event.id}`,
        label: event.event_type.replace(/_/g, ' '),
        subtitle: `${event.from_status ? event.from_status.replace(/_/g, ' ') : '—'} → ${event.to_status ? event.to_status.replace(/_/g, ' ') : '—'}`,
        payload: event.payload,
        sourceType: 'event' as const,
      })),
    ].filter((candidate): candidate is CompareCandidate => candidate !== null && (candidate.payload === null || typeof candidate.payload === 'object'))
  }, [order])

  const baselineCandidate = React.useMemo(
    () => compareCandidates.find((candidate) => candidate.id === baselineTargetId) ?? null,
    [compareCandidates, baselineTargetId],
  )

  const activeCompareCandidate = React.useMemo(
    () => compareCandidates.find((candidate) => candidate.id === compareTargetId) ?? null,
    [compareCandidates, compareTargetId],
  )

  const displayedCompareCandidates = React.useMemo<CompareCandidate[]>(
    () => [baselineCandidate, activeCompareCandidate].filter(
      (candidate): candidate is CompareCandidate => candidate !== null,
    ),
    [baselineCandidate, activeCompareCandidate],
  )

  const toggleExpanded = (eventId: string) => {
    setExpandedEventIds((current) =>
      current.includes(eventId)
        ? current.filter((id) => id !== eventId)
        : [...current, eventId],
    )
  }

  const pinBaseline = (candidateId: string) => {
    setBaselineTargetId((current) => {
      if (current === candidateId) {
        if (compareTargetId === candidateId) {
          setCompareTargetId(null)
        }
        return null
      }
      if (compareTargetId === candidateId) {
        setCompareTargetId(current)
      }
      return candidateId
    })
  }

  const selectCompareTarget = (candidateId: string) => {
    if (baselineTargetId === candidateId) {
      return
    }
    setCompareTargetId((current) => (current === candidateId ? null : candidateId))
  }

  if (!open) return null

  return (
    <div className="fixed inset-0 z-50 flex justify-end">
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" onClick={onClose} />
      <Card className="relative z-10 flex h-full w-full max-w-2xl flex-col rounded-none border-l border-border animate-slide-up">
        <div className="flex items-start justify-between gap-4 border-b border-border/60 px-5 py-4">
          <div>
            <p className="text-xs uppercase tracking-[0.18em] text-muted-foreground">Order Inspector</p>
            <h2 className="mt-1 text-lg font-semibold text-foreground">
              {order?.ticker ?? 'Loading order'}
              {order?.strategy_name ? ` · ${order.strategy_name}` : ''}
            </h2>
            <p className="mt-1 text-sm text-muted-foreground">
              Inspect broker payloads, signal context, and reconciliation history without leaving the orders page.
            </p>
          </div>
          <Button variant="ghost" size="icon-sm" onClick={onClose}>
            <X className="h-4 w-4" />
          </Button>
        </div>

        <div className="flex-1 space-y-5 overflow-y-auto px-5 py-5">
          {loading || !order ? (
            <div className="rounded-xl border border-border/60 bg-muted/15 px-4 py-6 text-sm text-muted-foreground">
              Loading order details…
            </div>
          ) : (
            <>
              <div className="grid gap-3 md:grid-cols-2">
                <DetailRow label="Status" value={(
                  <div className="space-y-1">
                    <span className={cn('inline-flex items-center text-[10px] px-2 py-0.5 rounded-full font-semibold uppercase tracking-wider', orderStatusBg(order.status))}>
                      {order.status.replace(/_/g, ' ')}
                    </span>
                    {order.last_reconciled_at && (
                      <p className="text-xs text-muted-foreground">Last reconciled {formatDate(order.last_reconciled_at)}</p>
                    )}
                  </div>
                )} />
                <DetailRow label="Strategy Context" value={(
                  <div className="space-y-1">
                    <p>{order.strategy_name ?? 'Manual / direct broker order'}</p>
                    {order.strategy_type_name && (
                      <p className="text-xs text-muted-foreground capitalize">{order.strategy_type_name.replace(/_/g, ' ')}</p>
                    )}
                  </div>
                )} />
                <DetailRow label="Execution" value={(
                  <div className="space-y-1">
                    <p className="capitalize">{order.side} · {order.order_type.replace(/_/g, ' ')}</p>
                    <p className="text-xs text-muted-foreground">
                      Qty {order.quantity}
                      {order.avg_fill_price ? ` · Avg fill ${formatCurrency(Number(order.avg_fill_price))}` : ''}
                    </p>
                  </div>
                )} />
                <DetailRow label="Risk / Confidence" value={(
                  <div className="space-y-1">
                    {order.signal_risk_rejected ? (
                      <Badge variant="destructive">Risk rejected</Badge>
                    ) : order.signal_confidence ? (
                      <p>{Math.round(Number(order.signal_confidence) * 100)}% linked signal confidence</p>
                    ) : (
                      <p className="text-muted-foreground">No linked signal score recorded.</p>
                    )}
                    {order.signal_risk_rejection_reason && (
                      <p className="text-xs text-muted-foreground">{order.signal_risk_rejection_reason}</p>
                    )}
                  </div>
                )} />
                <DetailRow label="Pricing Controls" value={(
                  <div className="space-y-1 text-xs text-muted-foreground">
                    <p>Limit: {order.limit_price ? formatCurrency(Number(order.limit_price)) : '—'}</p>
                    <p>Stop: {order.stop_price ? formatCurrency(Number(order.stop_price)) : '—'}</p>
                    <p>Time validity: {order.time_validity}</p>
                  </div>
                )} />
                <DetailRow label="Broker / Mode" value={(
                  <div className="space-y-1 text-xs text-muted-foreground">
                    <p>{order.is_dry_run ? 'Dry-run simulation' : `${order.execution_environment ?? 'broker'} broker order`}</p>
                    <p>Broker order id: {order.broker_order_id ?? 'Not assigned'}</p>
                    <p>Retry count: {order.retry_count}</p>
                  </div>
                )} />
                <DetailRow label="Execution Quality" value={(
                  <div className="space-y-1">
                    {order.execution_quality_score ? (
                      <div className="flex items-center gap-2">
                        <span className={cn('inline-flex items-center text-[10px] px-2 py-0.5 rounded-full font-semibold uppercase tracking-wider', executionQualityBadge(order.execution_quality_grade))}>
                          {Number(order.execution_quality_score).toFixed(0)}
                        </span>
                        <span className={cn('text-xs capitalize', executionQualityClass(order.execution_quality_grade))}>
                          {order.execution_quality_grade ?? 'pending'}
                        </span>
                      </div>
                    ) : (
                      <p className="text-muted-foreground">Pending final execution metrics.</p>
                    )}
                    <p className="text-xs text-muted-foreground">
                      Expected {order.expected_fill_price ? formatCurrency(Number(order.expected_fill_price)) : '—'}
                      {order.slippage_pct !== null ? ` · Slip ${Number(order.slippage_pct).toFixed(3)}%` : ''}
                    </p>
                  </div>
                )} />
                <DetailRow label="Timing" value={(
                  <div className="space-y-1 text-xs text-muted-foreground">
                    <p>First ack: {formatMs(order.broker_latency_ms)}</p>
                    <p>Fill: {formatMs(order.fill_latency_ms)}</p>
                    <p>Reconciliation: {formatMs(order.reconciliation_latency_ms)}</p>
                  </div>
                )} />
              </div>

              {(order.signal_reason || order.error_message) && (
                <div className="rounded-xl border border-border/60 bg-muted/15 p-4">
                  <p className="text-xs uppercase tracking-[0.16em] text-muted-foreground">Decision Notes</p>
                  {order.signal_reason && (
                    <p className="mt-2 text-sm text-foreground">{order.signal_reason}</p>
                  )}
                  {order.error_message && (
                    <div className="mt-3 flex items-start gap-2 rounded-lg border border-red-500/25 bg-red-500/10 px-3 py-2 text-sm text-red-300">
                      <AlertTriangle className="mt-0.5 h-4 w-4 flex-shrink-0" />
                      <p>{order.error_message}</p>
                    </div>
                  )}
                </div>
              )}

              <div className="grid gap-4 lg:grid-cols-2">
                <div className="space-y-2">
                  <p className="text-xs uppercase tracking-[0.16em] text-muted-foreground">Broker Request</p>
                  <JsonBlock value={order.broker_request} />
                </div>
                <div className="space-y-2">
                  <p className="text-xs uppercase tracking-[0.16em] text-muted-foreground">Broker Response</p>
                  <JsonBlock value={order.broker_response} />
                </div>
              </div>

              <div className="space-y-2">
                <p className="text-xs uppercase tracking-[0.16em] text-muted-foreground">Execution Quality Notes</p>
                <JsonBlock value={order.execution_quality_notes} />
              </div>

              <div className="space-y-3">
                <div className="flex items-center justify-between gap-3">
                  <p className="text-xs uppercase tracking-[0.16em] text-muted-foreground">Reconciliation Timeline</p>
                  <p className="text-[11px] text-muted-foreground">
                    Pin one lifecycle object as the baseline, then click another to compare against it.
                  </p>
                </div>
                <div className="grid gap-2 md:grid-cols-2">
                  {compareCandidates.map((candidate) => (
                    <div
                      key={candidate.id}
                      className={cn(
                        'rounded-xl border px-3 py-3 transition-colors',
                        baselineTargetId === candidate.id
                          ? 'border-amber-500/35 bg-amber-500/10'
                          : compareTargetId === candidate.id
                            ? 'border-primary/40 bg-primary/10'
                            : 'border-border/60 bg-muted/10 hover:bg-muted/20',
                      )}
                    >
                      <div className="flex items-start justify-between gap-3">
                        <div className="min-w-0 flex-1">
                          <button
                            type="button"
                            onClick={() => selectCompareTarget(candidate.id)}
                            className="w-full text-left"
                          >
                            <p className="text-sm font-medium capitalize text-foreground">{candidate.label}</p>
                            <p className="mt-1 text-xs text-muted-foreground">{candidate.subtitle}</p>
                          </button>
                        </div>
                        <div className="flex flex-col items-end gap-2">
                          <span className={cn(
                            'rounded-full border px-2 py-0.5 text-[10px] uppercase tracking-[0.12em]',
                            baselineTargetId === candidate.id
                              ? 'border-amber-500/30 bg-amber-500/10 text-amber-300'
                              : compareTargetId === candidate.id
                                ? 'border-primary/30 bg-primary/10 text-primary'
                                : 'border-border/60 bg-card/40 text-muted-foreground',
                          )}>
                            {baselineTargetId === candidate.id ? 'baseline' : compareTargetId === candidate.id ? 'compare' : candidate.sourceType}
                          </span>
                          <Button
                            type="button"
                            variant="ghost"
                            size="xs"
                            className={cn(
                              'gap-1.5',
                              baselineTargetId === candidate.id && 'text-amber-300',
                            )}
                            onClick={() => pinBaseline(candidate.id)}
                          >
                            {baselineTargetId === candidate.id ? (
                              <CheckSquare className="h-3.5 w-3.5" />
                            ) : (
                              <Pin className="h-3.5 w-3.5" />
                            )}
                            {baselineTargetId === candidate.id ? 'Pinned' : 'Pin'}
                          </Button>
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
                {(baselineCandidate || activeCompareCandidate) && (
                  <div className="rounded-xl border border-border/60 bg-muted/10 p-4">
                    <div className="flex items-center justify-between gap-3">
                      <p className="text-xs uppercase tracking-[0.16em] text-muted-foreground">Lifecycle Compare</p>
                      {!baselineCandidate ? (
                        <p className="text-[11px] text-muted-foreground">Pin a baseline object to start a reusable comparison workflow.</p>
                      ) : !activeCompareCandidate ? (
                        <p className="text-[11px] text-muted-foreground">Baseline is pinned. Click another object to compare against it.</p>
                      ) : (
                        <p className="text-[11px] text-muted-foreground">Baseline stays pinned while you switch the compare target.</p>
                      )}
                    </div>
                    <div className={cn('mt-3 grid gap-3', baselineCandidate && activeCompareCandidate ? 'lg:grid-cols-2' : 'grid-cols-1')}>
                      {displayedCompareCandidates.map((candidate) => (
                        <div key={`compare-${candidate.id}`} className="space-y-2 rounded-xl border border-border/50 bg-card/40 p-3">
                          <div className="flex items-start justify-between gap-3">
                            <div>
                              <p className="text-sm font-medium text-foreground capitalize">
                                {candidate.label}
                              </p>
                              <p className="mt-1 text-xs text-muted-foreground">{candidate.subtitle}</p>
                            </div>
                            <span className={cn(
                              'rounded-full border px-2 py-0.5 text-[10px] uppercase tracking-[0.12em]',
                              baselineCandidate?.id === candidate.id
                                ? 'border-amber-500/30 bg-amber-500/10 text-amber-300'
                                : 'border-primary/30 bg-primary/10 text-primary',
                            )}>
                              {baselineCandidate?.id === candidate.id ? 'baseline' : 'compare'}
                            </span>
                          </div>
                          <JsonBlock value={candidate.payload} />
                        </div>
                      ))}
                    </div>
                    {baselineCandidate && activeCompareCandidate && (
                      <div className="mt-4">
                        <JsonDiffView
                          leftLabel={baselineCandidate.label}
                          rightLabel={activeCompareCandidate.label}
                          leftPayload={baselineCandidate.payload}
                          rightPayload={activeCompareCandidate.payload}
                        />
                      </div>
                    )}
                  </div>
                )}
                {order.events.length === 0 ? (
                  <div className="rounded-xl border border-border/60 bg-muted/15 px-4 py-4 text-sm text-muted-foreground">
                    No order events recorded yet.
                  </div>
                ) : (
                  <div className="space-y-3">
                    {order.events.map((event) => (
                      <div key={event.id} className="rounded-xl border border-border/60 bg-muted/10">
                        <div className="flex items-stretch">
                          <button
                            type="button"
                            className="flex min-w-0 flex-1 items-start justify-between gap-3 px-4 py-4 text-left"
                            onClick={() => toggleExpanded(event.id)}
                          >
                            <div className="flex min-w-0 items-start gap-3">
                              <div className="mt-0.5 rounded-full border border-border/60 bg-card p-2">
                                <Clock3 className="h-3.5 w-3.5 text-muted-foreground" />
                              </div>
                              <div className="min-w-0">
                                <div className="flex items-center gap-2">
                                  {expandedEventIds.includes(event.id) ? (
                                    <ChevronDown className="h-3.5 w-3.5 text-muted-foreground" />
                                  ) : (
                                    <ChevronRight className="h-3.5 w-3.5 text-muted-foreground" />
                                  )}
                                  <p className="text-sm font-medium text-foreground capitalize">
                                    {event.event_type.replace(/_/g, ' ')}
                                  </p>
                                </div>
                                <p className="mt-1 text-xs text-muted-foreground">
                                  {event.from_status ? event.from_status.replace(/_/g, ' ') : '—'}
                                  {' → '}
                                  {event.to_status ? event.to_status.replace(/_/g, ' ') : '—'}
                                </p>
                              </div>
                            </div>
                            <p className="pt-0.5 text-xs text-muted-foreground">{formatDate(event.occurred_at)}</p>
                          </button>
                          <div className="flex items-center pr-4">
                            <Button
                              type="button"
                              variant="ghost"
                              size="sm"
                              className={cn(
                                'gap-1.5 text-[11px]',
                                compareTargetId === `event:${event.id}` && 'text-primary',
                              )}
                              onClick={() => selectCompareTarget(`event:${event.id}`)}
                            >
                              {compareTargetId === `event:${event.id}` ? (
                                <CheckSquare className="h-3.5 w-3.5" />
                              ) : (
                                <Square className="h-3.5 w-3.5" />
                              )}
                              Compare
                            </Button>
                          </div>
                        </div>
                        {expandedEventIds.includes(event.id) && (
                          <div className="border-t border-border/50 px-4 py-4">
                            <div className="grid gap-3 md:grid-cols-2">
                              <DetailRow
                                label="Transition"
                                value={(
                                  <div className="space-y-1 text-xs text-muted-foreground">
                                    <p>From: {event.from_status ? event.from_status.replace(/_/g, ' ') : '—'}</p>
                                    <p>To: {event.to_status ? event.to_status.replace(/_/g, ' ') : '—'}</p>
                                  </div>
                                )}
                              />
                              <DetailRow
                                label="Occurred"
                                value={<span className="text-xs text-muted-foreground">{formatDate(event.occurred_at)}</span>}
                              />
                            </div>
                            <div className="mt-3">
                              <JsonBlock value={event.payload} />
                            </div>
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </>
          )}
        </div>
      </Card>
    </div>
  )
}
