'use client'
import { AlertOctagon, Wifi, WifiOff } from 'lucide-react'
import { usePathname } from 'next/navigation'
import { cn } from '@/lib/utils'
import { useSettings, useHealth } from '@/hooks/use-api'
import { MobileMenuButton, MobileDrawer } from './sidebar'
import { useState } from 'react'

const PAGE_META: Record<string, { title: string; subtitle?: string }> = {
  '/app/dashboard':   { title: 'Dashboard',          subtitle: 'Realtime portfolio and strategy overview' },
  '/app/broker':      { title: 'Broker Account',     subtitle: 'Trading 212 connection and account details' },
  '/app/instruments': { title: 'Instruments',        subtitle: 'Tradable symbols and market data' },
  '/app/strategies':  { title: 'Strategies',         subtitle: 'Automation rules and signal pipelines' },
  '/app/orders':      { title: 'Orders',             subtitle: 'Open, filled, and cancelled orders' },
  '/app/positions':   { title: 'Positions',          subtitle: 'Current holdings and unrealised P&L' },
  '/app/risk':        { title: 'Risk Controls',      subtitle: 'Limits, guards, and circuit breakers' },
  '/app/alerts':      { title: 'Alerts',             subtitle: 'Realtime notifications and routing' },
  '/app/reports':     { title: 'Reports',            subtitle: 'Performance, exposure, and P&L reports' },
  '/app/journal':     { title: 'Trade Journal',      subtitle: 'Trade log and post-trade review' },
  '/app/settings':    { title: 'Settings',           subtitle: 'Application preferences and integrations' },
  '/app/emergency':   { title: 'Emergency Controls', subtitle: 'Kill switch and rapid unwind' },
  '/app/audit':       { title: 'Audit Log',          subtitle: 'Immutable record of sensitive actions' },
  '/app/backtest':    { title: 'Backtest',           subtitle: 'Historical strategy simulation' },
}

function modeBadgeClass(mode: string) {
  switch (mode) {
    case 'live': return 'badge-live'
    case 'demo': return 'badge-demo'
    default:     return 'badge-mock'
  }
}

export function TopBar() {
  const pathname = usePathname()
  const { data: settings } = useSettings()
  const { data: health } = useHealth()
  const [drawerOpen, setDrawerOpen] = useState(false)

  const mode = (process.env.NEXT_PUBLIC_APP_MODE || health?.mode || 'mock') as string
  const meta = Object.entries(PAGE_META).find(([p]) => pathname.startsWith(p))?.[1] ?? {
    title: 'CashGuard',
  }

  return (
    <>
      <header className="fixed top-0 left-0 md:left-56 right-0 h-14 bg-background/75 backdrop-blur-xl border-b border-border flex items-center justify-between px-4 md:px-6 z-20">
        <div className="flex items-center gap-3 min-w-0">
          <MobileMenuButton open={drawerOpen} onToggle={() => setDrawerOpen(v => !v)} />
          <div className="min-w-0">
            <h1 className="text-[15px] font-semibold text-foreground tracking-tight leading-tight truncate">
              {meta.title}
            </h1>
            {meta.subtitle && (
              <p className="text-[11px] text-muted-foreground/80 leading-tight mt-0.5 hidden sm:block truncate">
                {meta.subtitle}
              </p>
            )}
          </div>
        </div>

        <div className="flex items-center gap-2 md:gap-3">
          {/* Kill switch warning */}
          {settings?.kill_switch_active && (
            <span className="hidden sm:inline-flex items-center gap-1.5 text-[10px] font-semibold px-2 py-1 rounded-full bg-red-500/10 border border-red-500/30 text-red-400 uppercase tracking-[0.08em]">
              <AlertOctagon className="w-3 h-3 animate-pulse-slow" />
              Kill Switch
            </span>
          )}

          {/* Mode badge */}
          <span className={cn(modeBadgeClass(mode), 'hidden sm:inline-flex')}>
            {mode}
          </span>

          {/* Connection indicator */}
          <div
            className={cn(
              'flex items-center gap-1.5 text-[11px] font-medium px-2 py-1 rounded-md border',
              health?.status === 'ok'
                ? 'text-emerald-400 border-emerald-500/20 bg-emerald-500/5'
                : 'text-red-400 border-red-500/20 bg-red-500/5'
            )}
          >
            {health?.status === 'ok' ? (
              <Wifi className="w-3 h-3" />
            ) : (
              <WifiOff className="w-3 h-3" />
            )}
            <span className="hidden sm:inline">
              {health?.status === 'ok' ? 'Connected' : 'Offline'}
            </span>
          </div>
        </div>
      </header>

      {/* Mobile drawer */}
      <MobileDrawer open={drawerOpen} onClose={() => setDrawerOpen(false)} />
    </>
  )
}
