'use client'
import { useState } from 'react'
import {
  Plus, Play, Pause, FlaskConical, Settings, TrendingUp,
  Clock, Target, BarChart3, Zap, ArrowUpRight,
} from 'lucide-react'
import { useStrategies, useToggleStrategy, useCreateStrategy, usePerformanceByStrategy } from '@/hooks/use-api'
import { Button, Card, CardContent, Badge, Spinner, EmptyState } from '@/components/ui'
import { QueryError } from '@/components/shared/query-error'
import { formatDate, formatPnL, pnlClass, cn } from '@/lib/utils'
import { Sparkline } from '@/components/charts/equity-curve'
import type { Strategy } from '@/types'
import Link from 'next/link'
import { StrategyPresetGrid } from '@/components/strategies/strategy-preset-grid'

const STRATEGY_DESCRIPTIONS: Record<string, string> = {
  orb: 'Opening Range Breakout — trades the first candle breakout at session open.',
  opening_fade: 'Gap-reversal day trade — fades large overnight shocks when the open starts to mean-revert.',
  vwap_reclaim: 'VWAP Reclaim — enters when price reclaims VWAP with volume confirmation.',
  closing_momentum: 'Closing Momentum — joins persistent early-session strength only in the final half-hour.',
  intraday_periodicity: 'Intraday Periodicity — trades repeatable positive time-of-day continuation only when recent slot history agrees.',
  mean_reversion: 'Mean Reversion — fades extended moves back to a rolling average.',
  momentum: 'Momentum Breakout — enters on high-volume breakouts from consolidation.',
  buy_hold_core: 'Diversified buy-and-hold equity sleeve with low-turnover annual rebalancing.',
  equal_weight_rebalance: 'Equal-weight basket with periodic rebalancing and concentration control.',
  cross_sectional_momentum: 'Monthly long-only rotation into the strongest recent winners.',
  low_volatility_tilt: 'Lower-volatility sleeve that leans toward calmer liquid equities.',
  trend_following_tactical: 'Moving-average timing overlay that can step partly back into cash.',
}

const STRATEGY_ICONS: Record<string, React.ReactNode> = {
  orb: <Zap className="w-4 h-4" />,
  opening_fade: <ArrowUpRight className="w-4 h-4" />,
  vwap_reclaim: <TrendingUp className="w-4 h-4" />,
  closing_momentum: <Target className="w-4 h-4" />,
  intraday_periodicity: <Clock className="w-4 h-4" />,
  mean_reversion: <BarChart3 className="w-4 h-4" />,
  momentum: <Target className="w-4 h-4" />,
  buy_hold_core: <TrendingUp className="w-4 h-4" />,
  equal_weight_rebalance: <BarChart3 className="w-4 h-4" />,
  cross_sectional_momentum: <Target className="w-4 h-4" />,
  low_volatility_tilt: <Clock className="w-4 h-4" />,
  trend_following_tactical: <Zap className="w-4 h-4" />,
}

interface StrategyPerf {
  strategy: string
  total_trades: number
  win_rate: number
  total_pnl: number
  profit_factor: number
}

