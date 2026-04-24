'use client'

import { useEffect, useState } from 'react'
import { useForm } from 'react-hook-form'
import {
  AlertTriangle,
  CheckCircle2,
  Clock,
  Play,
  ShieldAlert,
  TrendingUp,
  XCircle,
  type LucideIcon,
} from 'lucide-react'
import {
  Area,
  AreaChart,
  CartesianGrid,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import toast from 'react-hot-toast'

import {
  Badge,
  Button,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  Input,
  Label,
  Spinner,
  StatCard,
} from '@/components/ui'
import { cn, formatCurrency, formatPercent, pnlClass } from '@/lib/utils'
import api from '@/services/api'
import type {
  BacktestJob,
  BacktestMonteCarlo,
  BacktestResult,
  BacktestStrategyInfo,
  BacktestStrategyType,
  PortfolioBacktestJob,
  PortfolioBacktestResult,
  PortfolioBacktestStrategyInfo,
  PortfolioBacktestStrategyType,
  WalkForwardSummary,
  WalkForwardWindow,
} from '@/types'

type JobStatus = 'idle' | 'running' | 'complete' | 'error'

type BacktestFormValues = {
  ticker: string
  strategy_type: BacktestStrategyType
  from_date: string
  to_date: string
  initial_capital: string
  run_walk_forward: boolean
}

type PortfolioFormValues = {
  tickers: string
  strategy_type: PortfolioBacktestStrategyType
  from_date: string
  to_date: string
  initial_capital: string
}

const VERDICT_STYLES: Record<string, { color: string; icon: LucideIcon; label: string }> = {
  strong: { color: 'text-emerald-400', icon: CheckCircle2, label: 'Strong Edge' },
  promising: { color: 'text-blue-400', icon: TrendingUp, label: 'Promising' },
  mixed: { color: 'text-amber-400', icon: AlertTriangle, label: 'Mixed Results' },
  marginal: { color: 'text-amber-400', icon: AlertTriangle, label: 'Marginal Edge' },
  losing: { color: 'text-red-400', icon: XCircle, label: 'Losing' },
  insufficient_data: { color: 'text-muted-foreground', icon: Clock, label: 'Insufficient Data' },
}

function ratioClass(value: number | null, healthyThreshold: number) {
  if (value == null) return 'text-muted-foreground'
  if (value >= healthyThreshold) return 'text-emerald-400'
  if (value >= 0) return 'text-amber-400'
  return 'text-red-400'
}

function drawdownClass(value: number) {
  if (value <= 10) return 'text-emerald-400'
  if (value <= 20) return 'text-amber-400'
  return 'text-red-400'
}

function MonteCarloCard({ monteCarlo }: { monteCarlo: BacktestMonteCarlo }) {
  const hasIterations = monteCarlo.iterations > 0

  return (
    <Card>
      <CardHeader>
        <CardTitle>Monte Carlo Stress</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {!hasIterations ? (
          <div className="rounded-lg border border-border/60 bg-muted/20 p-4 text-sm text-muted-foreground">
            {monteCarlo.message || 'Need more trades before sequence stress testing is meaningful.'}
          </div>
        ) : (
          <>
            <div className="grid grid-cols-2 gap-3">
              <StatCard
                label="Median Max DD"
                value={<span className={drawdownClass(monteCarlo.median_max_drawdown_pct || 0)}>{formatPercent(-(monteCarlo.median_max_drawdown_pct || 0))}</span>}
              />
              <StatCard
                label="P95 Max DD"
                value={<span className={drawdownClass(monteCarlo.p95_max_drawdown_pct || 0)}>{formatPercent(-(monteCarlo.p95_max_drawdown_pct || 0))}</span>}
              />
            </div>
            <div className="grid grid-cols-2 gap-3">
              <StatCard
                label="P(DD > 10%)"
                value={<span>{formatPercent(monteCarlo.probability_drawdown_gt_10pct || 0)}</span>}
              />
              <StatCard
                label="P(DD > 20%)"
                value={<span>{formatPercent(monteCarlo.probability_drawdown_gt_20pct || 0)}</span>}
              />
            </div>
            <p className="text-xs text-muted-foreground">
              {monteCarlo.iterations} shuffled trade paths estimate path risk, not forecast future returns.
            </p>
          </>
        )}
      </CardContent>
    </Card>
  )
}

function WalkForwardCard({
  summary,
  windows,
}: {
  summary: WalkForwardSummary
  windows: WalkForwardWindow[] | null | undefined
}) {
  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between gap-3">
          <CardTitle>Walk-Forward Validation</CardTitle>
          <Badge
            variant={
              summary.verdict === 'robust'
                ? 'success'
                : summary.verdict === 'fragile'
                  ? 'destructive'
                  : 'warning'
            }
          >
            {summary.verdict.replace('_', ' ')}
          </Badge>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        {summary.message && (
          <div className="rounded-lg border border-border/60 bg-muted/20 p-4 text-sm text-muted-foreground">
            {summary.message}
          </div>
        )}

        {summary.windows > 0 && (
          <>
            <div className="grid grid-cols-2 gap-3">
              <StatCard label="Robustness" value={<span>{formatPercent(summary.robustness_score || 0)}</span>} />
              <StatCard label="Avg OOS Return" value={<span className={pnlClass(summary.avg_oos_return_pct || 0)}>{formatPercent(summary.avg_oos_return_pct || 0)}</span>} />
            </div>
            <div className="grid grid-cols-2 gap-3">
              <StatCard label="Median OOS Sharpe" value={<span className={ratioClass(summary.median_oos_sharpe ?? null, 1)}>{summary.median_oos_sharpe?.toFixed(2) ?? '—'}</span>} />
              <StatCard label="Worst OOS DD" value={<span className={drawdownClass(summary.worst_oos_max_dd || 0)}>{formatPercent(-(summary.worst_oos_max_dd || 0))}</span>} />
            </div>
            <div className="grid grid-cols-3 gap-3 text-xs text-muted-foreground">
              <div className="rounded-lg border border-border/60 bg-muted/20 p-3">
                <p className="font-medium text-foreground">{summary.profitable_windows ?? 0}</p>
                <p>Profitable windows</p>
              </div>
              <div className="rounded-lg border border-border/60 bg-muted/20 p-3">
                <p className="font-medium text-foreground">{summary.positive_sharpe_windows ?? 0}</p>
                <p>Positive Sharpe windows</p>
              </div>
              <div className="rounded-lg border border-border/60 bg-muted/20 p-3">
                <p className="font-medium text-foreground">{summary.controlled_drawdown_windows ?? 0}</p>
                <p>Controlled DD windows</p>
              </div>
            </div>
          </>
        )}

        {!!windows?.length && (
          <div className="rounded-lg border border-border/60">
            <div className="grid grid-cols-4 border-b border-border/60 px-3 py-2 text-[11px] uppercase tracking-wide text-muted-foreground">
              <span>Window</span>
              <span>OOS Return</span>
              <span>Sharpe</span>
              <span>Max DD</span>
            </div>
            {windows.slice(0, 4).map((windowResult) => (
              <div key={windowResult.window} className="grid grid-cols-4 px-3 py-2 text-sm">
                <span>#{windowResult.window}</span>
                <span className={pnlClass(windowResult.oos_return_pct)}>{formatPercent(windowResult.oos_return_pct)}</span>
                <span className={ratioClass(windowResult.oos_sharpe, 1)}>{windowResult.oos_sharpe.toFixed(2)}</span>
                <span className={drawdownClass(windowResult.oos_max_dd)}>{formatPercent(-windowResult.oos_max_dd)}</span>
              </div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  )
}

function EquityCurveCard({ result }: { result: BacktestResult }) {
  const equityCurve = result.equity_curve

  if (!equityCurve.length) return null

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between gap-3">
          <CardTitle>Equity Curve</CardTitle>
          <div className="text-xs text-muted-foreground">
            {result.strategy} on {result.ticker} from {result.from} to {result.to}
          </div>
        </div>
      </CardHeader>
      <CardContent>
        <ResponsiveContainer width="100%" height={260}>
          <AreaChart data={equityCurve} margin={{ top: 4, right: 4, left: 0, bottom: 0 }}>
            <defs>
              <linearGradient id="eqG" x1="0" y1="0" x2="0" y2="1">
                <stop offset="5%" stopColor="hsl(var(--primary))" stopOpacity={0.2} />
                <stop offset="95%" stopColor="hsl(var(--primary))" stopOpacity={0} />
              </linearGradient>
            </defs>
            <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
            <XAxis
              dataKey="time"
              tick={{ fontSize: 9, fill: 'hsl(var(--muted-foreground))' }}
              tickFormatter={(value: string) => value.slice(0, 10)}
              interval={Math.max(1, Math.floor(equityCurve.length / 6))}
            />
            <YAxis tick={{ fontSize: 10, fill: 'hsl(var(--muted-foreground))' }} tickFormatter={(value: number) => `$${value.toFixed(0)}`} />
            <ReferenceLine
              y={equityCurve[0]?.equity}
              stroke="hsl(var(--border))"
              strokeDasharray="4 4"
              label={{ value: 'Start', fill: 'hsl(var(--muted-foreground))', fontSize: 10 }}
            />
            <Tooltip
              contentStyle={{ background: 'hsl(var(--card))', border: '1px solid hsl(var(--border))', borderRadius: 8, fontSize: 11 }}
              formatter={(value: number) => [formatCurrency(value), 'Equity']}
              labelFormatter={(label: string) => label.slice(0, 10)}
            />
            <Area type="monotone" dataKey="equity" stroke="hsl(var(--primary))" fill="url(#eqG)" strokeWidth={1.5} dot={false} />
          </AreaChart>
        </ResponsiveContainer>
      </CardContent>
    </Card>
  )
}

function TradesCard({ result }: { result: BacktestResult }) {
  const trades = result.trades

  if (!trades.length) return null

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between gap-3">
          <CardTitle>Simulated Trades ({trades.length})</CardTitle>
          <span className="text-xs text-muted-foreground">Showing the first 50 trades with modeled friction.</span>
        </div>
      </CardHeader>
      <CardContent>
        <div className="overflow-x-auto">
          <table className="w-full data-table">
            <thead>
              <tr>
                <th>Entry</th>
                <th>Exit</th>
                <th>Qty</th>
                <th>P&amp;L</th>
                <th>Exit Reason</th>
                <th>MFE</th>
                <th>MAE</th>
              </tr>
            </thead>
            <tbody>
              {trades.slice(0, 50).map((trade) => (
                <tr key={trade.id}>
                  <td className="text-xs">
                    <div>{trade.entry_time.slice(0, 16).replace('T', ' ')}</div>
                    <div className="text-muted-foreground">{formatCurrency(trade.entry_price)}</div>
                  </td>
                  <td className="text-xs">
                    <div>{trade.exit_time.slice(0, 16).replace('T', ' ')}</div>
                    <div className="text-muted-foreground">{formatCurrency(trade.exit_price)}</div>
                  </td>
                  <td className="tabular-nums">{trade.quantity.toFixed(2)}</td>
                  <td className={cn('tabular-nums font-medium', pnlClass(trade.pnl))}>
                    <div>{formatCurrency(trade.pnl)}</div>
                    <div className="text-xs text-muted-foreground">{formatPercent(trade.pnl_pct)}</div>
                  </td>
                  <td>
                    <Badge
                      variant={
                        trade.exit_reason === 'take_profit'
                          ? 'success'
                          : trade.exit_reason === 'stop'
                            ? 'destructive'
                            : 'outline'
                      }
                      className="text-[10px]"
                    >
                      {trade.exit_reason}
                    </Badge>
                  </td>
                  <td className="tabular-nums text-emerald-400">{formatCurrency(trade.mfe)}</td>
                  <td className="tabular-nums text-red-400">{formatCurrency(trade.mae)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </CardContent>
    </Card>
  )
}

function PortfolioWeightsCard({ result }: { result: PortfolioBacktestResult }) {
  const weights = Object.entries(result.latest_weights)

  if (!weights.length) return null

  return (
    <Card>
      <CardHeader>
        <CardTitle>Latest Allocation</CardTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        {weights.sort((a, b) => b[1] - a[1]).map(([ticker, weight]) => (
          <div key={ticker} className="flex items-center justify-between rounded-lg border border-border/60 bg-muted/20 px-3 py-2 text-sm">
            <span className="font-medium">{ticker}</span>
            <span className="tabular-nums text-muted-foreground">{formatPercent(weight * 100)}</span>
          </div>
        ))}
      </CardContent>
    </Card>
  )
}

function PortfolioTradesCard({ result }: { result: PortfolioBacktestResult }) {
  if (!result.trades.length) return null

  return (
    <Card>
      <CardHeader>
        <CardTitle>Recent Rebalance Trades</CardTitle>
      </CardHeader>
      <CardContent>
        <div className="overflow-x-auto">
          <table className="w-full data-table">
            <thead>
              <tr>
                <th>Date</th>
                <th>Ticker</th>
                <th>Side</th>
                <th>Shares</th>
                <th>Notional</th>
                <th>Cost</th>
              </tr>
            </thead>
            <tbody>
              {result.trades.slice(-30).reverse().map((trade) => (
                <tr key={`${trade.date}-${trade.ticker}-${trade.side}-${trade.notional}`}>
                  <td>{trade.date}</td>
                  <td>{trade.ticker}</td>
                  <td className={trade.side === 'buy' ? 'text-emerald-400' : 'text-red-400'}>{trade.side}</td>
                  <td className="tabular-nums">{trade.shares.toFixed(2)}</td>
                  <td className="tabular-nums">{formatCurrency(trade.notional)}</td>
                  <td className="tabular-nums text-muted-foreground">{formatCurrency(trade.cost)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </CardContent>
    </Card>
  )
}

function PortfolioEquityCurveCard({ result }: { result: PortfolioBacktestResult }) {
  if (!result.equity_curve.length) return null

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between gap-3">
          <CardTitle>Portfolio Equity Curve</CardTitle>
          <div className="text-xs text-muted-foreground">{result.universe.join(', ')}</div>
        </div>
      </CardHeader>
      <CardContent>
        <ResponsiveContainer width="100%" height={260}>
          <AreaChart data={result.equity_curve} margin={{ top: 4, right: 4, left: 0, bottom: 0 }}>
            <defs>
              <linearGradient id="portfolioEq" x1="0" y1="0" x2="0" y2="1">
                <stop offset="5%" stopColor="hsl(162 73% 46%)" stopOpacity={0.24} />
                <stop offset="95%" stopColor="hsl(162 73% 46%)" stopOpacity={0} />
              </linearGradient>
            </defs>
            <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
            <XAxis
              dataKey="date"
              tick={{ fontSize: 9, fill: 'hsl(var(--muted-foreground))' }}
              interval={Math.max(1, Math.floor(result.equity_curve.length / 6))}
            />
            <YAxis tick={{ fontSize: 10, fill: 'hsl(var(--muted-foreground))' }} tickFormatter={(value: number) => `$${value.toFixed(0)}`} />
            <Tooltip
              contentStyle={{ background: 'hsl(var(--card))', border: '1px solid hsl(var(--border))', borderRadius: 8, fontSize: 11 }}
              formatter={(value: number) => [formatCurrency(value), 'Equity']}
            />
            <Area type="monotone" dataKey="equity" stroke="hsl(162 73% 46%)" fill="url(#portfolioEq)" strokeWidth={1.5} dot={false} />
          </AreaChart>
        </ResponsiveContainer>
      </CardContent>
    </Card>
  )
}

export default function BacktestPage() {
  const [status, setStatus] = useState<JobStatus>('idle')
  const [jobId, setJobId] = useState<string | null>(null)
  const [job, setJob] = useState<BacktestJob | null>(null)
  const [strategies, setStrategies] = useState<BacktestStrategyInfo[]>([])
  const [portfolioStatus, setPortfolioStatus] = useState<JobStatus>('idle')
  const [portfolioJobId, setPortfolioJobId] = useState<string | null>(null)
  const [portfolioJob, setPortfolioJob] = useState<PortfolioBacktestJob | null>(null)
  const [portfolioStrategies, setPortfolioStrategies] = useState<PortfolioBacktestStrategyInfo[]>([])
  const [portfolioForm, setPortfolioForm] = useState<PortfolioFormValues>({
    tickers: 'SPY, QQQ, IWM, EFA, GLD',
    strategy_type: 'buy_hold_core',
    from_date: new Date(Date.now() - 365 * 5 * 86400000).toISOString().split('T')[0],
    to_date: new Date(Date.now() - 86400000).toISOString().split('T')[0],
    initial_capital: '25000',
  })

  const {
    register,
    handleSubmit,
    watch,
  } = useForm<BacktestFormValues>({
    defaultValues: {
      ticker: 'AAPL',
      strategy_type: 'orb',
      from_date: new Date(Date.now() - 180 * 86400000).toISOString().split('T')[0],
      to_date: new Date(Date.now() - 86400000).toISOString().split('T')[0],
      initial_capital: '10000',
      run_walk_forward: false,
    },
  })

  const selectedStrategyType = watch('strategy_type')
  const runWalkForward = watch('run_walk_forward')
  const selectedStrategy = strategies.find((strategy) => strategy.type === selectedStrategyType)

  useEffect(() => {
    let active = true

    void api.listBacktestStrategies()
      .then((data) => {
        if (active) setStrategies(data)
      })
      .catch(() => {
        toast.error('Unable to load backtest strategies.')
      })

    void api.listPortfolioBacktestStrategies()
      .then((data) => {
        if (active) setPortfolioStrategies(data)
      })
      .catch(() => {
        toast.error('Unable to load portfolio research strategies.')
      })

    return () => {
      active = false
    }
  }, [])

  useEffect(() => {
    if (!jobId || status !== 'running') return

    let active = true
    const interval = window.setInterval(() => {
      void api.getBacktestResult(jobId)
        .then((data) => {
          if (!active) return
          if (data.status === 'complete') {
            setJob(data)
            setStatus('complete')
            window.clearInterval(interval)
            toast.success('Backtest complete.')
          } else if (data.status === 'error') {
            setJob(data)
            setStatus('error')
            window.clearInterval(interval)
            toast.error(data.error || 'Backtest failed.')
          }
        })
        .catch(() => {
          if (!active) return
        })
    }, 2000)

    return () => {
      active = false
      window.clearInterval(interval)
    }
  }, [jobId, status])

  useEffect(() => {
    if (!portfolioJobId || portfolioStatus !== 'running') return

    let active = true
    const interval = window.setInterval(() => {
      void api.getPortfolioBacktestResult(portfolioJobId)
        .then((data) => {
          if (!active) return
          if (data.status === 'complete') {
            setPortfolioJob(data)
            setPortfolioStatus('complete')
            window.clearInterval(interval)
            toast.success('Portfolio backtest complete.')
          } else if (data.status === 'error') {
            setPortfolioJob(data)
            setPortfolioStatus('error')
            window.clearInterval(interval)
            toast.error(data.error || 'Portfolio backtest failed.')
          }
        })
        .catch(() => {
          if (!active) return
        })
    }, 2000)

    return () => {
      active = false
      window.clearInterval(interval)
    }
  }, [portfolioJobId, portfolioStatus])

  const onSubmit = async (values: BacktestFormValues) => {
    setStatus('running')
    setJob(null)

    try {
      const response = await api.runBacktest({
        ticker: values.ticker.toUpperCase(),
        strategy_type: values.strategy_type,
        from_date: values.from_date,
        to_date: values.to_date,
        initial_capital: Number(values.initial_capital),
        run_walk_forward: values.run_walk_forward,
      })
      setJobId(response.job_id)
      toast.success('Backtest started. Polling for results.')
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Backtest failed.'
      setStatus('error')
      setJob({ status: 'error', error: message })
      toast.error(message)
    }
  }

  const onSubmitPortfolio = async () => {
    setPortfolioStatus('running')
    setPortfolioJob(null)

    try {
      const response = await api.runPortfolioBacktest({
        tickers: portfolioForm.tickers
          .split(',')
          .map((ticker) => ticker.trim().toUpperCase())
          .filter(Boolean),
        strategy_type: portfolioForm.strategy_type,
        from_date: portfolioForm.from_date,
        to_date: portfolioForm.to_date,
        initial_capital: Number(portfolioForm.initial_capital),
      })
      setPortfolioJobId(response.job_id)
      toast.success('Portfolio backtest started.')
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Portfolio backtest failed.'
      setPortfolioStatus('error')
      setPortfolioJob({ status: 'error', error: message })
      toast.error(message)
    }
  }

  const result = job?.result as BacktestResult | undefined
  const interpretation = job?.interpretation
  const verdict = interpretation?.verdict || 'insufficient_data'
  const verdictStyle = VERDICT_STYLES[verdict] || VERDICT_STYLES.insufficient_data
  const portfolioResult = portfolioJob?.result as PortfolioBacktestResult | undefined
  const portfolioInterpretation = portfolioJob?.interpretation
  const portfolioVerdict = portfolioInterpretation?.verdict || 'insufficient_data'
  const portfolioVerdictStyle = VERDICT_STYLES[portfolioVerdict] || VERDICT_STYLES.insufficient_data
  const selectedPortfolioStrategy = portfolioStrategies.find((strategy) => strategy.type === portfolioForm.strategy_type)

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-xl font-semibold tracking-tight">Backtest Engine</h2>
        <p className="mt-0.5 text-xs text-muted-foreground">
          Research strategies in demo-first mode with execution costs, drawdown controls, and walk-forward checks.
        </p>
      </div>

      <div className="flex items-start gap-3 rounded-lg border border-amber-500/20 bg-amber-500/10 p-4 text-amber-400">
        <ShieldAlert className="mt-0.5 h-4 w-4 flex-shrink-0" />
        <div className="text-sm">
          <p className="font-medium">Risk-first rule</p>
          <p className="mt-0.5 text-amber-400/80">
            Past performance does not guarantee future results. Prefer no-trade over low-confidence trades, and require demo or paper validation before any live use.
          </p>
        </div>
      </div>

      <div className="grid gap-6 lg:grid-cols-3">
        <Card>
          <CardHeader>
            <CardTitle>Configure Backtest</CardTitle>
          </CardHeader>
          <CardContent>
            <form onSubmit={handleSubmit(onSubmit)} className="space-y-4">
              <div className="space-y-1.5">
                <Label htmlFor="ticker">Symbol</Label>
                <Input id="ticker" placeholder="AAPL" className="uppercase" {...register('ticker', { required: true })} />
              </div>

              <div className="space-y-1.5">
                <Label htmlFor="strategy_type">Strategy</Label>
                <select
                  id="strategy_type"
                  className="flex h-9 w-full rounded-md border border-input bg-background px-3 py-1 text-sm shadow-sm transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                  {...register('strategy_type')}
                >
                  {strategies.map((strategy) => (
                    <option key={strategy.type} value={strategy.type} className="bg-background">
                      {strategy.label}
                    </option>
                  ))}
                  {!strategies.length && (
                    <option value="orb" className="bg-background">
                      Opening Range Breakout
                    </option>
                  )}
                </select>
              </div>

              <div className="space-y-1.5">
                <Label htmlFor="from_date">From Date</Label>
                <Input id="from_date" type="date" {...register('from_date', { required: true })} />
              </div>

              <div className="space-y-1.5">
                <Label htmlFor="to_date">To Date</Label>
                <Input id="to_date" type="date" {...register('to_date', { required: true })} />
              </div>

              <div className="space-y-1.5">
                <Label htmlFor="initial_capital">Initial Capital</Label>
                <Input id="initial_capital" type="number" min="1000" step="100" {...register('initial_capital')} />
              </div>

              <label className="flex items-start gap-3 rounded-lg border border-border/60 bg-muted/20 p-3">
                <input type="checkbox" className="mt-1 h-4 w-4 rounded" {...register('run_walk_forward')} />
                <span className="space-y-1">
                  <span className="block text-sm font-medium">Run walk-forward validation</span>
                  <span className="block text-xs text-muted-foreground">
                    Slower, but much better for spotting overfit results across rolling out-of-sample windows.
                  </span>
                </span>
              </label>

              <Button type="submit" className="w-full" loading={status === 'running'} disabled={status === 'running'}>
                <Play className="h-3.5 w-3.5" />
                {status === 'running' ? 'Running...' : 'Run Backtest'}
              </Button>
            </form>
          </CardContent>
        </Card>

        <div className="space-y-4 lg:col-span-2">
          <Card>
            <CardHeader>
              <div className="flex items-center justify-between gap-3">
                <CardTitle>Research Context</CardTitle>
                {selectedStrategy && <Badge variant="outline">{selectedStrategy.label}</Badge>}
              </div>
            </CardHeader>
            <CardContent className="space-y-3 text-sm">
              <p className="text-muted-foreground">
                {selectedStrategy?.description || 'Choose a strategy to compare its market hypothesis and risk profile.'}
              </p>
              <div className="grid gap-3 sm:grid-cols-2">
                <div className="rounded-lg border border-border/60 bg-muted/20 p-4">
                  <p className="text-xs uppercase tracking-wide text-muted-foreground">Execution realism</p>
                  <p className="mt-2 text-sm text-foreground">
                    Market-order fills include spread and impact assumptions, plus commission tracking and trade journaling.
                  </p>
                </div>
                <div className="rounded-lg border border-border/60 bg-muted/20 p-4">
                  <p className="text-xs uppercase tracking-wide text-muted-foreground">Validation stance</p>
                  <p className="mt-2 text-sm text-foreground">
                    {runWalkForward
                      ? 'This run will add rolling out-of-sample validation on top of the main backtest.'
                      : 'This run will execute a single historical simulation. Turn on walk-forward for stronger evidence.'}
                  </p>
                </div>
              </div>
            </CardContent>
          </Card>

          {status === 'running' && (
            <Card>
              <CardContent className="flex flex-col items-center gap-3 p-8">
                <Spinner className="h-8 w-8 text-primary" />
                <p className="text-sm font-medium">Running backtest...</p>
                <p className="text-xs text-muted-foreground">
                  Fetching historical data, simulating fills, and compiling risk-adjusted metrics.
                </p>
              </CardContent>
            </Card>
          )}

          {status === 'error' && (
            <Card className="border-red-500/30">
              <CardContent className="p-6">
                <div className="flex items-start gap-3">
                  <XCircle className="h-5 w-5 flex-shrink-0 text-red-400" />
                  <div>
                    <p className="text-sm font-medium text-red-400">Backtest Failed</p>
                    <p className="mt-1 text-xs text-muted-foreground">{job?.error || 'Unknown error'}</p>
                  </div>
                </div>
              </CardContent>
            </Card>
          )}

          {status === 'complete' && result && (
            <>
              {interpretation && (
                <Card
                  className={cn(
                    'border',
                    verdict === 'strong'
                      ? 'border-emerald-500/30'
                      : verdict === 'losing'
                        ? 'border-red-500/30'
                        : 'border-amber-500/30',
                  )}
                >
                  <CardContent className="flex items-start gap-3 p-4">
                    <verdictStyle.icon className={cn('mt-0.5 h-5 w-5 flex-shrink-0', verdictStyle.color)} />
                    <div>
                      <p className={cn('text-sm font-semibold', verdictStyle.color)}>{verdictStyle.label}</p>
                      <p className="mt-1 text-xs text-muted-foreground">{interpretation.summary}</p>
                      {!!interpretation.warnings?.length && (
                        <div className="mt-2 space-y-1">
                          {interpretation.warnings.map((warning) => (
                            <div key={warning} className="flex items-start gap-1 text-xs text-amber-400">
                              <AlertTriangle className="mt-0.5 h-3 w-3 flex-shrink-0" />
                              <span>{warning}</span>
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  </CardContent>
                </Card>
              )}

              <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
                <StatCard
                  label="Net Return"
                  value={<span className={pnlClass(result.total_return_pct)}>{formatPercent(result.total_return_pct)}</span>}
                  sub={formatCurrency(result.net_pnl)}
                  trend={result.total_return_pct > 0 ? 'up' : result.total_return_pct < 0 ? 'down' : 'neutral'}
                />
                <StatCard
                  label="Gross Return"
                  value={<span className={pnlClass(result.gross_return_pct)}>{formatPercent(result.gross_return_pct)}</span>}
                  sub={formatCurrency(result.gross_pnl)}
                />
                <StatCard
                  label="Sharpe Ratio"
                  value={<span className={ratioClass(result.sharpe_ratio, 1)}>{result.sharpe_ratio?.toFixed(2) ?? '—'}</span>}
                  sub="Target ≥ 1.0"
                />
                <StatCard
                  label="Max Drawdown"
                  value={<span className={drawdownClass(result.max_drawdown_pct)}>{formatPercent(-result.max_drawdown_pct)}</span>}
                  sub={`${result.max_drawdown_duration_days} days`}
                />
              </div>

              <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
                <StatCard
                  label="Alpha vs Benchmark"
                  value={<span className={pnlClass(result.alpha_vs_benchmark_pct)}>{formatPercent(result.alpha_vs_benchmark_pct)}</span>}
                  sub={`Benchmark ${formatPercent(result.benchmark_return_pct)}`}
                />
                <StatCard
                  label="Calmar Ratio"
                  value={<span className={ratioClass(result.calmar_ratio, 0.7)}>{result.calmar_ratio?.toFixed(2) ?? '—'}</span>}
                  sub="Return / drawdown"
                />
                <StatCard label="Exposure" value={<span>{formatPercent(result.exposure_pct)}</span>} sub={`${result.total_trades} trades`} />
                <StatCard label="Turnover" value={<span>{formatPercent(result.turnover_pct)}</span>} sub="Notional traded" />
              </div>

              <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
                <StatCard
                  label="Profit Factor"
                  value={<span className={ratioClass(result.profit_factor, 1.5)}>{result.profit_factor.toFixed(2)}</span>}
                  sub={`${result.winning_trades}W / ${result.losing_trades}L`}
                />
                <StatCard
                  label="Expectancy"
                  value={<span className={pnlClass(result.expectancy)}>{formatCurrency(result.expectancy)}</span>}
                  sub={formatPercent(result.expectancy_pct)}
                />
                <StatCard label="Slippage" value={<span className="text-amber-400">{formatCurrency(result.total_slippage_cost)}</span>} sub="Execution friction" />
                <StatCard label="Commission" value={<span>{formatCurrency(result.total_commission_cost)}</span>} sub={`Avg hold ${result.avg_holding_bars.toFixed(1)} bars`} />
              </div>
            </>
          )}
        </div>
      </div>

      {status === 'complete' && result && (
        <div className={cn('grid gap-6', job?.walk_forward_summary ? 'xl:grid-cols-2' : '')}>
          {job?.walk_forward_summary && (
            <WalkForwardCard summary={job.walk_forward_summary} windows={job.walk_forward} />
          )}
          <MonteCarloCard monteCarlo={result.monte_carlo} />
        </div>
      )}

      {status === 'complete' && result && <EquityCurveCard result={result} />}

      {status === 'complete' && result && <TradesCard result={result} />}

      <div className="space-y-6 border-t border-border/60 pt-6">
        <div>
          <h3 className="text-xl font-semibold tracking-tight">Portfolio Research Lab</h3>
          <p className="mt-0.5 text-xs text-muted-foreground">
            Five lower-friction, Trading 212-friendly portfolio strategies built on validated daily price data rather than unsupported fundamentals proxies.
          </p>
        </div>

        <div className="grid gap-6 lg:grid-cols-3">
          <Card>
            <CardHeader>
              <CardTitle>Configure Portfolio Test</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="space-y-1.5">
                <Label htmlFor="pf_tickers">Universe</Label>
                <Input
                  id="pf_tickers"
                  value={portfolioForm.tickers}
                  onChange={(event) => setPortfolioForm((current) => ({ ...current, tickers: event.target.value }))}
                  placeholder="SPY, QQQ, IWM, EFA, GLD"
                />
                <p className="text-[11px] text-muted-foreground">
                  Comma-separated tickers. Keep the universe liquid and reasonably compact for cleaner research.
                </p>
              </div>

              <div className="space-y-1.5">
                <Label htmlFor="pf_strategy_type">Strategy</Label>
                <select
                  id="pf_strategy_type"
                  className="flex h-9 w-full rounded-md border border-input bg-background px-3 py-1 text-sm shadow-sm transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                  value={portfolioForm.strategy_type}
                  onChange={(event) =>
                    setPortfolioForm((current) => ({
                      ...current,
                      strategy_type: event.target.value as PortfolioBacktestStrategyType,
                    }))}
                >
                  {portfolioStrategies.map((strategy) => (
                    <option key={strategy.type} value={strategy.type} className="bg-background">
                      {strategy.label}
                    </option>
                  ))}
                </select>
              </div>

              <div className="space-y-1.5">
                <Label htmlFor="pf_from_date">From Date</Label>
                <Input
                  id="pf_from_date"
                  type="date"
                  value={portfolioForm.from_date}
                  onChange={(event) => setPortfolioForm((current) => ({ ...current, from_date: event.target.value }))}
                />
              </div>

              <div className="space-y-1.5">
                <Label htmlFor="pf_to_date">To Date</Label>
                <Input
                  id="pf_to_date"
                  type="date"
                  value={portfolioForm.to_date}
                  onChange={(event) => setPortfolioForm((current) => ({ ...current, to_date: event.target.value }))}
                />
              </div>

              <div className="space-y-1.5">
                <Label htmlFor="pf_initial_capital">Initial Capital</Label>
                <Input
                  id="pf_initial_capital"
                  type="number"
                  min="1000"
                  step="500"
                  value={portfolioForm.initial_capital}
                  onChange={(event) => setPortfolioForm((current) => ({ ...current, initial_capital: event.target.value }))}
                />
              </div>

              <Button className="w-full" onClick={onSubmitPortfolio} loading={portfolioStatus === 'running'} disabled={portfolioStatus === 'running'}>
                <Play className="h-3.5 w-3.5" />
                {portfolioStatus === 'running' ? 'Running...' : 'Run Portfolio Backtest'}
              </Button>
            </CardContent>
          </Card>

          <div className="space-y-4 lg:col-span-2">
            <Card>
              <CardHeader>
                <div className="flex items-center justify-between gap-3">
                  <CardTitle>Research Pack Context</CardTitle>
                  {selectedPortfolioStrategy && <Badge variant="outline">{selectedPortfolioStrategy.label}</Badge>}
                </div>
              </CardHeader>
              <CardContent className="space-y-3 text-sm">
                <p className="text-muted-foreground">
                  {selectedPortfolioStrategy?.description || 'Select a portfolio strategy to review its rationale and turnover profile.'}
                </p>
                {selectedPortfolioStrategy && (
                  <div className="grid gap-3 sm:grid-cols-2">
                    <div className="rounded-lg border border-border/60 bg-muted/20 p-4">
                      <p className="text-xs uppercase tracking-wide text-muted-foreground">Why This Is Included</p>
                      <p className="mt-2 text-sm text-foreground">{selectedPortfolioStrategy.rationale}</p>
                    </div>
                    <div className="rounded-lg border border-border/60 bg-muted/20 p-4">
                      <p className="text-xs uppercase tracking-wide text-muted-foreground">Rebalance Rhythm</p>
                      <p className="mt-2 text-sm text-foreground">
                        {selectedPortfolioStrategy.rebalance_frequency} with at least {selectedPortfolioStrategy.min_history_bars} historical bars before activation.
                      </p>
                    </div>
                  </div>
                )}
              </CardContent>
            </Card>

            {portfolioStatus === 'running' && (
              <Card>
                <CardContent className="flex flex-col items-center gap-3 p-8">
                  <Spinner className="h-8 w-8 text-primary" />
                  <p className="text-sm font-medium">Running portfolio backtest...</p>
                  <p className="text-xs text-muted-foreground">
                    Aligning daily bars, simulating rebalances, and comparing against an equal-weight benchmark.
                  </p>
                </CardContent>
              </Card>
            )}

            {portfolioStatus === 'error' && (
              <Card className="border-red-500/30">
                <CardContent className="p-6">
                  <div className="flex items-start gap-3">
                    <XCircle className="h-5 w-5 flex-shrink-0 text-red-400" />
                    <div>
                      <p className="text-sm font-medium text-red-400">Portfolio Backtest Failed</p>
                      <p className="mt-1 text-xs text-muted-foreground">{portfolioJob?.error || 'Unknown error'}</p>
                    </div>
                  </div>
                </CardContent>
              </Card>
            )}

            {portfolioStatus === 'complete' && portfolioResult && (
              <>
                {portfolioInterpretation && (
                  <Card
                    className={cn(
                      'border',
                      portfolioVerdict === 'strong'
                        ? 'border-emerald-500/30'
                        : portfolioVerdict === 'losing'
                          ? 'border-red-500/30'
                          : 'border-amber-500/30',
                    )}
                  >
                    <CardContent className="flex items-start gap-3 p-4">
                      <portfolioVerdictStyle.icon className={cn('mt-0.5 h-5 w-5 flex-shrink-0', portfolioVerdictStyle.color)} />
                      <div>
                        <p className={cn('text-sm font-semibold', portfolioVerdictStyle.color)}>{portfolioVerdictStyle.label}</p>
                        <p className="mt-1 text-xs text-muted-foreground">{portfolioInterpretation.summary}</p>
                        {!!portfolioInterpretation.warnings?.length && (
                          <div className="mt-2 space-y-1">
                            {portfolioInterpretation.warnings.map((warning) => (
                              <div key={warning} className="flex items-start gap-1 text-xs text-amber-400">
                                <AlertTriangle className="mt-0.5 h-3 w-3 flex-shrink-0" />
                                <span>{warning}</span>
                              </div>
                            ))}
                          </div>
                        )}
                      </div>
                    </CardContent>
                  </Card>
                )}

                <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
                  <StatCard label="Total Return" value={<span className={pnlClass(portfolioResult.total_return_pct)}>{formatPercent(portfolioResult.total_return_pct)}</span>} sub={formatCurrency(portfolioResult.final_capital)} />
                  <StatCard label="Annualised" value={<span className={pnlClass(portfolioResult.annualised_return_pct)}>{formatPercent(portfolioResult.annualised_return_pct)}</span>} sub={portfolioResult.strategy} />
                  <StatCard label="Alpha vs Benchmark" value={<span className={pnlClass(portfolioResult.alpha_vs_benchmark_pct)}>{formatPercent(portfolioResult.alpha_vs_benchmark_pct)}</span>} sub={`${portfolioResult.benchmark_name} ${formatPercent(portfolioResult.benchmark_return_pct)}`} />
                  <StatCard label="Max Drawdown" value={<span className={drawdownClass(portfolioResult.max_drawdown_pct)}>{formatPercent(-portfolioResult.max_drawdown_pct)}</span>} sub={`Turnover ${formatPercent(portfolioResult.turnover_pct)}`} />
                </div>

                <div className="grid gap-4 lg:grid-cols-3">
                  <Card>
                    <CardHeader>
                      <CardTitle>Risk-Adjusted View</CardTitle>
                    </CardHeader>
                    <CardContent className="grid grid-cols-2 gap-3">
                      <StatCard label="Sharpe" value={<span className={ratioClass(portfolioResult.sharpe_ratio, 1)}>{portfolioResult.sharpe_ratio?.toFixed(2) ?? '—'}</span>} />
                      <StatCard label="Sortino" value={<span className={ratioClass(portfolioResult.sortino_ratio, 1.2)}>{portfolioResult.sortino_ratio?.toFixed(2) ?? '—'}</span>} />
                      <StatCard label="Calmar" value={<span className={ratioClass(portfolioResult.calmar_ratio, 0.75)}>{portfolioResult.calmar_ratio?.toFixed(2) ?? '—'}</span>} />
                      <StatCard label="Avg Exposure" value={<span>{formatPercent(portfolioResult.avg_exposure_pct)}</span>} />
                    </CardContent>
                  </Card>
                  <PortfolioWeightsCard result={portfolioResult} />
                  <Card>
                    <CardHeader>
                      <CardTitle>Operational Footprint</CardTitle>
                    </CardHeader>
                    <CardContent className="space-y-3">
                      <StatCard label="Rebalances" value={<span>{portfolioResult.rebalance_count}</span>} />
                      <StatCard label="Trade Legs" value={<span>{portfolioResult.total_trades}</span>} />
                      <div className="rounded-lg border border-border/60 bg-muted/20 p-4 text-xs text-muted-foreground">
                        {portfolioResult.rationale}
                      </div>
                    </CardContent>
                  </Card>
                </div>

                <PortfolioEquityCurveCard result={portfolioResult} />
                <PortfolioTradesCard result={portfolioResult} />
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
