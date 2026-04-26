'use client'
import { useEffect, useState } from 'react'
import { AlertOctagon, TrendingUp, Wifi, WifiOff, Activity, Clock } from 'lucide-react'
import { useSettings, useAccount, useDepsHealth } from '@/hooks/use-api'
import { formatCurrency, cn } from '@/lib/utils'
import { useUIStore } from '@/stores/ui-store'

export function StatusBar() {
  const { data: settings } = useSettings()
  const { data: account } = useAccount()
  const { data: deps } = useDepsHealth()
  const sidebarCollapsed = useUIStore((s) => s.sidebarCollapsed)

  const isHealthy = deps?.database === 'ok' && deps?.redis === 'ok'
  const mode = process.env.NEXT_PUBLIC_APP_MODE ?? 'mock'

  return (
    <div
      className={cn(
        'fixed bottom-0 right-0 z-20 hidden h-7 items-center gap-5 px-6 md:flex',
        'text-[11px] font-medium border-t backdrop-blur-xl transition-colors tabular-nums',
        sidebarCollapsed ? 'left-16' : 'left-56',
        settings?.kill_switch_active
          ? 'bg-red-950/85 border-red-900/60 text-red-200'
          : 'bg-card/80 border-border text-muted-foreground'
      )}
    >
      {/* Kill switch warning */}
      {settings?.kill_switch_active && (
        <span className="flex items-center gap-1.5 text-red-300 font-semibold">
          <AlertOctagon className="w-3 h-3 animate-pulse-slow" />
          KILL SWITCH ACTIVE
        </span>
      )}

      {/* Auto-trading status */}
      <span
        className={cn(
          'flex items-center gap-1.5',
          settings?.auto_trading_enabled ? 'text-emerald-400' : 'text-muted-foreground/80'
        )}
      >
        <span
          className={cn(
            'w-1.5 h-1.5 rounded-full',
            settings?.auto_trading_enabled ? 'bg-emerald-400 animate-pulse-slow' : 'bg-muted-foreground/40'
          )}
        />
        Auto {settings?.auto_trading_enabled ? 'ON' : 'OFF'}
      </span>

      {/* Mode badge */}
      <span
        className={cn(
          'flex items-center gap-1.5 uppercase tracking-[0.08em]',
          mode === 'live' ? 'text-red-400' : mode === 'demo' ? 'text-blue-400' : 'text-purple-400'
        )}
      >
        <span
          className={cn(
            'w-1.5 h-1.5 rounded-full',
            mode === 'live' ? 'bg-red-400' : mode === 'demo' ? 'bg-blue-400' : 'bg-purple-400'
          )}
        />
        {mode}
      </span>

      {/* Cash available */}
      {account && (
        <span className="flex items-center gap-1.5 text-foreground/90">
          <TrendingUp className="w-3 h-3 text-emerald-400" />
          {formatCurrency(account.free_funds, account.currency)}
          <span className="text-muted-foreground/70">available</span>
        </span>
      )}

      {/* Spacer */}
      <span className="flex-1" />

      {/* Market data provider */}
      <span className="text-muted-foreground/80">
        {deps?.market_data === 'polygon' ? 'Polygon.io' : 'Mock data'}
      </span>

      {/* Connectivity */}
      <span
        className={cn(
          'flex items-center gap-1.5',
          isHealthy ? 'text-emerald-400' : 'text-red-400'
        )}
      >
        {isHealthy ? <Wifi className="w-3 h-3" /> : <WifiOff className="w-3 h-3" />}
        {isHealthy ? 'Connected' : 'Degraded'}
      </span>

      {/* Time */}
      <span className="flex items-center gap-1.5 text-muted-foreground/80">
        <Clock className="w-3 h-3" />
        <ClientTime />
      </span>
    </div>
  )
}

function ClientTime() {
  const [time, setTime] = useState('')
  useEffect(() => {
    const update = () =>
      setTime(
        new Date().toLocaleTimeString('en-US', {
          hour12: false,
          timeZone: 'America/New_York',
        }) + ' ET'
      )
    update()
    const id = setInterval(update, 1000)
    return () => clearInterval(id)
  }, [])
  return <span suppressHydrationWarning>{time}</span>
}