function StrategyCard({ strategy, perf }: { strategy: Strategy; perf?: StrategyPerf }) {
  const toggle = useToggleStrategy()

  // Build a small sparkline from seeded random data if no real perf
  // (Real data comes from /v1/reports/performance/by-strategy)
  const sparkData = perf
    ? Array.from({ length: 12 }, (_, i) => {
        // Simulate a curve from the totals
        const baseline = (perf.total_pnl / 12) * i
        return baseline + (Math.random() - 0.5) * Math.abs(perf.total_pnl / 6)
      })
    : []

  const winPct = perf ? Math.round(perf.win_rate * 100) : null
  const pnl = perf?.total_pnl ?? 0

  return (
    <Card className={cn(
      'transition-all duration-200 hover:shadow-[var(--elev-2)] hover:border-border',
      strategy.is_enabled
        ? 'border-primary/30 bg-primary/[0.025]'
        : 'opacity-80 hover:opacity-100',
    )}>
      <CardContent className="p-5">
        <div className="flex items-start gap-4">
          {/* Icon */}
          <div className={cn(
            'w-10 h-10 rounded-xl flex items-center justify-center flex-shrink-0 border',
            strategy.is_enabled
              ? 'bg-primary/12 text-primary border-primary/25 shadow-sm'
              : 'bg-muted/40 text-muted-foreground border-border/60',
          )}>
            {STRATEGY_ICONS[strategy.type] ?? <BarChart3 className="w-4 h-4" />}
          </div>

          {/* Info */}
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <h3 className="text-sm font-semibold">{strategy.name}</h3>
              <Badge variant={strategy.is_enabled ? 'success' : 'outline'}>
                {strategy.is_enabled ? 'Active' : 'Inactive'}
              </Badge>
              {strategy.is_live ? (
                <span className="badge-live">LIVE</span>
              ) : (
                <span className="badge-mock">DRY RUN</span>
              )}
            </div>

            <p className="text-xs text-muted-foreground mt-1.5 leading-relaxed">
              {strategy.description || STRATEGY_DESCRIPTIONS[strategy.type]}
            </p>

            {/* Meta row */}
            <div className="flex items-center gap-4 mt-3 text-xs text-muted-foreground flex-wrap">
              <span className="flex items-center gap-1">
                <Clock className="w-3 h-3" />
                {strategy.session_start}–{strategy.session_end}
              </span>
              <span className="flex items-center gap-1 truncate max-w-[160px]">
                <TrendingUp className="w-3 h-3 flex-shrink-0" />
                {strategy.allowed_tickers.length > 0
                  ? strategy.allowed_tickers.slice(0, 4).join(', ') + (strategy.allowed_tickers.length > 4 ? '…' : '')
                  : 'No symbols'}
              </span>
              {strategy.last_signal_at && (
                <span>Last: {formatDate(strategy.last_signal_at)}</span>
              )}
            </div>

            {/* Performance strip */}
            {perf && (
              <div className="flex items-center gap-4 mt-3 pt-3 border-t border-border/50">
                <div>
                  <p className="text-[10px] text-muted-foreground">P&L (30d)</p>
                  <p className={cn('text-xs font-bold', pnlClass(pnl))}>{formatPnL(pnl)}</p>
                </div>
                <div>
                  <p className="text-[10px] text-muted-foreground">Win Rate</p>
                  <p className={cn('text-xs font-bold', winPct && winPct >= 50 ? 'text-emerald-400' : 'text-red-400')}>
                    {winPct}%
                  </p>
                </div>
                <div>
                  <p className="text-[10px] text-muted-foreground">Trades</p>
                  <p className="text-xs font-bold">{perf.total_trades}</p>
                </div>
                <div>
                  <p className="text-[10px] text-muted-foreground">PF</p>
                  <p className={cn('text-xs font-bold', perf.profit_factor >= 1 ? 'text-emerald-400' : 'text-red-400')}>
                    {perf.profit_factor.toFixed(2)}
                  </p>
                </div>
                {sparkData.length > 0 && (
                  <div className="ml-auto">
                    <Sparkline data={sparkData} width={72} height={28} positive={pnl >= 0} />
                  </div>
                )}
              </div>
            )}
          </div>

          {/* Actions */}
          <div className="flex flex-col items-end gap-2 flex-shrink-0">
            <Link href={`/app/strategies/${strategy.id}`}>
              <Button variant="ghost" size="icon" title="Configure">
                <Settings className="w-4 h-4" />
              </Button>
            </Link>
            <Button
              variant={strategy.is_enabled ? 'outline' : 'default'}
              size="sm"
              loading={toggle.isPending}
              onClick={() => toggle.mutate({ id: strategy.id, enable: !strategy.is_enabled })}
            >
              {strategy.is_enabled
                ? <><Pause className="w-3.5 h-3.5" /> Pause</>
                : <><Play className="w-3.5 h-3.5" /> Enable</>}
            </Button>
          </div>
        </div>
      </CardContent>
    </Card>
  )
}

