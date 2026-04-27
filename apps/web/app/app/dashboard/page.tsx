'use client'
import { useEffect, useState, useMemo, type ReactNode } from 'react'
import {
  RefreshCw, TrendingUp, Wallet, BarChart2, AlertOctagon,
  Activity, ArrowUpRight, ArrowDownRight, Wifi, WifiOff,
  BookOpen, ShieldCheck, Zap, GripVertical, RotateCcw,
} from 'lucide-react'
import { motion, AnimatePresence, Reorder } from 'framer-motion'
import { WIDGET_LABELS, normalizeWidgetOrder, useDashboardStore } from '@/stores/dashboard'
import type { WidgetId } from '@/stores/dashboard'
import { useAccount, useCashGuard, useMarketDataHealth, usePortfolioAttribution, usePortfolioMonitoring, usePositions, useOrders, useSignals, useSettings, useRiskProfile, usePerformanceReport, useWatchlistIntelligence } from '@/hooks/use-api'
import {
  Card, CardHeader, CardTitle, CardContent,
  Badge, Button, Spinner, EmptyState, TerminalCard,
} from '@/components/ui'
import { formatCurrency, formatPnL, pnlClass, formatDateShort, orderStatusBg, cn } from '@/lib/utils'
import { useQueryClient } from '@tanstack/react-query'
import { useWebSocket } from '@/hooks/use-websocket'
import { EquityCurve, Sparkline, type EquityPoint } from '@/components/charts/equity-curve'
import { RegimeBadge } from '@/components/dashboard/regime-badge'
import api from '@/services/api'
import { StrategyPresetGrid } from '@/components/strategies/strategy-preset-grid'
import type { AllocatorDecision } from '@/types'

function allocationFromSnapshot(snapshot: Record<string, unknown> | null): AllocatorDecision | null {
  const allocation = snapshot?.allocation
  if (!allocation || typeof allocation !== 'object') return null
  return allocation as AllocatorDecision
}

// ── WS status pill ────────────────────────────────────────────────────────────

function WsStatusPill({ status }: { status: string }) {
  const map: Record<string, { label: string; cls: string; dot: string }> = {
    connected:    { label: 'Live',           cls: 'text-emerald-400 bg-emerald-500/10 border-emerald-500/25', dot: 'bg-emerald-400' },
    connecting:   { label: 'Connecting',     cls: 'text-amber-400 bg-amber-500/10 border-amber-500/25',       dot: 'bg-amber-400' },
    reconnecting: { label: 'Reconnecting',   cls: 'text-amber-400 bg-amber-500/10 border-amber-500/25',       dot: 'bg-amber-400' },
    disconnected: { label: 'Offline',        cls: 'text-red-400 bg-red-500/10 border-red-500/25',             dot: 'bg-red-400' },
  }
  const cfg = map[status] ?? map.disconnected
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-[10px] font-semibold uppercase tracking-[0.08em] border',
        cfg.cls,
      )}
    >
      {status === 'connected' ? (
        <span className="relative flex items-center justify-center">
          <span className={cn('absolute w-2 h-2 rounded-full opacity-60 animate-ping', cfg.dot)} />
          <span className={cn('relative w-1.5 h-1.5 rounded-full', cfg.dot)} />
        </span>
      ) : status === 'disconnected' ? (
        <WifiOff className="w-3 h-3" />
      ) : (
        <span className={cn('w-1.5 h-1.5 rounded-full animate-pulse', cfg.dot)} />
      )}
      {cfg.label}
    </span>
  )
}

// ── Main dashboard ────────────────────────────────────────────────────────────

