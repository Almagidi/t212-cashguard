'use client'
import { useExecutionQualityReport, usePerformanceReport, useTradesReport } from '@/hooks/use-api'
import { Card, CardHeader, CardTitle, CardContent, TerminalCard, StatCard, Spinner, EmptyState, Button, Badge, PageHeader } from '@/components/ui'
import { QueryError } from '@/components/shared/query-error'
import { executionQualityClass, formatCurrency, formatDate, orderStatusBg, pnlClass, cn } from '@/lib/utils'
import { AreaChart, Area, BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, ReferenceLine } from 'recharts'
import { AlertTriangle, BarChart2, Download, Gauge, TimerReset, TrendingUp, TrendingDown } from 'lucide-react'

function rate(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return `${(value * 100).toFixed(1)}%`
}

function ms(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  if (value >= 1000) return `${(value / 1000).toFixed(2)}s`
  return `${value.toFixed(0)}ms`
}

function executionStatusVariant(status: string): 'success' | 'warning' | 'destructive' | 'outline' {
  if (status === 'ok') return 'success'
  if (status === 'watch') return 'warning'
  if (status === 'degraded') return 'destructive'
  return 'outline'
}

export default function ReportsPage() {
  const { data: perf, isLoading, isError, error, refetch } = usePerformanceReport()
  const { data: execQuality } = useExecutionQualityReport(30, false)
  const { data: trades = [] } = useTradesReport(200)

  const exportCSV = () => {
    if (!trades.length) return
    const header = 'Date,Symbol,Side,Type,Qty,Fill Price,Cash Used,Status,Dry Run'
    const rows = trades.map(t =>
      [new Date(t.created_at).toISOString(), t.ticker, t.side, t.order_type,
       t.quantity, t.avg_fill_price ?? '', t.cash_used ?? '', t.status, t.is_dry_run].join(',')
    )
    const blob = new Blob([[header, ...rows].join('\n')], { type: 'text/csv' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url; a.download = `cashguard-trades-${new Date().toISOString().split('T')[0]}.csv`; a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <div className="space-y-6">
      <PageHeader
        icon={<BarChart2 className="h-5 w-5" />}
        label="Reports"
        sub="Performance analytics and trade history"
        actions={
          <Button variant="outline" size="sm" onClick={exportCSV} disabled={!trades.length}>
            <Download className="w-3.5 h-3.5" />
            Export CSV
          </Button>
        }
      />

      {isLoading ? (
        <div className="flex items-center gap-2 text-muted-foreground text-sm"><Spinner className="w-4 h-4" />Loading...</div>
      ) : isError ? (
        <QueryError error={error} onRetry={refetch} label="performance report" />
      ) : perf ? (
        <>
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
            <TerminalCard label="Total Trades" value={perf.total_trades.toString()} variant="cyan" />
            <TerminalCard
              label="Win Rate"
              value={`${(perf.win_rate * 100).toFixed(1)}%`}
              sub={`${perf.winning_trades}W · ${perf.losing_trades}L`}
              variant={perf.win_rate >= 0.5 ? 'teal' : 'red'}
            />
            <TerminalCard
              label="Total P&L"
              value={`${perf.total_pnl >= 0 ? '+' : ''}${formatCurrency(perf.total_pnl)}`}
              variant={perf.total_pnl > 0 ? 'teal' : perf.total_pnl < 0 ? 'red' : 'cyan'}
            />
            <TerminalCard
              label="Profit Factor"
              value={perf.profit_factor.toFixed(2)}
              sub={perf.profit_factor >= 1 ? 'Profitable' : 'Unprofitable'}
              variant={perf.profit_factor >= 1 ? 'teal' : 'red'}
            />
          </div>

          <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
            <TerminalCard label="Avg Win" value={formatCurrency(perf.avg_win)} variant="teal" />
            <TerminalCard label="Avg Loss" value={formatCurrency(perf.avg_loss)} variant="red" />
            <TerminalCard label="Max Drawdown" value={formatCurrency(perf.max_drawdown)} variant="red" />
            <TerminalCard
              label="Sharpe Ratio"
              value={perf.sharpe_ratio !== null ? perf.sharpe_ratio.toFixed(2) : '—'}
              sub={perf.sharpe_ratio !== null ? 'Annualised' : 'Need more data'}
              variant={perf.sharpe_ratio !== null && perf.sharpe_ratio >= 1 ? 'teal' : 'cyan'}
            />
          </div>

          {execQuality && (
            <div className="space-y-4">
              <div className="flex items-center justify-between gap-3 flex-wrap">
                <div>
                  <h3 className="text-sm font-semibold tracking-tight">Execution Quality</h3>
                  <p className="mt-1 text-xs text-muted-foreground">{execQuality.summary.status_reason}</p>
                </div>
                <Badge variant={executionStatusVariant(execQuality.summary.status)}>
                  {execQuality.summary.status.replace(/_/g, ' ')}
                </Badge>
              </div>

              <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
                <StatCard
                  label="Execution Score"
                  icon={<Gauge className="h-4 w-4" />}
                  value={execQuality.summary.avg_score !== null
                    ? <span className={executionQualityClass(execQuality.summary.avg_score >= 75 ? 'good' : execQuality.summary.avg_score >= 60 ? 'watch' : 'degraded')}>{execQuality.summary.avg_score.toFixed(0)}</span>
                    : <span className="text-muted-foreground">—</span>}
                  sub={execQuality.summary.score_delta !== null ? `${execQuality.summary.score_delta >= 0 ? '+' : ''}${execQuality.summary.score_delta.toFixed(1)} vs prior window` : 'No prior comparison'}
                />
                <StatCard
                  label="Adverse Slippage"
                  value={<span className={execQuality.summary.avg_slippage_pct && execQuality.summary.avg_slippage_pct > 0.5 ? 'text-red-400' : 'text-muted-foreground'}>{execQuality.summary.avg_slippage_pct !== null ? `${execQuality.summary.avg_slippage_pct.toFixed(3)}%` : '—'}</span>}
                  sub={`${formatCurrency(execQuality.summary.total_slippage_value)} total cost`}
                />
                <StatCard
                  label="First Ack"
                  icon={<TimerReset className="h-4 w-4" />}
                  value={ms(execQuality.summary.avg_broker_latency_ms)}
                  sub={`Fill ${ms(execQuality.summary.avg_fill_latency_ms)}`}
                />
                <StatCard
                  label="Reject / Error"
                  icon={<AlertTriangle className="h-4 w-4" />}
                  value={<span className={execQuality.summary.reject_rate + execQuality.summary.error_rate > 0.05 ? 'text-red-400' : 'text-emerald-400'}>{rate(execQuality.summary.reject_rate + execQuality.summary.error_rate)}</span>}
                  sub={`${execQuality.summary.rejected_orders} rejected · ${execQuality.summary.error_orders} errors`}
                />
              </div>

              <Card>
                <CardHeader>
                  <CardTitle>Execution Score by Symbol / Type</CardTitle>
                </CardHeader>
                <CardContent className="p-0">
                  {execQuality.by_symbol_order_type.length === 0 ? (
                    <EmptyState title="No broker execution data" description="Demo and live broker orders will appear here." />
                  ) : (
                    <div className="overflow-x-auto scrollbar-none">
                      <table className="w-full data-table min-w-[780px]">
                        <thead>
                          <tr>
                            <th>Symbol</th>
                            <th>Type</th>
                            <th>Env</th>
                            <th className="text-right">Orders</th>
                            <th className="text-right">Fill Rate</th>
                            <th className="text-right">Score</th>
                            <th className="text-right">Avg Slip</th>
                            <th className="text-right">Ack</th>
                          </tr>
                        </thead>
                        <tbody>
                          {execQuality.by_symbol_order_type.slice(0, 12).map(row => (
                            <tr key={`${row.environment}-${row.ticker}-${row.order_type}`}>
                              <td className="font-semibold">{row.ticker}</td>
                              <td className="text-xs capitalize text-muted-foreground">{row.order_type.replace(/_/g, ' ')}</td>
                              <td><Badge variant="outline">{row.environment}</Badge></td>
                              <td className="tnum text-right">{row.order_count}</td>
                              <td className="tnum text-right">{rate(row.fill_rate)}</td>
                              <td className={cn('tnum text-right font-medium', executionQualityClass(row.avg_score === null ? 'pending' : row.avg_score >= 75 ? 'good' : row.avg_score >= 60 ? 'watch' : 'degraded'))}>
                                {row.avg_score !== null ? row.avg_score.toFixed(0) : '—'}
                              </td>
                              <td className={cn('tnum text-right', row.avg_slippage_pct && row.avg_slippage_pct > 0 ? 'text-red-400' : 'text-muted-foreground')}>
                                {row.avg_slippage_pct !== null ? `${row.avg_slippage_pct.toFixed(3)}%` : '—'}
                              </td>
                              <td className="tnum text-right text-muted-foreground">{ms(row.avg_broker_latency_ms)}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </CardContent>
              </Card>

              <div className="grid gap-6 lg:grid-cols-2">
                <Card>
                  <CardHeader><CardTitle>Reject / Cancel Patterns</CardTitle></CardHeader>
                  <CardContent className="p-0">
                    {execQuality.reject_cancel_patterns.length === 0 ? (
                      <EmptyState title="No reject patterns" description="Rejected, cancelled, and errored orders will be summarized here." />
                    ) : (
                      <div className="overflow-x-auto scrollbar-none">
                        <table className="w-full data-table">
                          <thead>
                            <tr>
                              <th>Status</th>
                              <th>Symbol</th>
                              <th className="text-right">Count</th>
                              <th>Reason</th>
                            </tr>
                          </thead>
                          <tbody>
                            {execQuality.reject_cancel_patterns.map(pattern => (
                              <tr key={`${pattern.status}-${pattern.ticker}-${pattern.order_type}-${pattern.reason}`}>
                                <td><span className={cn('inline-flex items-center text-[10px] px-2 py-0.5 rounded-full font-semibold uppercase tracking-wider', orderStatusBg(pattern.status))}>{pattern.status}</span></td>
                                <td className="font-semibold">{pattern.ticker}</td>
                                <td className="tnum text-right">{pattern.count}</td>
                                <td className="max-w-[14rem] truncate text-xs text-muted-foreground">{pattern.reason}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    )}
                  </CardContent>
                </Card>

                <Card>
                  <CardHeader><CardTitle>Worst Fills</CardTitle></CardHeader>
                  <CardContent className="p-0">
                    {execQuality.worst_orders.length === 0 ? (
                      <EmptyState title="No filled orders scored" description="Filled broker orders with expected prices will appear here." />
                    ) : (
                      <div className="overflow-x-auto scrollbar-none">
                        <table className="w-full data-table">
                          <thead>
                            <tr>
                              <th>Symbol</th>
                              <th className="text-right">Slip</th>
                              <th className="text-right">Cost</th>
                              <th className="text-right">Score</th>
                              <th>Time</th>
                            </tr>
                          </thead>
                          <tbody>
                            {execQuality.worst_orders.slice(0, 6).map(order => (
                              <tr key={order.id}>
                                <td className="font-semibold">{order.ticker}</td>
                                <td className="tnum text-right text-red-400">{order.slippage_pct !== null ? `${order.slippage_pct.toFixed(3)}%` : '—'}</td>
                                <td className="tnum text-right">{formatCurrency(order.slippage_value)}</td>
                                <td className={cn('tnum text-right font-medium', executionQualityClass(order.grade))}>{order.score !== null ? order.score.toFixed(0) : '—'}</td>
                                <td className="whitespace-nowrap text-xs text-muted-foreground">{formatDate(order.created_at)}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    )}
                  </CardContent>
                </Card>
              </div>
            </div>
          )}

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
            <Card>
              <CardHeader><CardTitle>Daily P&L</CardTitle></CardHeader>
              <CardContent>
                {perf.daily_pnl.length > 0 ? (
                  <ResponsiveContainer width="100%" height={200}>
                    <AreaChart data={perf.daily_pnl} margin={{ top: 4, right: 4, left: 0, bottom: 0 }}>
                      <defs>
                        <linearGradient id="pnlG" x1="0" y1="0" x2="0" y2="1">
                          <stop offset="5%" stopColor={perf.total_pnl >= 0 ? 'hsl(142 71% 45%)' : 'hsl(0 84% 60%)'} stopOpacity={0.2} />
                          <stop offset="95%" stopColor={perf.total_pnl >= 0 ? 'hsl(142 71% 45%)' : 'hsl(0 84% 60%)'} stopOpacity={0} />
                        </linearGradient>
                      </defs>
                      <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
                      <XAxis dataKey="date" tick={{ fontSize: 10, fill: 'hsl(var(--muted-foreground))' }} tickFormatter={d => d.slice(5)} />
                      <YAxis tick={{ fontSize: 10, fill: 'hsl(var(--muted-foreground))' }} tickFormatter={v => `$${v}`} />
                      <ReferenceLine y={0} stroke="hsl(var(--border))" strokeDasharray="4 4" />
                      <Tooltip contentStyle={{ background: 'hsl(var(--card))', border: '1px solid hsl(var(--border))', borderRadius: 8, fontSize: 12 }}
                        formatter={(v: number) => [formatCurrency(v), 'P&L']} />
                      <Area type="monotone" dataKey="pnl"
                        stroke={perf.total_pnl >= 0 ? 'hsl(142 71% 45%)' : 'hsl(0 84% 60%)'}
                        fill="url(#pnlG)" strokeWidth={1.5} />
                    </AreaChart>
                  </ResponsiveContainer>
                ) : (
                  <div className="h-[200px] flex items-center justify-center">
                    <EmptyState icon={<BarChart2 className="w-10 h-10" />} title="No P&L data yet" description="Complete some trades first." />
                  </div>
                )}
              </CardContent>
            </Card>

            <Card>
              <CardHeader><CardTitle>Win / Loss Distribution</CardTitle></CardHeader>
              <CardContent>
                {perf.total_trades > 0 ? (
                  <div className="space-y-4">
                    <ResponsiveContainer width="100%" height={140}>
                      <BarChart data={[{ name: 'Wins', value: perf.winning_trades }, { name: 'Losses', value: perf.losing_trades }]}
                        margin={{ top: 4, right: 4, left: 0, bottom: 0 }}>
                        <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
                        <XAxis dataKey="name" tick={{ fontSize: 11, fill: 'hsl(var(--muted-foreground))' }} />
                        <YAxis tick={{ fontSize: 11, fill: 'hsl(var(--muted-foreground))' }} />
                        <Tooltip contentStyle={{ background: 'hsl(var(--card))', border: '1px solid hsl(var(--border))', borderRadius: 8, fontSize: 12 }} />
                        <Bar dataKey="value" radius={[4, 4, 0, 0]}
                          fill="hsl(var(--primary))"
                          label={{ position: 'top', fontSize: 11, fill: 'hsl(var(--foreground))' }} />
                      </BarChart>
                    </ResponsiveContainer>
                    <div className="grid grid-cols-2 gap-3 text-center">
                      <div className="p-2 bg-emerald-500/10 rounded-md">
                        <p className="text-emerald-400 font-semibold">{formatCurrency(perf.avg_win)}</p>
                        <p className="text-xs text-muted-foreground mt-0.5">Avg win</p>
                      </div>
                      <div className="p-2 bg-red-500/10 rounded-md">
                        <p className="text-red-400 font-semibold">{formatCurrency(Math.abs(perf.avg_loss))}</p>
                        <p className="text-xs text-muted-foreground mt-0.5">Avg loss</p>
                      </div>
                    </div>
                  </div>
                ) : (
                  <div className="h-[200px] flex items-center justify-center">
                    <EmptyState title="No trades yet" description="Distribution appears after trades." />
                  </div>
                )}
              </CardContent>
            </Card>
          </div>
        </>
      ) : null}

      <Card>
        <CardHeader>
          <div className="flex items-center justify-between">
            <CardTitle>Trade History</CardTitle>
            <span className="text-[11px] text-muted-foreground font-medium tabular-nums">{trades.length} trades</span>
          </div>
        </CardHeader>
        <CardContent className="p-0">
          {trades.length === 0 ? (
            <EmptyState
              icon={<TrendingUp className="w-5 h-5" />}
              title="No completed trades"
              description="Filled orders appear here. Enable a strategy to start."
            />
          ) : (
            <div className="overflow-x-auto scrollbar-none">
              <table className="w-full data-table">
                <thead>
                  <tr>
                    <th>Symbol</th>
                    <th>Side</th>
                    <th>Type</th>
                    <th className="text-right">Qty</th>
                    <th className="text-right">Fill Price</th>
                    <th className="text-right">Cash Used</th>
                    <th>Status</th>
                    <th>Mode</th>
                    <th>Time</th>
                  </tr>
                </thead>
                <tbody>
                  {trades.map(t => (
                    <tr key={t.id}>
                      <td className="font-semibold">{t.ticker}</td>
                      <td>
                        <span className={cn(
                          'inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-[10px] font-bold uppercase tracking-wider border',
                          t.side === 'buy'
                            ? 'text-emerald-400 bg-emerald-500/10 border-emerald-500/20'
                            : 'text-red-400 bg-red-500/10 border-red-500/20'
                        )}>
                          {t.side === 'buy' ? <TrendingUp className="w-3 h-3" /> : <TrendingDown className="w-3 h-3" />}
                          {t.side}
                        </span>
                      </td>
                      <td className="text-muted-foreground capitalize text-xs">{t.order_type}</td>
                      <td className="tnum text-right">{Number(t.quantity).toFixed(4)}</td>
                      <td className="tnum text-right">{t.avg_fill_price ? formatCurrency(Number(t.avg_fill_price)) : <span className="text-muted-foreground/50">—</span>}</td>
                      <td className="tnum text-right text-muted-foreground">{t.cash_used ? formatCurrency(Number(t.cash_used)) : <span className="text-muted-foreground/50">—</span>}</td>
                      <td>
                        <span className={cn('inline-flex items-center text-[10px] px-2 py-0.5 rounded-full font-semibold uppercase tracking-wider', orderStatusBg(t.status))}>
                          {t.status.replace('_', ' ')}
                        </span>
                      </td>
                      <td>
                        <Badge variant={t.is_dry_run ? 'secondary' : 'info'}>
                          {t.is_dry_run ? 'dry' : 'live'}
                        </Badge>
                      </td>
                      <td className="text-muted-foreground text-xs whitespace-nowrap">{formatDate(t.created_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