export default function StrategiesPage() {
  const { data: strategies = [], isLoading, isError, error, refetch } = useStrategies()
  const { data: perfByStrategy = [] } = usePerformanceByStrategy(30)
  const createMutation = useCreateStrategy()

  // Map strategy name → perf
  const perfMap = Object.fromEntries(
    (perfByStrategy as StrategyPerf[]).map((p) => [p.strategy, p])
  )

  const active   = strategies.filter(s => s.is_enabled)
  const inactive = strategies.filter(s => !s.is_enabled)

  const createDemo = () => {
    createMutation.mutate({
      name: 'ORB Strategy', type: 'orb',
      description: 'Opening Range Breakout — 15-minute opening range.',
      params: { orb_minutes: 15, min_range_pct: 0.3, risk_reward_ratio: 2.0 },
      allowed_tickers: ['AAPL', 'MSFT', 'TSLA', 'NVDA', 'SPY'],
      session_start: '09:30', session_end: '16:00', eod_flatten: true,
    })
  }

  const createPortfolioDemo = () => {
    createMutation.mutate({
      name: 'Core Portfolio Sleeve',
      type: 'buy_hold_core',
      description: 'Diversified equity core managed by the portfolio rebalance service.',
      params: {
        capital_fraction: 0.5,
        min_trade_value: 50,
        min_weight_delta_pct: 1.0,
      },
      allowed_tickers: ['SPY', 'QQQ', 'IWM', 'AAPL', 'MSFT'],
      session_start: '09:30',
      session_end: '16:00',
      eod_flatten: false,
    })
  }

  if (isLoading) {
    return (
      <div className="flex items-center gap-2 text-muted-foreground text-sm">
        <Spinner className="w-4 h-4" /> Loading strategies…
      </div>
    )
  }

  if (isError) {
    return <QueryError error={error} onRetry={refetch} label="strategies" />
  }

  // Summary totals
  const totalPnl = (perfByStrategy as StrategyPerf[]).reduce((s, p) => s + p.total_pnl, 0)
  const totalTrades = (perfByStrategy as StrategyPerf[]).reduce((s, p) => s + p.total_trades, 0)

  return (
    <div className="max-w-4xl space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h2 className="text-xl font-semibold tracking-tight">Strategies</h2>
          <p className="text-[13px] text-muted-foreground mt-1">
            {active.length} active · {strategies.length} total
            {totalTrades > 0 && ` · ${totalTrades} trades (30d)`}
          </p>
        </div>
        <div className="flex gap-2">
          {totalPnl !== 0 && (
            <span className={cn('text-sm font-bold self-center', pnlClass(totalPnl))}>
              {formatPnL(totalPnl)} 30d
            </span>
          )}
          <Button variant="outline" size="sm" onClick={createPortfolioDemo} loading={createMutation.isPending}>
            <TrendingUp className="w-3.5 h-3.5" />
            Add Core Portfolio
          </Button>
          <Button variant="outline" size="sm" onClick={createDemo} loading={createMutation.isPending}>
            <FlaskConical className="w-3.5 h-3.5" />
            Add Demo ORB
          </Button>
        </div>
      </div>

      <StrategyPresetGrid
        title="Demo-Ready Intraday Presets"
        description="Create the five strongest Trading 212-compatible intraday strategies with pre-tuned demo risk templates. Each preset starts disabled and dry-run safe."
        compact
      />

      {strategies.length === 0 ? (
        <Card>
          <CardContent>
            <EmptyState
              icon={<TrendingUp className="w-12 h-12" />}
              title="No strategies configured"
              description="Create a strategy to start generating trade signals. Begin with the demo ORB strategy."
              action={
                <Button size="sm" onClick={createDemo} loading={createMutation.isPending}>
                  <Plus className="w-3.5 h-3.5" /> Create Demo Strategy
                </Button>
              }
            />
          </CardContent>
        </Card>
      ) : (
        <>
          {active.length > 0 && (
            <div className="space-y-3">
              <p className="section-title">Active ({active.length})</p>
              {active.map(s => (
                <StrategyCard key={s.id} strategy={s} perf={perfMap[s.name]} />
              ))}
            </div>
          )}
          {inactive.length > 0 && (
            <div className="space-y-3">
              <p className="section-title">Inactive ({inactive.length})</p>
              {inactive.map(s => (
                <StrategyCard key={s.id} strategy={s} perf={perfMap[s.name]} />
              ))}
            </div>
          )}
        </>
      )}
    </div>
  )
}