export default function DashboardPage() {
  const qc = useQueryClient()
  const { data: account, isLoading: acctLoading } = useAccount()
  const { data: cash } = useCashGuard()
  const { data: positions = [] } = usePositions()
  const { data: orders = [] } = useOrders({ limit: 8 })
  const { data: signals = [] } = useSignals({ limit: 6 })
  const { data: settings } = useSettings()
  const { data: riskProfile } = useRiskProfile()
  const { data: perfReport } = usePerformanceReport()
  const { data: portfolioMonitoring = [] } = usePortfolioMonitoring()
  const { data: portfolioAttribution = [] } = usePortfolioAttribution()
  const { data: marketDataHealth } = useMarketDataHealth()
  const { data: watchlistIntelligence } = useWatchlistIntelligence(5)

  // Widget order (drag-to-reorder, persisted in localStorage)
  const { widgetOrder, setWidgetOrder, resetOrder } = useDashboardStore()
  const [customizing, setCustomizing] = useState(false)
  const normalizedWidgetOrder = useMemo(() => normalizeWidgetOrder(widgetOrder), [widgetOrder])

  useEffect(() => {
    if (normalizedWidgetOrder.join('|') !== widgetOrder.join('|')) {
      setWidgetOrder(normalizedWidgetOrder)
    }
  }, [normalizedWidgetOrder, setWidgetOrder, widgetOrder])

  // Token from localStorage for WS auth
  const [token, setToken] = useState<string | null>(null)
  useEffect(() => { setToken(api.getToken()) }, [])

  // Live WebSocket feed
  const { snapshot, status: wsStatus } = useWebSocket({ token, enabled: !!token })

  const signalDetailsById = useMemo(
    () => new Map(signals.map((signal) => [signal.id, signal])),
    [signals],
  )
  const orderDetailsById = useMemo(
    () => new Map(orders.map((order) => [order.id, order])),
    [orders],
  )

  // Merge live WS data over REST data for real-time feel while preserving explainability.
  const liveAccount   = snapshot?.account
  const livePositions = snapshot?.positions ?? positions
  const liveSignals = snapshot?.signals
    ? snapshot.signals.map((signal) => {
        const detail = signalDetailsById.get(signal.id)
        return {
          ...signal,
          strategy_name: detail?.strategy_name ?? null,
          strategy_type_name: detail?.strategy_type_name ?? null,
          reason: detail?.reason ?? null,
          risk_rejected: detail?.risk_rejected ?? false,
          risk_rejection_reason: detail?.risk_rejection_reason ?? null,
          params_snapshot: detail?.params_snapshot ?? null,
        }
      })
    : signals.map((signal) => ({
        id: signal.id,
        ticker: signal.ticker,
        side: signal.side,
        signal_type: signal.signal_type,
        status: signal.status,
        confidence: Number(signal.confidence ?? 0),
        generated_at: signal.generated_at,
        strategy_name: signal.strategy_name,
        strategy_type_name: signal.strategy_type_name,
        reason: signal.reason,
        risk_rejected: signal.risk_rejected,
        risk_rejection_reason: signal.risk_rejection_reason,
        params_snapshot: signal.params_snapshot,
      }))
  const liveOrders = snapshot?.orders
    ? snapshot.orders.map((order) => {
        const detail = orderDetailsById.get(order.id)
        return {
          ...order,
          strategy_name: detail?.strategy_name ?? null,
          strategy_type_name: detail?.strategy_type_name ?? null,
          signal_reason: detail?.signal_reason ?? null,
          signal_confidence: detail?.signal_confidence ? Number(detail.signal_confidence) : null,
          signal_risk_rejected: detail?.signal_risk_rejected ?? null,
          signal_risk_rejection_reason: detail?.signal_risk_rejection_reason ?? null,
          error_message: detail?.error_message ?? null,
          is_dry_run: detail?.is_dry_run ?? false,
        }
      })
    : orders.map((order) => ({
        id: order.id,
        ticker: order.ticker,
        side: order.side,
        order_type: order.order_type,
        quantity: Number(order.quantity),
        status: order.status,
        created_at: order.created_at,
        strategy_name: order.strategy_name,
        strategy_type_name: order.strategy_type_name,
        signal_reason: order.signal_reason,
        signal_confidence: order.signal_confidence ? Number(order.signal_confidence) : null,
        signal_risk_rejected: order.signal_risk_rejected,
        signal_risk_rejection_reason: order.signal_risk_rejection_reason,
        error_message: order.error_message,
        is_dry_run: order.is_dry_run,
      }))
  const liveSystem    = snapshot?.system
  const liveRegime    = snapshot?.regime
  const recentAllocationDecisions = useMemo(
    () => signals
      .map((signal) => allocationFromSnapshot(signal.params_snapshot))
      .filter((decision): decision is AllocatorDecision => decision !== null)
      .slice(0, 4),
    [signals],
  )

  const totalValue   = liveAccount?.total_value ?? account?.total_value ?? 0
  const freeCash     = liveAccount?.free_cash    ?? cash?.available_to_trade ?? 0
  const invested     = liveAccount?.invested      ?? account?.invested ?? 0
  const unrealizedPnl = liveAccount?.unrealized_pnl
    ?? livePositions.reduce((sum, p) => sum + (p.unrealized_pnl ?? 0), 0)

  // Equity curve from performance report
  const equityData = useMemo<EquityPoint[]>(() => {
    if (!perfReport?.daily_pnl?.length) return []
    let cumulative = 0
    return perfReport.daily_pnl.map(({ date, pnl }) => {
      cumulative += pnl
      return { date: date.slice(5), pnl: parseFloat(cumulative.toFixed(2)), daily: pnl }
    })
  }, [perfReport])

  const pendingOrders = liveOrders.filter(o =>
    ['submitted', 'accepted', 'pending_intent'].includes(o.status)
  )

  const portfolioAttributionById = useMemo(
    () => Object.fromEntries(portfolioAttribution.map((item) => [item.strategy_id, item])),
    [portfolioAttribution],
  )

  const killSwitchActive = liveSystem?.kill_switch_active ?? settings?.kill_switch_active
  const autoTradingEnabled = liveSystem?.auto_trading_enabled ?? settings?.auto_trading_enabled
  const degradedSymbols = marketDataHealth?.symbols.filter((symbol) => !['ok', 'fallback'].includes(symbol.status)) ?? []

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ['account'] })
    qc.invalidateQueries({ queryKey: ['positions'] })
    qc.invalidateQueries({ queryKey: ['orders'] })
    qc.invalidateQueries({ queryKey: ['signals'] })
  }


  const dashboardSections: Record<WidgetId, ReactNode> = {
    stats: (
      <>
        {/* ── Stat cards ── */}
        {acctLoading && !snapshot ? (
          <div className="flex items-center gap-2 text-muted-foreground text-sm h-24">
            <Spinner className="w-4 h-4" /> Loading account…
          </div>
        ) : (
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
            <TerminalCard
              label="Total Value"
              value={formatCurrency(totalValue)}
              sub={liveAccount?.currency ?? account?.currency ?? 'GBP'}
              variant="cyan"
              icon={<Wallet className="w-5 h-5" />}
              live={wsStatus === 'connected'}
            />
            <TerminalCard
              label="Available to Trade"
              value={formatCurrency(freeCash)}
              sub="Cash-only · no leverage"
              variant="cyan"
              icon={<ShieldCheck className="w-5 h-5" />}
            />
            <TerminalCard
              label="Invested"
              value={formatCurrency(invested)}
              sub={`${livePositions.length} position${livePositions.length !== 1 ? 's' : ''}`}
              variant="cyan"
              icon={<BarChart2 className="w-5 h-5" />}
            />
            <TerminalCard
              label="Unrealized P&L"
              value={<span className={pnlClass(unrealizedPnl)}>{formatPnL(unrealizedPnl)}</span>}
              sub={unrealizedPnl > 0 ? 'Profitable' : unrealizedPnl < 0 ? 'In drawdown' : 'Flat'}
              variant={unrealizedPnl >= 0 ? 'teal' : 'red'}
              icon={unrealizedPnl >= 0 ? <TrendingUp className="w-5 h-5" /> : <ArrowDownRight className="w-5 h-5" />}
              live={wsStatus === 'connected'}
            />
          </div>
        )}
      </>
    ),
    presets: (
      <>
        <Card>
          <CardContent className="p-5">
            <StrategyPresetGrid
              title="Add Demo Strategies"
              description="Launch pre-tuned ORB, gap fade, VWAP reclaim, closing momentum, and intraday periodicity sleeves directly from the dashboard. Each preset is created disabled with a matching demo risk template."
              compact
            />
          </CardContent>
        </Card>
      </>
    ),
    equity: (
      <>
        {/* ── Equity curve + Regime ── */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
          {/* Equity curve spans 2 cols */}
          <Card className="lg:col-span-2">
            <CardHeader>
              <div className="flex items-center justify-between">
                <div>
                  <CardTitle>Equity Curve</CardTitle>
                  <p className="text-[10px] text-muted-foreground mt-0.5">30-day cumulative P&L</p>
                </div>
                <div className="text-right">
                  <p className={cn('text-lg font-bold tabular-nums', pnlClass(perfReport?.total_pnl ?? 0))}>
                    {formatPnL(perfReport?.total_pnl ?? 0)}
                  </p>
                  <p className="text-[10px] text-muted-foreground">
                    {perfReport?.total_trades ?? 0} trades · {Math.round((perfReport?.win_rate ?? 0) * 100)}% win
                  </p>
                </div>
              </div>
            </CardHeader>
            <CardContent className="pt-1">
              <EquityCurve data={equityData} height={170} showGrid />
            </CardContent>
          </Card>

          {/* Regime + system status */}
          <div className="flex flex-col gap-3">
            <Card className="flex-1">
              <CardHeader>
                <CardTitle>Market Regime</CardTitle>
              </CardHeader>
              <CardContent className="space-y-3 pt-1">
                <RegimeBadge regime={liveRegime} />
                {liveRegime && (
                  <div className="space-y-1.5 text-xs">
                    {liveRegime.detail && (
                      <p className="text-muted-foreground leading-relaxed">{liveRegime.detail}</p>
                    )}
                    {liveRegime.active_strategies.length > 0 && (
                      <div className="flex items-start gap-2">
                        <span className="text-emerald-400 font-medium shrink-0">Active:</span>
                        <span className="text-muted-foreground capitalize">
                          {liveRegime.active_strategies.join(', ')}
                        </span>
                      </div>
                    )}
                    {liveRegime.suppressed_strategies.length > 0 && (
                      <div className="flex items-start gap-2">
                        <span className="text-amber-400 font-medium shrink-0">Paused:</span>
                        <span className="text-muted-foreground capitalize">
                          {liveRegime.suppressed_strategies.join(', ')}
                        </span>
                      </div>
                    )}
                  </div>
                )}
              </CardContent>
            </Card>

            {/* System status mini card */}
            <Card>
              <CardContent className="p-4 space-y-3">
                <StatusRow
                  label="Auto Trading"
                  active={!!autoTradingEnabled}
                  activeText="Enabled"
                  inactiveText="Disabled"
                  activeClass="text-emerald-400"
                  inactiveClass="text-muted-foreground"
                />
                <StatusRow
                  label="Kill Switch"
                  active={!!killSwitchActive}
                  activeText="ACTIVE"
                  inactiveText="Inactive"
                  activeClass="text-red-400 animate-pulse"
                  inactiveClass="text-emerald-400"
                />
                <StatusRow
                  label="Pending Orders"
                  active={pendingOrders.length > 0}
                  activeText={`${pendingOrders.length} pending`}
                  inactiveText="None"
                  activeClass="text-amber-400"
                  inactiveClass="text-muted-foreground"
                />
                <StatusRow
                  label="Max Daily Loss"
                  active={false}
                  activeText=""
                  inactiveText={riskProfile ? `${riskProfile.max_daily_loss_pct}%` : '—'}
                  activeClass=""
                  inactiveClass="text-foreground"
                />
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <CardTitle>Decision Context</CardTitle>
              </CardHeader>
              <CardContent className="space-y-3 pt-1">
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <p className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground">Market Data</p>
                    <p className={cn(
                      'mt-1 text-sm font-semibold capitalize',
                      marketDataHealth?.status === 'ok' || marketDataHealth?.status === 'fallback'
                        ? 'text-emerald-400'
                        : marketDataHealth?.status === 'unknown'
                          ? 'text-muted-foreground'
                          : 'text-amber-400',
                    )}>
                      {marketDataHealth?.status ?? 'unknown'}
                    </p>
                  </div>
                  <Badge variant={degradedSymbols.length > 0 ? 'warning' : 'success'}>
                    {degradedSymbols.length > 0 ? `${degradedSymbols.length} flagged` : 'trade-safe'}
                  </Badge>
                </div>
                <p className="text-xs text-muted-foreground leading-relaxed">
                  {marketDataHealth?.detail ?? 'Feed health has not been sampled yet.'}
                </p>
                {degradedSymbols.length > 0 && (
                  <div className="rounded-lg border border-amber-500/20 bg-amber-500/5 px-3 py-2.5">
                    <p className="text-[11px] uppercase tracking-[0.18em] text-amber-300">Blocked Symbols</p>
                    <div className="mt-2 space-y-1.5">
                      {degradedSymbols.slice(0, 3).map((symbol) => (
                        <div key={symbol.ticker} className="flex items-start justify-between gap-3 text-xs">
                          <span className="font-medium text-foreground">{symbol.ticker}</span>
                          <span className="text-right text-muted-foreground">{symbol.status}</span>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
                <div className="rounded-lg border border-border/60 bg-muted/15 px-3 py-2.5">
                  <p className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground">Top Watchlist Catalysts</p>
                  {watchlistIntelligence?.news?.length ? (
                    <div className="mt-2 space-y-2">
                      {watchlistIntelligence.news.slice(0, 3).map((item) => (
                        <div key={item.id} className="text-xs">
                          <div className="flex items-center justify-between gap-2">
                            <span className="font-medium text-foreground">{item.tickers.join(', ') || 'Watchlist'}</span>
                            <span className="text-muted-foreground">{item.event_type.replace(/_/g, ' ')}</span>
                          </div>
                          <p className="mt-1 text-muted-foreground line-clamp-2">{item.title}</p>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <p className="mt-2 text-xs text-muted-foreground">No fresh structured catalyst context is available right now.</p>
                  )}
                </div>
                <div className="rounded-lg border border-border/60 bg-muted/15 px-3 py-2.5">
                  <div className="flex items-center justify-between gap-3">
                    <p className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground">Capital Allocator</p>
                    <Badge variant={recentAllocationDecisions.some((decision) => decision.status === 'rejected') ? 'warning' : 'success'}>
                      {recentAllocationDecisions.length} recent
                    </Badge>
                  </div>
                  {recentAllocationDecisions.length === 0 ? (
                    <p className="mt-2 text-xs text-muted-foreground">No allocation decisions recorded yet. The next strategy run will stamp each signal with won/lost allocation evidence.</p>
                  ) : (
                    <div className="mt-2 space-y-2">
                      {recentAllocationDecisions.map((decision, index) => (
                        <div key={`${decision.generated_at ?? index}-${decision.ticker}`} className="rounded-md border border-border/40 px-2.5 py-2 text-xs">
                          <div className="flex items-center justify-between gap-3">
                            <span className="font-medium text-foreground">{decision.ticker} · {decision.strategy_type.replace(/_/g, ' ')}</span>
                            <span className={decision.status === 'allocated' ? 'text-emerald-400' : 'text-amber-300'}>
                              {decision.status === 'allocated' ? 'won' : 'lost'} · {(decision.score * 100).toFixed(0)}
                            </span>
                          </div>
                          <p className="mt-1 text-muted-foreground line-clamp-2">{decision.reason}</p>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              </CardContent>
            </Card>
          </div>
        </div>
      </>
    ),
    positions: (
      <>
          {/* Open Positions */}
          <Card>
            <CardHeader>
              <div className="flex items-center justify-between">
                <CardTitle>Open Positions</CardTitle>
                <a href="/app/positions" className="text-xs text-primary hover:underline">View all</a>
              </div>
            </CardHeader>
            <CardContent>
              {livePositions.length === 0 ? (
                <EmptyState title="No open positions" description="Positions appear once trades are executed." />
              ) : (
                <div className="space-y-0">
                  {livePositions.slice(0, 5).map((pos) => (
                    <div
                      key={pos.ticker}
                      className="flex items-center justify-between py-2.5 border-b border-border/40 last:border-0"
                    >
                      <div className="flex items-center gap-3">
                        <div className="w-8 h-8 rounded-lg bg-primary/10 flex items-center justify-center text-xs font-bold text-primary">
                          {pos.ticker.slice(0, 2)}
                        </div>
                        <div>
                          <p className="text-sm font-semibold">{pos.ticker}</p>
                          <p className="text-[10px] text-muted-foreground">{pos.quantity} shares</p>
                        </div>
                      </div>
                      <div className="text-right">
                        <p className="text-sm font-medium">{formatCurrency(pos.current_price)}</p>
                        <p className={cn('text-xs font-medium', pnlClass(pos.unrealized_pnl))}>
                          {formatPnL(pos.unrealized_pnl)}
                        </p>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>
      </>
    ),
    signals: (
      <>
          {/* Recent Signals */}
          <Card>
            <CardHeader>
              <div className="flex items-center justify-between">
                <CardTitle>Recent Signals</CardTitle>
                <a href="/app/strategies" className="text-xs text-primary hover:underline">Strategies →</a>
              </div>
            </CardHeader>
            <CardContent>
              {liveSignals.length === 0 ? (
                <EmptyState title="No signals yet" description="Enable a strategy to start generating signals." />
              ) : (
                <div className="space-y-0">
                  {liveSignals.map((sig) => (
                    <div
                      key={sig.id}
                      className="flex items-center justify-between py-2.5 border-b border-border/40 last:border-0"
                    >
                      <div className="flex items-center gap-2.5">
                        <div className={cn(
                          'w-7 h-7 rounded-lg flex items-center justify-center',
                          sig.side === 'buy' ? 'bg-emerald-500/10' : 'bg-red-500/10',
                        )}>
                          {sig.side === 'buy'
                            ? <ArrowUpRight className="w-3.5 h-3.5 text-emerald-400" />
                            : <ArrowDownRight className="w-3.5 h-3.5 text-red-400" />}
                        </div>
                        <div>
                          <p className="text-sm font-semibold">{sig.ticker}</p>
                          <p className="text-[10px] text-muted-foreground capitalize">
                            {sig.signal_type.replace(/_/g, ' ')}
                            {sig.strategy_name ? ` · ${sig.strategy_name}` : ''}
                          </p>
                          {(sig.risk_rejection_reason ?? sig.reason) && (
                            <p className="mt-0.5 max-w-[16rem] text-[10px] text-muted-foreground line-clamp-2">
                              {sig.risk_rejection_reason ?? sig.reason}
                            </p>
                          )}
                        </div>
                      </div>
                      <div className="text-right">
                        <Badge
                          variant={
                            sig.status === 'executed' ? 'success'
                            : sig.status === 'rejected' ? 'destructive'
                            : 'default'
                          }
                          className="text-[10px]"
                        >
                          {sig.status}
                        </Badge>
                        {sig.confidence > 0 && (
                          <p className="text-[10px] text-muted-foreground mt-0.5">
                            {Math.round(sig.confidence * 100)}% conf
                          </p>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>
      </>
    ),
    orders: (
      <>
        {/* ── Recent Orders table ── */}
        <Card>
          <CardHeader>
            <div className="flex items-center justify-between">
              <CardTitle>Recent Orders</CardTitle>
              <a href="/app/orders" className="text-xs text-primary hover:underline">View all</a>
            </div>
          </CardHeader>
          <CardContent className="p-0 overflow-x-auto">
            {liveOrders.length === 0 ? (
              <div className="p-5 pt-0">
                <EmptyState title="No orders yet" description="Orders placed by strategies or manually will appear here." />
              </div>
            ) : (
              <table className="w-full data-table">
                <thead>
                  <tr>
                    <th>Symbol</th>
                    <th>Side</th>
                    <th className="hidden sm:table-cell">Type</th>
                    <th className="hidden sm:table-cell">Qty</th>
                    <th>Status</th>
                    <th className="hidden md:table-cell">Time</th>
                  </tr>
                </thead>
                <tbody>
                  {liveOrders.slice(0, 8).map((o) => (
                    <tr key={o.id}>
                      <td>
                        <p className="font-semibold">{o.ticker}</p>
                        {(o.strategy_name || o.signal_risk_rejection_reason || o.signal_reason || o.error_message) && (
                          <p className="mt-0.5 max-w-[16rem] text-[10px] text-muted-foreground line-clamp-2">
                            {o.signal_risk_rejection_reason ?? o.error_message ?? o.signal_reason ?? o.strategy_name}
                          </p>
                        )}
                      </td>
                      <td className={o.side === 'buy' ? 'text-emerald-400 font-medium' : 'text-red-400 font-medium'}>
                        {o.side.toUpperCase()}
                      </td>
                      <td className="hidden sm:table-cell text-muted-foreground capitalize">{o.order_type}</td>
                      <td className="hidden sm:table-cell tabular-nums">{o.quantity}</td>
                      <td>
                        <span className={cn('text-xs px-2 py-0.5 rounded-full font-medium', orderStatusBg(o.status))}>
                          {o.status}
                        </span>
                      </td>
                      <td className="hidden md:table-cell text-muted-foreground">{formatDateShort(o.created_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </CardContent>
        </Card>
      </>
    ),
    portfolio: (
      <>
        {/* ── Portfolio automation monitoring ── */}
        <Card>
          <CardHeader>
            <div className="flex items-center justify-between">
              <CardTitle>Portfolio Rebalance Monitor</CardTitle>
              <a href="/app/strategies" className="text-xs text-primary hover:underline">Manage sleeves →</a>
            </div>
          </CardHeader>
          <CardContent>
            {portfolioMonitoring.length === 0 ? (
              <EmptyState
                title="No portfolio sleeves configured"
                description="Create a portfolio strategy to see rebalance status, target weights, and recent sleeve orders here."
              />
            ) : (
              <div className="grid gap-3 lg:grid-cols-2">
                {portfolioMonitoring.map((strategy) => (
                  <div key={strategy.strategy_id} className="rounded-xl border border-border/60 bg-muted/15 p-4">
                    <div className="flex items-start justify-between gap-3">
                      <div>
                        <div className="flex items-center gap-2">
                          <p className="text-sm font-semibold">{strategy.strategy_name}</p>
                          <Badge variant={strategy.is_enabled ? 'success' : 'outline'}>
                            {strategy.is_enabled ? 'Active' : 'Inactive'}
                          </Badge>
                        </div>
                        <p className="mt-1 text-[11px] uppercase tracking-[0.16em] text-muted-foreground">
                          {strategy.strategy_type.replace(/_/g, ' ')}
                        </p>
                      </div>
                      <div className="text-right">
                        <Badge
                          variant={
                            strategy.last_status === 'rebalanced'
                              ? 'success'
                              : strategy.last_status === 'skipped'
                                ? 'warning'
                                : strategy.last_status === 'error'
                                  ? 'destructive'
                                  : 'outline'
                          }
                        >
                          {strategy.last_status ?? 'unknown'}
                        </Badge>
                        <p className="mt-1 text-[11px] text-muted-foreground">{strategy.last_mode ?? 'pending'}</p>
                      </div>
                    </div>

                    <div className="mt-3 grid grid-cols-2 gap-2 text-xs">
                      <div className="rounded-lg border border-border/50 px-3 py-2">
                        <p className="text-muted-foreground">Last rebalance</p>
                        <p className="mt-1 font-medium">{strategy.last_rebalance_at ? formatDateShort(strategy.last_rebalance_at) : 'Not yet'}</p>
                      </div>
                      <div className="rounded-lg border border-border/50 px-3 py-2">
                        <p className="text-muted-foreground">Orders / blocks</p>
                        <p className="mt-1 font-medium">
                          {strategy.last_orders_submitted + strategy.last_dry_run_orders} / {strategy.last_risk_blocks}
                        </p>
                        <p className="mt-0.5 text-[11px] text-muted-foreground">
                          allocator {strategy.last_allocation_blocks}
                        </p>
                      </div>
                    </div>

                    {portfolioAttributionById[strategy.strategy_id] && (
                      <div className="mt-3 rounded-xl border border-border/50 bg-card/40 px-3 py-3">
                        <div className="flex items-center justify-between gap-3">
                          <div>
                            <p className="text-[11px] uppercase tracking-[0.16em] text-muted-foreground">Sleeve P&amp;L</p>
                            <p className={cn('mt-1 text-base font-semibold', pnlClass(portfolioAttributionById[strategy.strategy_id].total_pnl))}>
                              {formatPnL(portfolioAttributionById[strategy.strategy_id].total_pnl)}
                            </p>
                            <p className="mt-1 text-[11px] text-muted-foreground">
                              Realized {formatPnL(portfolioAttributionById[strategy.strategy_id].realized_pnl)} · Unrealized {formatPnL(portfolioAttributionById[strategy.strategy_id].unrealized_pnl)}
                            </p>
                          </div>
                          <Sparkline
                            data={portfolioAttributionById[strategy.strategy_id].recent_timeline.map((point) => point.equity_pnl)}
                            positive={portfolioAttributionById[strategy.strategy_id].total_pnl >= 0}
                            width={88}
                            height={38}
                          />
                        </div>
                        <div className="mt-3 grid grid-cols-2 gap-2 text-xs">
                          <div className="rounded-lg border border-border/40 px-3 py-2">
                            <p className="text-muted-foreground">Market value</p>
                            <p className="mt-1 font-medium">{formatCurrency(portfolioAttributionById[strategy.strategy_id].current_market_value)}</p>
                          </div>
                          <div className="rounded-lg border border-border/40 px-3 py-2">
                            <p className="text-muted-foreground">Turnover</p>
                            <p className="mt-1 font-medium">{formatCurrency(portfolioAttributionById[strategy.strategy_id].turnover_notional)}</p>
                          </div>
                        </div>
                        <div className="mt-2 grid grid-cols-2 gap-2 text-xs">
                          <div className="rounded-lg border border-border/40 px-3 py-2">
                            <p className="text-muted-foreground">Alpha</p>
                            <p className={cn('mt-1 font-medium', pnlClass(portfolioAttributionById[strategy.strategy_id].alpha_vs_benchmark_pct))}>
                              {portfolioAttributionById[strategy.strategy_id].alpha_vs_benchmark_pct >= 0 ? '+' : ''}
                              {portfolioAttributionById[strategy.strategy_id].alpha_vs_benchmark_pct.toFixed(2)}%
                            </p>
                            <p className="mt-0.5 text-[11px] text-muted-foreground">
                              vs {portfolioAttributionById[strategy.strategy_id].benchmark_name}
                            </p>
                          </div>
                          <div className="rounded-lg border border-border/40 px-3 py-2">
                            <p className="text-muted-foreground">Max drawdown</p>
                            <p className="mt-1 font-medium text-red-400">
                              -{portfolioAttributionById[strategy.strategy_id].max_drawdown_pct.toFixed(2)}%
                            </p>
                            <p className="mt-0.5 text-[11px] text-muted-foreground">
                              Bench -{portfolioAttributionById[strategy.strategy_id].benchmark_max_drawdown_pct.toFixed(2)}%
                            </p>
                          </div>
                        </div>
                      </div>
                    )}

                    <div className="mt-3 space-y-2">
                      <p className="text-[11px] uppercase tracking-[0.16em] text-muted-foreground">Weights</p>
                      {strategy.weights.length === 0 ? (
                        <p className="text-xs text-muted-foreground">No stored weight snapshot yet.</p>
                      ) : (
                        <div className="space-y-1.5">
                          {strategy.weights.slice(0, 4).map((weight) => (
                            <div key={`${strategy.strategy_id}-${weight.ticker}`} className="flex items-center justify-between rounded-lg border border-border/40 px-3 py-2 text-xs">
                              <span className="font-medium">{weight.ticker}</span>
                              <span className="text-muted-foreground">
                                {weight.target_weight == null ? '—' : `${(weight.target_weight * 100).toFixed(1)}%`} target
                              </span>
                              <span className="text-foreground">
                                {weight.current_weight == null ? '—' : `${(weight.current_weight * 100).toFixed(1)}%`} current
                              </span>
                            </div>
                          ))}
                        </div>
                      )}
                    </div>

                    <div className="mt-3 space-y-2">
                      <p className="text-[11px] uppercase tracking-[0.16em] text-muted-foreground">Recent rebalance orders</p>
                      {strategy.recent_orders.length === 0 ? (
                        <p className="text-xs text-muted-foreground">No recent rebalance orders recorded.</p>
                      ) : (
                        <div className="space-y-1.5">
                          {strategy.recent_orders.slice(0, 2).map((order) => (
                            <div key={order.order_id} className="flex items-center justify-between rounded-lg border border-border/40 px-3 py-2 text-xs">
                              <div>
                                <p className="font-medium">
                                  {order.ticker} <span className={order.side === 'buy' ? 'text-emerald-400' : 'text-red-400'}>{order.side.toUpperCase()}</span>
                                </p>
                                <p className="mt-0.5 text-muted-foreground">
                                  {order.quantity.toFixed(4)} · {order.is_dry_run ? 'dry run' : order.status}
                                </p>
                                {order.allocation_status && (
                                  <p className={cn(
                                    'mt-0.5',
                                    order.allocation_status === 'allocated' ? 'text-emerald-400' : 'text-amber-300',
                                  )}>
                                    {order.allocation_status === 'allocated' ? 'won allocation' : 'lost allocation'}
                                    {order.allocation_score != null ? ` ${(order.allocation_score * 100).toFixed(0)}` : ''}
                                  </p>
                                )}
                              </div>
                              <div className="text-right">
                                <p className="font-medium">{order.target_weight == null ? '—' : `${(order.target_weight * 100).toFixed(1)}%`}</p>
                                <p className="mt-0.5 text-muted-foreground">{formatDateShort(order.created_at)}</p>
                              </div>
                            </div>
                          ))}
                        </div>
                      )}
                    </div>

                    <div className="mt-3">
                      <a href={`/app/strategies/${strategy.strategy_id}`} className="text-xs text-primary hover:underline">
                        Open strategy detail →
                      </a>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </CardContent>
        </Card>
      </>
    ),
    performance: (
      <>
        {/* ── Performance summary strip ── */}
        {perfReport && perfReport.total_trades > 0 && (
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
            <MiniStat label="Win Rate" value={`${Math.round(perfReport.win_rate * 100)}%`} positive={perfReport.win_rate >= 0.5} />
            <MiniStat label="Profit Factor" value={perfReport.profit_factor.toFixed(2)} positive={perfReport.profit_factor >= 1} />
            <MiniStat label="Avg Win" value={formatCurrency(perfReport.avg_win)} positive={true} />
            <MiniStat label="Max Drawdown" value={formatCurrency(perfReport.max_drawdown)} positive={false} />
          </div>
        )}
      </>
    ),
  }
  return (
    <div className="space-y-5 animate-fade-in">

      {/* Kill switch banner */}
      <AnimatePresence>
        {killSwitchActive && (
          <motion.div
            initial={{ opacity: 0, y: -8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -8 }}
            className="flex items-center gap-3 px-4 py-3 bg-red-500/10 border border-red-500/30 rounded-xl text-red-300 shadow-[var(--elev-1)]"
          >
            <div className="w-8 h-8 rounded-lg bg-red-500/15 border border-red-500/30 flex items-center justify-center flex-shrink-0">
              <AlertOctagon className="w-4 h-4 animate-pulse-slow" />
            </div>
            <div className="flex-1 min-w-0">
              <p className="text-sm font-semibold">Kill Switch is active</p>
              <p className="text-xs text-red-300/70">All automated trading is halted</p>
            </div>
            <a
              href="/app/emergency"
              className="text-xs font-medium px-3 py-1.5 rounded-md bg-red-500/15 hover:bg-red-500/25 border border-red-500/30 transition-colors"
            >
              Manage →
            </a>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Header row */}
      <div className="flex items-center justify-between gap-4 flex-wrap">
        <div>
          <h2 className="text-xl font-semibold tracking-tight">Portfolio Overview</h2>
          <p className="text-[13px] text-muted-foreground mt-1">Real-time account snapshot</p>
        </div>
        <div className="flex items-center gap-2">
          <WsStatusPill status={wsStatus} />
          <Button
            variant={customizing ? 'default' : 'outline'}
            size="sm"
            onClick={() => setCustomizing(v => !v)}
            title="Drag widgets to reorder"
          >
            <GripVertical className="w-3.5 h-3.5" />
            <span className="hidden sm:inline">{customizing ? 'Done' : 'Customize'}</span>
          </Button>
          {customizing && (
            <Button variant="ghost" size="sm" onClick={resetOrder} title="Reset order">
              <RotateCcw className="w-3.5 h-3.5" />
            </Button>
          )}
          <Button variant="outline" size="sm" onClick={refresh}>
            <RefreshCw className="w-3.5 h-3.5" />
            <span className="hidden sm:inline">Refresh</span>
          </Button>
        </div>
      </div>

      <AnimatePresence>
        {customizing && (
          <motion.div
            initial={{ opacity: 0, y: -8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -8 }}
            className="border border-primary/30 bg-primary/5 rounded-xl p-4"
          >
            <p className="text-xs font-semibold text-primary mb-3 flex items-center gap-1.5">
              <GripVertical className="w-3.5 h-3.5" /> Drag to reorder dashboard sections
            </p>
            <Reorder.Group
              axis="y"
              values={normalizedWidgetOrder}
              onReorder={setWidgetOrder}
              className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4"
            >
              {normalizedWidgetOrder.map((widgetId) => (
                <Reorder.Item
                  key={widgetId}
                  value={widgetId}
                  className="flex items-center gap-3 bg-card border border-border rounded-lg px-3 py-2.5 cursor-grab active:cursor-grabbing select-none"
                >
                  <GripVertical className="w-4 h-4 text-muted-foreground flex-shrink-0" />
                  <span className="text-sm text-foreground">
                    {WIDGET_LABELS[widgetId]}
                  </span>
                </Reorder.Item>
              ))}
            </Reorder.Group>
            <p className="text-[10px] text-muted-foreground mt-3">
              Order is saved automatically and persists across sessions.
            </p>
          </motion.div>
        )}
      </AnimatePresence>

      <div className="space-y-5">
        {normalizedWidgetOrder.map((widgetId) => (
          <section key={widgetId}>
            {dashboardSections[widgetId]}
          </section>
        ))}
      </div>
    </div>
  )
}

// ── Internal components ───────────────────────────────────────────────────────

function StatusRow({
  label, active, activeText, inactiveText, activeClass, inactiveClass,
}: {
  label: string
  active: boolean
  activeText: string
  inactiveText: string
  activeClass: string
  inactiveClass: string
}) {
  return (
    <div className="flex items-center justify-between text-xs">
      <span className="text-muted-foreground">{label}</span>
      <span className={cn('font-medium', active ? activeClass : inactiveClass)}>
        {active ? activeText : inactiveText}
      </span>
    </div>
  )
}

function MiniStat({ label, value, positive }: { label: string; value: string; positive: boolean }) {
  return (
    <div className="bg-card border border-border rounded-lg px-3 py-2.5">
      <p className="text-[10px] text-muted-foreground">{label}</p>
      <p className={cn('text-sm font-bold mt-0.5', positive ? 'text-emerald-400' : 'text-red-400')}>{value}</p>
    </div>
  )
}
