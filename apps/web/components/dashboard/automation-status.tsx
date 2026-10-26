'use client'

import { useEffect, useState } from 'react'
import type { WsSnapshot, WsStatus } from '@/hooks/use-websocket'

type ControlSnapshot = Pick<WsSnapshot, 'ts' | 'system'>

// Match the feed's existing ten-second liveness window. Pings alone do not
// make an old control snapshot current.
const MAX_SNAPSHOT_AGE_MS = 10_000

export function AutomationStatus({ snapshot, status }: {
  snapshot: ControlSnapshot | null
  status: WsStatus
}) {
  const [now, setNow] = useState(() => Date.now())
  const [connection, setConnection] = useState({
    status,
    disconnectedSnapshot: status === 'connected' ? null : snapshot,
  })

  // A reconnect must wait for a new frame, even when the previous one is young.
  if (connection.status !== status) {
    setConnection({
      status,
      disconnectedSnapshot: status === 'connected' ? connection.disconnectedSnapshot : snapshot,
    })
  }

  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1_000)
    return () => clearInterval(timer)
  }, [])

  const age = now - Date.parse(snapshot?.ts ?? '')
  const current = status === 'connected'
    && snapshot !== null
    && snapshot !== connection.disconnectedSnapshot
    && Number.isFinite(age)
    && age >= 0
    && age <= MAX_SNAPSHOT_AGE_MS
  const enabled = snapshot?.system.auto_trading_enabled
  const label = current ? (enabled ? 'Enabled' : 'Disabled') : 'Unknown'
  const color = current
    ? (enabled ? 'text-emerald-400' : 'text-muted-foreground')
    : 'text-amber-400'

  return (
    <div className="flex items-center justify-between text-xs">
      <span className="text-muted-foreground">Auto Trading</span>
      <span className={`font-medium ${color}`}>{label}</span>
    </div>
  )
}
