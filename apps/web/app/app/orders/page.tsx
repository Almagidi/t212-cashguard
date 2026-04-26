'use client'
import { useState } from 'react'
import { Eye, RefreshCw, X, ClipboardList } from 'lucide-react'
import { useOrder, useOrders, useCancelOrder, useCancelAllPending } from '@/hooks/use-api'
import { Button, Card, CardContent, Badge, Spinner, EmptyState, PageHeader } from '@/components/ui'
import { QueryError } from '@/components/shared/query-error'
import { ConfirmDialog } from '@/components/shared/confirm-dialog'
import { OrderDetailDialog } from '@/components/orders/order-detail-dialog'
import { executionQualityBadge, formatCurrency, formatDate, orderStatusBg, cn } from '@/lib/utils'
import { useQueryClient } from '@tanstack/react-query'
import type { Order } from '@/types'

const TABS = ['all', 'pending', 'filled', 'cancelled'] as const
type Tab = typeof TABS[number]

function statusMatchesTab(status: string, tab: Tab): boolean {
  if (tab === 'all') return true
  if (tab === 'pending') return ['pending_intent', 'submitted', 'accepted'].includes(status)
  if (tab === 'filled') return status === 'filled'
  if (tab === 'cancelled') return ['cancelled', 'rejected', 'error'].includes(status)
  return true
}

