'use client'
import { RefreshCw, TrendingUp, TrendingDown } from 'lucide-react'
import { usePositions } from '@/hooks/use-api'
import { Button, Card, CardContent, Spinner, EmptyState, TerminalCard, PageHeader } from '@/components/ui'
import { QueryError } from '@/components/shared/query-error'
import { formatCurrency, formatPnL, pnlClass, cn } from '@/lib/utils'
import { useQueryClient } from '@tanstack/react-query'
import api from '@/services/api'

export default function PositionsPage() {
  const qc = useQueryClient()
  const { data: positions = [], isLoading, isError, error, refetch } = usePositions()

  const totalValue = positions.reduce((s, p) => s + (p.value ?? 0), 0)
  const totalPnl = positions.reduce((s, p) => s + (p.unrealized_pnl ?? 0), 0)

  const refresh = async () => {
    await api.refreshPositions()
    qc.invalidateQueries({ queryKey: ['positions'] })
  }

  return (
    <div className="space-y-6">
      <PageHeader
        icon={<TrendingUp className="h-5 w-5" />}
        label="Positions"
        sub={<>
          {positions.length} open position{positions.length !== 1 ? 's' : ''}
          {positions.length > 0 && <> · {formatCurrency(totalValue)} invested</>}
        </>}
        actions={
          <Button variant="outline" size="sm" onClick={refresh}>
            <RefreshCw className="w-3.5 h-3.5" />
            Refresh
          </Button>
        }
      />

      {positions.length > 0 && (
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
          <TerminalCard
            label="Position Value"
            value={formatCurrency(totalValue)}
            sub="Current market value"
            variant="cyan"
          />
          <TerminalCard
            label="Unrealized P&L"
            value={formatPnL(totalPnl)}
            sub={totalPnl > 0 ? 'Profitable' : totalPnl < 0 ? 'In drawdown' : 'Flat'}
            variant={totalPnl > 0 ? 'teal' : totalPnl < 0 ? 'red' : 'cyan'}
          />
          <TerminalCard
            label="Open Positions"
            value={positions.length.toString()}
            sub="Across all symbols"
            variant="cyan"
          />
        </div>
      )}

      {isLoading ? (
        <Card>
          <CardContent className="flex items-center gap-2 text-muted-foreground text-sm py-12 justify-center">
            <Spinner className="w-4 h-4" /> Loading positions…
          </CardContent>
        </Card>
      ) : isError ? (
        <Card>
          <CardContent className="p-0">
            <QueryError error={error} onRetry={refetch} label="positions" />
          </CardContent>
        </Card>
      ) : positions.length === 0 ? (
        <Card>
          <CardContent className="p-0">
            <EmptyState
              icon={<TrendingUp className="w-5 h-5" />}
              title="No open positions"
              description="Positions will appear here when trades are executed."
            />
          </CardContent>
        </Card>
      ) : (
        <Card>
          <CardContent className="p-0">
            <div className="overflow-x-auto scrollbar-none -mx-0">
              <table className="w-full data-table min-w-[680px]">
                <thead>
                  <tr>
                    <th className="sticky left-0 bg-card z-10">Symbol</th>
                    <th className="text-right">Qty</th>
                    <th className="text-right">Avg Price</th>
                    <th className="text-right">Mark</th>
                    <th className="text-right">Value</th>
                    <th className="text-right">Unrealised P&L</th>
                    <th className="text-right hidden sm:table-cell">Available</th>
                    <th className="text-right">Return</th>
                  </tr>
                </thead>
                <tbody>
                  {positions.map(pos => {
                    const returnPct = pos.avg_price > 0 && pos.current_price
                      ? ((pos.current_price - pos.avg_price) / pos.avg_price) * 100
                      : null

                    return (
                      <tr key={pos.ticker}>
                        <td className="sticky left-0 bg-card">
                          <div className="flex items-center gap-2.5">
                            <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-primary/20 to-primary/5 border border-primary/20 flex items-center justify-center text-[10px] font-bold tracking-wider text-primary">
                              {pos.ticker.slice(0, 2)}
                            </div>
                            <div>
                              <p className="font-semibold text-foreground leading-none">{pos.ticker}</p>
                              <p className="text-[10px] text-muted-foreground/70 mt-1 uppercase tracking-wider">Long</p>
                            </div>
                          </div>
                        </td>
                        <td className="tnum text-right">{pos.quantity.toFixed(4)}</td>
                        <td className="tnum text-right text-muted-foreground">{formatCurrency(pos.avg_price)}</td>
                        <td className="tnum text-right">{pos.current_price ? formatCurrency(pos.current_price) : '—'}</td>
                        <td className="tnum text-right font-medium">{pos.value ? formatCurrency(pos.value) : '—'}</td>
                        <td className={cn('tnum text-right font-semibold', pnlClass(pos.unrealized_pnl))}>
                          {formatPnL(pos.unrealized_pnl)}
                        </td>
                        <td className="tnum text-right text-muted-foreground/80 hidden sm:table-cell">{pos.quantity_available?.toFixed(4) ?? '—'}</td>
                        <td className={cn('tnum text-right', returnPct !== null ? pnlClass(returnPct) : 'text-muted-foreground')}>
                          {returnPct !== null ? (
                            <span className="inline-flex items-center gap-1 font-medium">
                              {returnPct >= 0 ? <TrendingUp className="w-3 h-3" /> : <TrendingDown className="w-3 h-3" />}
                              {returnPct >= 0 ? '+' : ''}{returnPct.toFixed(2)}%
                            </span>
                          ) : '—'}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  )
}
