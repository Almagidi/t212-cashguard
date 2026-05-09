'use client'
import { useState } from 'react'
import type { FormEvent } from 'react'
import { AlertTriangle, Eye, RefreshCw, X, ClipboardList, Send, ShieldCheck } from 'lucide-react'
import {
  useOrder,
  useOrders,
  useCancelOrder,
  useCancelAllPending,
  useKillSwitch,
  usePaperExecutionHistory,
  usePlacePaperOrder,
  useSettings,
} from '@/hooks/use-api'
import { Button, Card, CardContent, Badge, Spinner, EmptyState, PageHeader, Input, Label } from '@/components/ui'
import { QueryError } from '@/components/shared/query-error'
import { ConfirmDialog } from '@/components/shared/confirm-dialog'
import { OrderDetailDialog } from '@/components/orders/order-detail-dialog'
import { executionQualityBadge, formatCurrency, formatDate, orderStatusBg, cn } from '@/lib/utils'
import { useQueryClient } from '@tanstack/react-query'
import type { OrderSide } from '@/types'

const TABS = ['all', 'pending', 'filled', 'cancelled'] as const
type Tab = typeof TABS[number]

function statusMatchesTab(status: string, tab: Tab): boolean {
  if (tab === 'all') return true
  if (tab === 'pending') return ['pending_intent', 'submitted', 'accepted'].includes(status)
  if (tab === 'filled') return status === 'filled'
  if (tab === 'cancelled') return ['cancelled', 'rejected', 'error'].includes(status)
  return true
}

function extractMutationMessage(error: unknown, fallback: string): string {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  if (typeof detail === 'string' && detail.trim()) return detail
  if (
    detail &&
    typeof detail === 'object' &&
    typeof (detail as { message?: unknown }).message === 'string'
  ) {
    return (detail as { message: string }).message
  }
  return fallback
}

function hasBackendResponse(error: unknown): boolean {
  return Boolean((error as { response?: unknown })?.response)
}