export default function OrdersPage() {
  const qc = useQueryClient()
  const [tab, setTab] = useState<Tab>('all')
  const [cancelTarget, setCancelTarget] = useState<string | null>(null)
  const [showCancelAll, setShowCancelAll] = useState(false)
  const [selectedOrderId, setSelectedOrderId] = useState<string | null>(null)
  const { data: allOrders = [], isLoading, isError, error, refetch } = useOrders({ limit: 200 })
  const { data: selectedOrder, isLoading: loadingSelectedOrder } = useOrder(selectedOrderId, { enabled: Boolean(selectedOrderId) })
  const cancelOne = useCancelOrder()
  const cancelAll = useCancelAllPending()

  const orders = allOrders.filter(o => statusMatchesTab(o.status, tab))
  const pendingCount = allOrders.filter(o => ['pending_intent', 'submitted', 'accepted'].includes(o.status)).length

  return (
    <div className="space-y-5">
      <PageHeader
        icon={<ClipboardList className="h-5 w-5" />}
        label="Orders"
        sub={<>
          {allOrders.length} total
          {pendingCount > 0 && <> · <span className="text-amber-400 font-medium">{pendingCount} pending</span></>}
        </>}
        actions={
          <div className="flex gap-2">
            <Button variant="outline" size="sm" onClick={() => qc.invalidateQueries({ queryKey: ['orders'] })}>
              <RefreshCw className="w-3.5 h-3.5" />
              Refresh
            </Button>
            {pendingCount > 0 && (
              <Button variant="danger" size="sm" onClick={() => setShowCancelAll(true)}>
                <X className="w-3.5 h-3.5" />
                Cancel All ({pendingCount})
              </Button>
            )}
          </div>
        }
      />

      {/* Tabs */}
      <div className="inline-flex gap-0.5 p-1 bg-muted/40 border border-border rounded-lg">
        {TABS.map(t => {
          const isActive = tab === t
          return (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={cn(
                'inline-flex items-center gap-1.5 px-3.5 py-1.5 rounded-md text-xs font-medium transition-all capitalize',
                isActive
                  ? 'bg-card text-foreground shadow-sm border border-border/60'
                  : 'text-muted-foreground hover:text-foreground hover:bg-card/40'
              )}
            >
              {t}
              {t === 'pending' && pendingCount > 0 && (
                <span className={cn(
                  'ml-0.5 px-1.5 py-0.5 rounded-full text-[9px] font-bold tabular-nums',
                  isActive ? 'bg-amber-500/20 text-amber-400' : 'bg-muted text-muted-foreground'
                )}>
                  {pendingCount}
                </span>
              )}
            </button>
          )
        })}
      </div>

      {isLoading ? (
        <Card>
          <CardContent className="flex items-center gap-2 text-muted-foreground text-sm py-12 justify-center">
            <Spinner className="w-4 h-4" /> Loading orders…
          </CardContent>
        </Card>
      ) : isError ? (
        <Card>
          <CardContent className="p-0">
            <QueryError error={error} onRetry={refetch} label="orders" />
          </CardContent>
        </Card>
      ) : orders.length === 0 ? (
        <Card>
          <CardContent className="p-0">
            <EmptyState
              title="No orders"
              description="Orders placed by strategies or manually will appear here."
            />
          </CardContent>
        </Card>
      ) : (
        <Card>
          <CardContent className="p-0">
            <div className="overflow-x-auto scrollbar-none">
              <table className="w-full data-table min-w-[920px]">
                <thead>
                  <tr>
                    <th className="sticky left-0 bg-card z-10">Symbol</th>
                    <th>Side</th>
                    <th className="hidden sm:table-cell">Type</th>
                    <th className="text-right">Qty</th>
                    <th className="text-right">Fill Price</th>
                    <th className="text-right hidden lg:table-cell">Slippage</th>
                    <th className="hidden md:table-cell">Exec Score</th>
                    <th>Status</th>
                    <th className="text-right hidden md:table-cell">Cash Used</th>
                    <th className="hidden md:table-cell">Mode</th>
                    <th className="hidden sm:table-cell">Time</th>
                    <th className="w-[1%]"></th>
                  </tr>
                </thead>
                <tbody>
                  {orders.map(o => (
                    <tr key={o.id}>
                      <td className="sticky left-0 bg-card">
                        <div>
                          <p className="font-semibold text-foreground">{o.ticker}</p>
                          {(o.strategy_name || o.signal_reason || o.signal_risk_rejection_reason) && (
                            <p className="mt-0.5 text-[10px] text-muted-foreground">
                              {o.strategy_name ? `${o.strategy_name}` : 'Manual order'}
                              {o.strategy_type_name ? ` · ${o.strategy_type_name.replace(/_/g, ' ')}` : ''}
                            </p>
                          )}
                          {(o.signal_risk_rejection_reason ?? o.error_message ?? o.signal_reason) && (
                            <p className="mt-1 max-w-[18rem] text-[10px] text-muted-foreground line-clamp-2">
                              {o.signal_risk_rejection_reason ?? o.error_message ?? o.signal_reason}
                            </p>
                          )}
                        </div>
                      </td>
                      <td>
                        <span className={cn(
                          'inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-[10px] font-bold uppercase tracking-wider border',
                          o.side === 'buy'
                            ? 'text-emerald-400 bg-emerald-500/10 border-emerald-500/20'
                            : 'text-red-400 bg-red-500/10 border-red-500/20'
                        )}>
                          {o.side}
                        </span>
                      </td>
                      <td className="text-muted-foreground capitalize text-xs hidden sm:table-cell">{o.order_type}</td>
                      <td className="tnum text-right">{o.quantity}</td>
                      <td className="tnum text-right">{o.avg_fill_price ? formatCurrency(Number(o.avg_fill_price)) : <span className="text-muted-foreground/50">—</span>}</td>
                      <td className="tnum text-right hidden lg:table-cell">
                        {o.slippage_pct !== null ? (
                          <span className={Number(o.slippage_pct) > 0 ? 'text-red-400' : 'text-emerald-400'}>
                            {Number(o.slippage_pct).toFixed(3)}%
                          </span>
                        ) : (
                          <span className="text-muted-foreground/50">—</span>
                        )}
                      </td>
                      <td className="hidden md:table-cell">
                        {o.execution_quality_score !== null ? (
                          <span className={cn('inline-flex items-center text-[10px] px-2 py-0.5 rounded-full font-semibold uppercase tracking-wider', executionQualityBadge(o.execution_quality_grade))}>
                            {Number(o.execution_quality_score).toFixed(0)}
                          </span>
                        ) : (
                          <span className="text-[10px] text-muted-foreground">pending</span>
                        )}
                      </td>
                      <td>
                        <div className="space-y-1">
                          <span className={cn('inline-flex items-center text-[10px] px-2 py-0.5 rounded-full font-semibold uppercase tracking-wider', orderStatusBg(o.status))}>
                            {o.status.replace('_', ' ')}
                          </span>
                          {(o.signal_confidence || o.signal_risk_rejected) && (
                            <p className="text-[10px] text-muted-foreground">
                              {o.signal_risk_rejected
                                ? 'Risk blocked before execution'
                                : o.signal_confidence
                                  ? `${Math.round(Number(o.signal_confidence) * 100)}% signal confidence`
                                  : 'Linked signal'}
                            </p>
                          )}
                        </div>
                      </td>
                      <td className="tnum text-right text-muted-foreground hidden md:table-cell">
                        {o.cash_used ? formatCurrency(Number(o.cash_used)) : <span className="text-muted-foreground/50">—</span>}
                      </td>
                      <td className="hidden md:table-cell">
                        {o.is_dry_run ? (
                          <span className="text-[10px] font-medium px-1.5 py-0.5 rounded bg-purple-500/10 border border-purple-500/20 text-purple-400 uppercase tracking-wider">
                            dry
                          </span>
                        ) : o.execution_environment === 'demo' ? (
                          <span className="text-[10px] font-medium px-1.5 py-0.5 rounded bg-sky-500/10 border border-sky-500/20 text-sky-400 uppercase tracking-wider">
                            demo
                          </span>
                        ) : o.execution_environment === 'live' ? (
                          <span className="text-[10px] font-medium px-1.5 py-0.5 rounded bg-blue-500/10 border border-blue-500/20 text-blue-400 uppercase tracking-wider">
                            live
                          </span>
                        ) : (
                          <span className="text-[10px] font-medium px-1.5 py-0.5 rounded bg-slate-500/10 border border-slate-500/20 text-slate-300 uppercase tracking-wider">
                            {o.execution_environment ?? 'broker'}
                          </span>
                        )}
                      </td>
                      <td className="text-muted-foreground text-xs whitespace-nowrap hidden sm:table-cell">{formatDate(o.created_at)}</td>
                      <td>
                        <Button
                          variant="ghost"
                          size="icon-sm"
                          className="text-muted-foreground hover:text-foreground"
                          onClick={() => setSelectedOrderId(o.id)}
                          title="Inspect order"
                        >
                          <Eye className="w-3.5 h-3.5" />
                        </Button>
                        {['pending_intent', 'submitted', 'accepted'].includes(o.status) && (
                          <Button
                            variant="ghost"
                            size="icon-sm"
                            className="text-muted-foreground hover:text-red-400 hover:bg-red-500/10"
                            onClick={() => setCancelTarget(o.id)}
                          >
                            <X className="w-3.5 h-3.5" />
                          </Button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </CardContent>
        </Card>
      )}

      <ConfirmDialog
        open={!!cancelTarget}
        onClose={() => setCancelTarget(null)}
        onConfirm={() => { cancelOne.mutate(cancelTarget!); setCancelTarget(null) }}
        title="Cancel this order?"
        description="The order will be cancelled at the broker if it is still pending."
        confirmLabel="Cancel Order"
        dangerous
        loading={cancelOne.isPending}
      />

      <ConfirmDialog
        open={showCancelAll}
        onClose={() => setShowCancelAll(false)}
        onConfirm={() => { cancelAll.mutate(); setShowCancelAll(false) }}
        title={`Cancel all ${pendingCount} pending orders?`}
        description="All pending and working orders will be cancelled at the broker. This cannot be undone."
        confirmLabel="Cancel All"
        dangerous
        loading={cancelAll.isPending}
      />

      <OrderDetailDialog
        open={!!selectedOrderId}
        onClose={() => setSelectedOrderId(null)}
        order={selectedOrder ?? null}
        loading={loadingSelectedOrder}
      />
    </div>
  )
}
