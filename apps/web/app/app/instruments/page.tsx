'use client'
import { useState } from 'react'
import { Search, RefreshCw, CheckCircle2, XCircle, Database } from 'lucide-react'
import { useInstruments, useSyncInstruments } from '@/hooks/use-api'
import { Button, Card, CardContent, Input, Badge, Spinner, EmptyState, PageHeader } from '@/components/ui'
import { QueryError } from '@/components/shared/query-error'
import { formatDate, cn } from '@/lib/utils'

export default function InstrumentsPage() {
  const [search, setSearch] = useState('')
  const [page, setPage] = useState(1)
  const PAGE_SIZE = 50

  const { data, isLoading, isError, error, refetch } = useInstruments({ search: search || undefined, page, page_size: PAGE_SIZE })
  const syncMutation = useSyncInstruments()

  const instruments = data?.items ?? []
  const total = data?.total ?? 0
  const enabledCount = instruments.filter((i) => i.trading_enabled).length

  return (
    <div className="space-y-5">
      <PageHeader
        icon={<Database className="h-5 w-5" />}
        label="Instruments"
        sub={total > 0
          ? <><span className="tnum">{total.toLocaleString()}</span> instruments · <span className="tnum">{enabledCount}</span> trading enabled</>
          : 'Sync from your broker to populate the instrument list'}
        actions={
          <Button variant="outline" size="sm" onClick={() => syncMutation.mutate()} loading={syncMutation.isPending}>
            <RefreshCw className="h-3.5 w-3.5" />
            Sync from Broker
          </Button>
        }
      />

      {/* Search */}
      <div className="relative max-w-md">
        <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-muted-foreground" />
        <Input
          placeholder="Search ticker or name..."
          className="pl-9"
          value={search}
          onChange={(e) => { setSearch(e.target.value); setPage(1) }}
        />
      </div>

      {/* Content */}
      {isLoading ? (
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Spinner className="h-4 w-4" /> Loading instruments…
        </div>
      ) : isError ? (
        <QueryError error={error} onRetry={refetch} label="instruments" />
      ) : instruments.length === 0 ? (
        <Card>
          <CardContent>
            <EmptyState
              title="No instruments"
              description="Sync instruments from the broker to populate this list."
              action={
                <Button size="sm" onClick={() => syncMutation.mutate()} loading={syncMutation.isPending}>
                  <RefreshCw className="h-3.5 w-3.5" /> Sync Now
                </Button>
              }
            />
          </CardContent>
        </Card>
      ) : (
        <Card>
          <CardContent className="p-0">
            <div className="overflow-x-auto scrollbar-none">
              <table className="w-full data-table tnum">
                <thead>
                  <tr>
                    <th>Ticker</th>
                    <th>Name</th>
                    <th>Type</th>
                    <th>Currency</th>
                    <th className="text-center">Ext Hours</th>
                    <th>Trading</th>
                    <th>Synced</th>
                  </tr>
                </thead>
                <tbody>
                  {instruments.map((inst) => (
                    <tr key={inst.id}>
                      <td>
                        <span className="font-mono font-semibold text-foreground">{inst.ticker}</span>
                      </td>
                      <td className="max-w-[220px] truncate text-muted-foreground">{inst.name}</td>
                      <td>
                        <Badge variant="outline" className="text-[10px] uppercase tracking-wide">
                          {inst.type}
                        </Badge>
                      </td>
                      <td>
                        <span className="text-xs text-muted-foreground">{inst.currency_code}</span>
                      </td>
                      <td className="text-center">
                        {inst.extended_hours
                          ? <CheckCircle2 className="mx-auto h-3.5 w-3.5 text-emerald-400" />
                          : <XCircle className="mx-auto h-3.5 w-3.5 text-muted-foreground/25" />}
                      </td>
                      <td>
                        <Badge variant={inst.trading_enabled ? 'success' : 'outline'} className="text-[10px]">
                          {inst.trading_enabled ? 'Enabled' : 'Disabled'}
                        </Badge>
                      </td>
                      <td className="text-xs text-muted-foreground">
                        {inst.synced_at ? formatDate(inst.synced_at) : '—'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Pagination */}
      {total > PAGE_SIZE && (
        <div className="flex items-center justify-between text-sm">
          <p className="text-muted-foreground">
            Page <span className="tnum font-medium text-foreground">{page}</span> ·{' '}
            showing <span className="tnum">{instruments.length}</span> of{' '}
            <span className="tnum">{total.toLocaleString()}</span>
          </p>
          <div className="flex gap-2">
            <Button variant="outline" size="sm" disabled={page === 1} onClick={() => setPage((p) => p - 1)}>
              Previous
            </Button>
            <Button variant="outline" size="sm" disabled={instruments.length < PAGE_SIZE} onClick={() => setPage((p) => p + 1)}>
              Next
            </Button>
          </div>
        </div>
      )}
    </div>
  )
}
