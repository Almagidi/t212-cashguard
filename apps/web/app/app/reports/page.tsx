'use client'
import { useState } from 'react'
import { usePerformanceReport, useTradesReport } from '@/hooks/use-api'
import { Card, CardHeader, CardTitle, CardContent, StatCard, Spinner, EmptyState, Button, Badge } from '@/components/ui'
import { QueryError } from '@/components/shared/query-error'
import { formatCurrency, formatDate, orderStatusBg, pnlClass, cn } from '@/lib/utils'
import { AreaChart, Area, BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, ReferenceLine } from 'recharts'
import { BarChart2, Download, TrendingUp, TrendingDown } from 'lucide-react'

export default function ReportsPage() {
  const { data: perf, isLoading, isError, error, refetch } = usePerformanceReport()
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
      <div className="flex items-center justify-between gap-4 flex-wrap">
        <div>
          <h2 className="text-xl font-semibold tracking-tight">Reports</h2>
          <p className="text-[13px] text-muted-foreground mt-1">Performance analytics and trade history</p>
        </div>
        <Button variant="outline" size="sm" onClick={exportCSV} disabled={!trades.length}>
          <Download className="w-3.5 h-3.5" />
          Export CSV
        </Button>
      </div>

      {isLoading ? (
        <div className="flex items-center gap-2 text-muted-foreground text-sm"><Spinner className="w-4 h-4" />Loading...</div>
      ) : isError ? (
        <QueryError error={error} onRetry={refetch} label="performance report" />
      ) : perf ? (
        <>
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
            <StatCard label="Total Trades" value={perf.total_trades.toString()} />
            <StatCard label="Win Rate"
              value={<span className={perf.win_rate >= 0.5 ? 'text-emerald-400' : 'text-red-400'}>{(perf.win_rate * 100).toFixed(1)}%</span>}
              sub={`${perf.winning_trades}W · ${perf.losing_trades}L`} />
            <StatCard label="Total P&L"
              value={<span className={pnlClass(perf.total_pnl)}>{perf.total_pnl >= 0 ? '+' : ''}{formatCurrency(perf.total_pnl)}</span>}
              trend={perf.total_pnl > 0 ? 'up' : perf.total_pnl < 0 ? 'down' : 'neutral'} />
            <StatCard label="Profit Factor"
              value={<span className={perf.profit_factor >= 1 ? 'text-emerald-400' : 'text-red-400'}>{perf.profit_factor.toFixed(2)}</span>}
              sub={perf.profit_factor >= 1 ? 'Profitable' : 'Unprofitable'} />
          </div>

          <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
            <StatCard label="Avg Win" value={<span className="text-emerald-400">{formatCurrency(perf.avg_win)}</span>} />
            <StatCard label="Avg Loss" value={<span className="text-red-400">{formatCurrency(perf.avg_loss)}</span>} />
            <StatCard label="Max Drawdown" value={<span className="text-red-400">{formatCurrency(perf.max_drawdown)}</span>} />
            <StatCard label="Sharpe Ratio"
              value={perf.sharpe_ratio !== null
                ? <span className={perf.sharpe_ratio >= 1 ? 'text-emerald-400' : 'text-muted-foreground'}>{perf.sharpe_ratio.toFixed(2)}</span>
                : <span className="text-muted-foreground">—</span>}
              sub={perf.sharpe_ratio !== null ? 'Annualised' : 'Need more data'} />
          </div>

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