export default function OrdersPage() {
  const qc = useQueryClient()
  const [tab, setTab] = useState<Tab>('all')
  const [cancelTarget, setCancelTarget] = useState<string | null>(null)
  const [showCancelAll, setShowCancelAll] = useState(false)
  const [selectedOrderId, setSelectedOrderId] = useState<string | null>(null)
  const [paperTicker, setPaperTicker] = useState('AAPL')
  const [paperSide, setPaperSide] = useState<OrderSide>('buy')
  const [paperQuantity, setPaperQuantity] = useState('1')
  const [paperVenue, setPaperVenue] = useState<'paper' | 'mock'>('paper')
  const [paperResult, setPaperResult] = useState<{
    tone: 'success' | 'blocked'
    title: string
    detail: string
    orderId?: string
  } | null>(null)
  const { data: allOrders = [], isLoading, isError, error, refetch } = useOrders({ limit: 200 })
  const { data: selectedOrder, isLoading: loadingSelectedOrder } = useOrder(selectedOrderId, { enabled: Boolean(selectedOrderId) })
  const { data: settings } = useSettings()
  const { data: paperHistory, isLoading: loadingPaperHistory } = usePaperExecutionHistory({ limit: 8 })
  const placePaperOrder = usePlacePaperOrder()
  const killSwitch = useKillSwitch()
  const cancelOne = useCancelOrder()
  const cancelAll = useCancelAllPending()

  const orders = allOrders.filter(o => statusMatchesTab(o.status, tab))
  const pendingCount = allOrders.filter(o => ['pending_intent', 'submitted', 'accepted'].includes(o.status)).length
  const appMode = process.env.NEXT_PUBLIC_APP_MODE || 'mock'
  const isMockMode = appMode === 'mock'

  const submitPaperOrder = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    setPaperResult(null)

    try {
      const order = await placePaperOrder.mutateAsync({
        ticker: paperTicker.trim().toUpperCase(),
        side: paperSide,
        quantity: paperQuantity,
        estimated_price: '100',
        order_type: 'market',
        source: 'manual_demo_ui',
        venue: paperVenue,
        paper_only: true,
      })
      setPaperResult({
        tone: 'success',
        title: 'Paper order filled',
        detail: `${order.side.toUpperCase()} ${order.quantity} ${order.ticker} was simulated locally. No broker order sent.`,
        orderId: order.id,
      })
    } catch (err) {
      const backendResponded = hasBackendResponse(err)
      setPaperResult({
        tone: 'blocked',
        title: backendResponded
          ? 'Paper order blocked by safety controls'
          : 'Paper order could not reach mock backend',
        detail: backendResponded
          ? `${extractMutationMessage(err, 'Safety controls blocked this paper order.')} No broker order was sent.`
          : 'The mock backend did not respond to this paper request. No broker order was sent.',
      })
    }
  }

  const enableKillSwitch = async () => {
    await killSwitch.mutateAsync({ active: true })
  }

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

      <Card className="border-emerald-500/20 bg-emerald-500/[0.03]">
        <CardContent className="pt-5">
          <div className="flex flex-col gap-5 lg:flex-row lg:items-start lg:justify-between">
            <div className="space-y-4 lg:max-w-xl">
              <div className="flex flex-wrap items-center gap-2">
                <h2 className="text-sm font-semibold text-foreground">Paper / Mock Order</h2>
                <Badge variant={isMockMode ? 'success' : 'warning'}>{isMockMode ? 'Mock mode only' : `${appMode} mode`}</Badge>
                {settings?.kill_switch_active && <Badge variant="destructive">Kill switch is active</Badge>}
              </div>
              <div className="grid gap-2 text-xs text-muted-foreground sm:grid-cols-3">
                <div className="flex items-center gap-2 rounded-lg border border-border/70 bg-background/40 px-3 py-2">
                  <ShieldCheck className="h-3.5 w-3.5 text-emerald-400" />
                  <span>Mock mode only</span>
                </div>
                <div className="flex items-center gap-2 rounded-lg border border-border/70 bg-background/40 px-3 py-2">
                  <ShieldCheck className="h-3.5 w-3.5 text-emerald-400" />
                  <span>No real broker order will be placed</span>
                </div>
                <div className="flex items-center gap-2 rounded-lg border border-border/70 bg-background/40 px-3 py-2">
                  <ShieldCheck className="h-3.5 w-3.5 text-emerald-400" />
                  <span>No funds are moved</span>
                </div>
              </div>

              <form onSubmit={submitPaperOrder} className="grid gap-3 sm:grid-cols-[1fr_130px_130px_130px_auto] sm:items-end">
                <div className="space-y-1.5">
                  <Label htmlFor="paper-ticker">Ticker</Label>
                  <Input
                    id="paper-ticker"
                    value={paperTicker}
                    onChange={(event) => setPaperTicker(event.target.value)}
                    required
                    autoCapitalize="characters"
                    maxLength={50}
                  />
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="paper-side">Side</Label>
                  <select
                    id="paper-side"
                    value={paperSide}
                    onChange={(event) => setPaperSide(event.target.value as OrderSide)}
                    className="flex h-9 w-full rounded-lg border border-input bg-background/60 px-3 py-1 text-sm focus-visible:outline-none focus-visible:border-primary/70 focus-visible:ring-2 focus-visible:ring-primary/20"
                  >
                    <option value="buy">buy</option>
                    <option value="sell">sell</option>
                  </select>
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="paper-quantity">Quantity</Label>
                  <Input
                    id="paper-quantity"
                    type="number"
                    min="0.000001"
                    step="0.000001"
                    value={paperQuantity}
                    onChange={(event) => setPaperQuantity(event.target.value)}
                    required
                  />
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="paper-venue">Venue</Label>
                  <select
                    id="paper-venue"
                    value={paperVenue}
                    onChange={(event) => setPaperVenue(event.target.value as 'paper' | 'mock')}
                    className="flex h-9 w-full rounded-lg border border-input bg-background/60 px-3 py-1 text-sm focus-visible:outline-none focus-visible:border-primary/70 focus-visible:ring-2 focus-visible:ring-primary/20"
                  >
                    <option value="paper">paper</option>
                    <option value="mock">mock</option>
                  </select>
                </div>
                <Button type="submit" size="sm" loading={placePaperOrder.isPending}>
                  <Send className="h-3.5 w-3.5" />
                  Submit Paper Order
                </Button>
              </form>

              {paperResult && (
                <div className={cn(
                  'rounded-lg border px-3 py-2 text-sm',
                  paperResult.tone === 'success'
                    ? 'border-emerald-500/25 bg-emerald-500/10 text-emerald-200'
                    : 'border-amber-500/30 bg-amber-500/10 text-amber-200',
                )}>
                  <p className="font-semibold">{paperResult.title}</p>
                  <p className="mt-0.5 text-xs opacity-85">{paperResult.detail}</p>
                  {paperResult.orderId && (
                    <p className="mt-1 text-[10px] uppercase tracking-wider opacity-70">Order {paperResult.orderId}</p>
                  )}
                </div>
              )}
            </div>

            <div className="w-full space-y-3 lg:w-[24rem]">
              <div className="rounded-lg border border-border/70 bg-background/40 p-3">
                <div className="flex items-center justify-between gap-3">
                  <div>
                    <p className="text-xs font-semibold">Kill Switch</p>
                    <p className={cn(
                      'mt-1 text-xs',
                      settings?.kill_switch_active ? 'text-red-300' : 'text-muted-foreground',
                    )}>
                      {settings?.kill_switch_active
                        ? 'Kill switch is active. Paper orders will be blocked by safety controls.'
                        : 'Activate it to demonstrate a blocked paper order.'}
                    </p>
                  </div>
                  <Button
                    type="button"
                    variant="danger"
                    size="sm"
                    onClick={enableKillSwitch}
                    loading={killSwitch.isPending}
                    disabled={settings?.kill_switch_active}
                  >
                    <AlertTriangle className="h-3.5 w-3.5" />
                    Enable Kill Switch
                  </Button>
                </div>
              </div>

              <div>
                <div className="mb-2 flex items-center justify-between gap-2">
                  <h3 className="text-xs font-semibold uppercase tracking-[0.08em] text-muted-foreground">
                    Paper Order History
                  </h3>
                  <Button
                    variant="ghost"
                    size="xs"
                    onClick={() => qc.invalidateQueries({ queryKey: ['orders', 'paper'] })}
                  >
                    <RefreshCw className="h-3 w-3" />
                    Refresh
                  </Button>
                </div>
                <div className="max-h-72 overflow-auto rounded-lg border border-border/70 bg-background/40">
                  {loadingPaperHistory ? (
                    <div className="flex items-center justify-center gap-2 px-3 py-8 text-xs text-muted-foreground">
                      <Spinner className="h-3.5 w-3.5" />
                      Loading paper history...
                    </div>
                  ) : paperHistory?.items.length ? (
                    <div className="divide-y divide-border/60">
                      {paperHistory.items.map((item) => (
                        <div key={item.id} className="px-3 py-2">
                          <div className="flex items-start justify-between gap-3">
                            <div>
                              <p className="text-xs font-semibold text-foreground">
                                {item.ticker} <span className="text-muted-foreground">{item.side}</span>
                              </p>
                              <p className="mt-0.5 text-[11px] text-muted-foreground">
                                {item.quantity ?? '0'} qty · {item.venue ?? 'paper'} · No broker order sent
                              </p>
                            </div>
                            <Badge variant={item.risk_result === 'blocked' ? 'warning' : 'success'}>
                              {item.status}
                            </Badge>
                          </div>
                          {item.rejection_reason && (
                            <p className="mt-1 text-[11px] text-amber-300">{item.rejection_reason}</p>
                          )}
                        </div>
                      ))}
                    </div>
                  ) : (
                    <p className="px-3 py-8 text-center text-xs text-muted-foreground">
                      Paper submissions and safety blocks will appear here.
                    </p>
                  )}
                </div>
              </div>
            </div>
          </div>
        </CardContent>
      </Card>

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
