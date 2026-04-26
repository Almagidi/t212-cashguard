'use client'
import { useState } from 'react'
import { Search, ChevronDown, ChevronRight, ScrollText } from 'lucide-react'
import { useAuditLogs } from '@/hooks/use-api'
import { Button, Card, CardContent, Input, Spinner, EmptyState, PageHeader } from '@/components/ui'
import { QueryError } from '@/components/shared/query-error'
import { formatDate, cn } from '@/lib/utils'
import type { AuditLog } from '@/types'

const ACTION_COLORS: Record<string, string> = {
  login_success: 'text-emerald-400', login_failed: 'text-red-400',
  logout: 'text-muted-foreground', broker_connected: 'text-blue-400',
  broker_disconnected: 'text-amber-400', strategy_enabled: 'text-emerald-400',
  strategy_disabled: 'text-amber-400', order_placed: 'text-blue-400',
  order_cancelled: 'text-amber-400', kill_switch_enabled: 'text-red-400',
  kill_switch_disabled: 'text-emerald-400', emergency_kill_switch: 'text-red-400',
  emergency_flatten_all: 'text-red-400', emergency_cancel_all: 'text-amber-400',
  auto_trading_enabled: 'text-emerald-400', auto_trading_disabled: 'text-amber-400',
  risk_profile_updated: 'text-blue-400', settings_updated: 'text-muted-foreground',
}

function AuditRow({ log }: { log: AuditLog }) {
  const [expanded, setExpanded] = useState(false)
  const hasPayload = log.payload && Object.keys(log.payload).length > 0

  return (
    <>
      <tr
        className={cn(hasPayload && 'cursor-pointer')}
        onClick={() => hasPayload && setExpanded(!expanded)}
      >
        <td className="sticky left-0 bg-card">
          <div className="flex items-center gap-1.5">
            {hasPayload && (
              expanded ? <ChevronDown className="w-3 h-3 text-muted-foreground" /> : <ChevronRight className="w-3 h-3 text-muted-foreground" />
            )}
            <span className={cn('text-xs font-medium font-mono', ACTION_COLORS[log.action] ?? 'text-foreground')}>
              {log.action}
            </span>
          </div>
        </td>
        <td className="text-muted-foreground hidden sm:table-cell">{log.actor}</td>
        <td className="text-muted-foreground">{log.entity_type ?? '—'}</td>
        <td className="font-mono text-muted-foreground text-xs hidden md:table-cell">{log.entity_id ? log.entity_id.slice(0, 12) + '…' : '—'}</td>
        <td className="text-muted-foreground hidden lg:table-cell">{log.ip_address ?? '—'}</td>
        <td className="text-muted-foreground">{formatDate(log.occurred_at)}</td>
      </tr>
      {expanded && hasPayload && (
        <tr>
          <td colSpan={6} className="bg-muted/30 px-3 py-3">
            <pre className="text-xs font-mono text-muted-foreground overflow-x-auto whitespace-pre-wrap">
              {JSON.stringify(log.payload, null, 2)}
            </pre>
          </td>
        </tr>
      )}
    </>
  )
}

export default function AuditPage() {
  const [search, setSearch] = useState('')
  const [page, setPage] = useState(1)
  const PAGE_SIZE = 50

  const { data, isLoading, isError, error, refetch } = useAuditLogs({
    action: search || undefined,
    page,
    page_size: PAGE_SIZE,
  })

  const logs = data?.items ?? []
  const total = data?.total ?? 0
  const totalPages = Math.ceil(total / PAGE_SIZE)

  return (
    <div className="space-y-5">
      <PageHeader
        icon={<ScrollText className="h-5 w-5" />}
        label="Audit Log"
        sub={<span className="tabular-nums">{total.toLocaleString()} events recorded</span>}
      />

      {/* Search */}
      <div className="relative max-w-sm">
        <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-muted-foreground/70 pointer-events-none" />
        <Input
          placeholder="Filter by action…"
          className="pl-9"
          value={search}
          onChange={e => { setSearch(e.target.value); setPage(1) }}
        />
      </div>

      {isLoading ? (
        <div className="flex items-center gap-2 text-muted-foreground text-sm"><Spinner className="w-4 h-4" />Loading...</div>
      ) : isError ? (
        <QueryError error={error} onRetry={refetch} label="audit logs" />
      ) : logs.length === 0 ? (
        <Card><CardContent><EmptyState title="No audit logs" description="Events will be recorded here as you use the application." /></CardContent></Card>
      ) : (
        <Card>
          <CardContent className="p-0">
            <div className="overflow-x-auto scrollbar-none">
              <table className="w-full data-table min-w-[480px]">
                <thead>
                  <tr>
                    <th className="sticky left-0 bg-card z-10">Action</th>
                    <th className="hidden sm:table-cell">Actor</th>
                    <th>Entity</th>
                    <th className="hidden md:table-cell">Entity ID</th>
                    <th className="hidden lg:table-cell">IP</th>
                    <th>Time</th>
                  </tr>
                </thead>
                <tbody>
                  {logs.map(log => <AuditRow key={log.id} log={log} />)}
                </tbody>
              </table>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Pagination */}
      {totalPages > 1 && (
        <div className="flex items-center justify-between text-sm">
          <p className="text-muted-foreground">Page {page} of {totalPages} · {total} total events</p>
          <div className="flex gap-2">
            <Button variant="outline" size="sm" disabled={page === 1} onClick={() => setPage(p => p - 1)}>Previous</Button>
            <Button variant="outline" size="sm" disabled={page === totalPages} onClick={() => setPage(p => p + 1)}>Next</Button>
          </div>
        </div>
      )}
    </div>
  )
}
