'use client'

import { useEffect, useState } from 'react'
import type { WsSnapshot, WsStatus } from '@/hooks/use-websocket'

type ControlSnapshot = Pick<WsSnapshot, 'ts' | 'system'>

// Match the feed's existing ten-second liveness window. Pings alone do not
// make an old control snapshot current.
const MAX_SNAPSHOT_AGE_MS = 10_000

type StatusProps = {
  snapshot: ControlSnapshot | null
  status: WsStatus
  lastFrameAt: Date | null
}

function ControlStatus({ snapshot, status, lastFrameAt, field }: StatusProps & { field: 'auto_trading_enabled' | 'kill_switch_active' }) {
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

  // A newly received frame can be newer than the last expiration timer tick.
  const observedNow = Math.max(now, lastFrameAt?.getTime() ?? now)
  const age = observedNow - Date.parse(snapshot?.ts ?? '')
  const current = status === 'connected'
    && snapshot !== null
    && snapshot !== connection.disconnectedSnapshot
    && Number.isFinite(age)
    && age >= 0
    && age <= MAX_SNAPSHOT_AGE_MS
  const enabled = snapshot?.system[field]
  const known = current && typeof enabled === 'boolean'
  const automation = field === 'auto_trading_enabled'
  const label = known ? (automation ? (enabled ? 'Enabled' : 'Disabled') : (enabled ? 'ACTIVE' : 'Inactive')) : 'Unknown'
  const color = known
    ? (enabled ? (automation ? 'text-emerald-700 dark:text-emerald-400' : 'text-red-700 dark:text-red-400') : 'text-muted-foreground')
    : 'text-amber-800 dark:text-amber-400'

  return (
    <div className="flex items-center justify-between text-xs">
      <span className="text-muted-foreground">{automation ? 'Auto Trading' : 'Kill Switch'}</span>
      <span className={`font-medium ${color}`}>{label}</span>
    </div>
  )
}

export function AutomationStatus(props: StatusProps) {
  return <ControlStatus {...props} field="auto_trading_enabled" />
}

export function KillSwitchStatus(props: StatusProps) {
  return <ControlStatus {...props} field="kill_switch_active" />
}
