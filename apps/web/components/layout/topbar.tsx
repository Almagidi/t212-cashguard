'use client'
import { AlertOctagon, Wifi, WifiOff, Sun, Moon } from 'lucide-react'
import { usePathname } from 'next/navigation'
import { useTheme } from 'next-themes'
import { cn } from '@/lib/utils'
import { useSettings, useHealth } from '@/hooks/use-api'
import { useUIStore } from '@/stores/ui-store'
import { MobileMenuButton, MobileDrawer } from './sidebar'
import { useState, useEffect } from 'react'

const PAGE_META: Record<string, { title: string; subtitle?: string; live?: boolean }> = {
  '/app/dashboard':   { title: 'Dashboard',          subtitle: 'Realtime portfolio overview',         live: true },
  '/app/broker':      { title: 'Broker Account',     subtitle: 'Trading 212 connection and account' },
  '/app/instruments': { title: 'Instruments',        subtitle: 'Tradable symbols and market data' },
  '/app/strategies':  { title: 'Strategies',         subtitle: 'Automation rules and signal pipelines' },
  '/app/orders':      { title: 'Orders',             subtitle: 'Open, filled, and cancelled orders',  live: true },
  '/app/positions':   { title: 'Positions',          subtitle: 'Current holdings and unrealised P&L', live: true },
  '/app/risk':        { title: 'Risk Controls',      subtitle: 'Limits, guards, and circuit breakers' },
  '/app/alerts':      { title: 'Alerts',             subtitle: 'Realtime notifications and routing',  live: true },
  '/app/reports':     { title: 'Reports',            subtitle: 'Performance, exposure, and P&L' },
  '/app/journal':     { title: 'Trade Journal',      subtitle: 'Trade log and post-trade review' },
  '/app/settings':    { title: 'Settings',           subtitle: 'Application preferences' },
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

function UtcClock() {
  const [time, setTime] = useState('')

  useEffect(() => {
    const fmt = () => {
      const now = new Date()
      const hh = String(now.getUTCHours()).padStart(2, '0')
      const mm = String(now.getUTCMinutes()).padStart(2, '0')
      const ss = String(now.getUTCSeconds()).padStart(2, '0')
      setTime(`${hh}:${mm}:${ss}`)
    }
    fmt()
    const id = setInterval(fmt, 1000)
    return () => clearInterval(id)
  }, [])

  if (!time) return null

  return (
    <span className="hidden sm:block font-mono text-[12px] text-primary/70 tabular-nums tracking-tight select-none">
      {time} UTC
    </span>
  )
}

export function TopBar() {
  const pathname = usePathname()
  const { data: settings } = useSettings()
  const { data: health }   = useHealth()
  const { theme, setTheme } = useTheme()
  const sidebarCollapsed   = useUIStore((s) => s.sidebarCollapsed)
  const [drawerOpen, setDrawerOpen] = useState(false)

  const mode = (process.env.NEXT_PUBLIC_APP_MODE || health?.mode || 'mock') as string
  const meta = Object.entries(PAGE_META).find(([p]) => pathname.startsWith(p))?.[1] ?? { title: 'CashGuard' }

  return (
    <>
      <header
        className={cn(
          'fixed top-0 right-0 h-14 bg-background/80 backdrop-blur-xl border-b border-border',
          'flex items-center justify-between px-4 md:px-6 z-20 transition-[left] duration-200',
          sidebarCollapsed ? 'left-0 md:left-16' : 'left-0 md:left-56',
        )}
      >
        {/* Left: hamburger + UTC clock + page title */}
        <div className="flex items-center gap-3 min-w-0">
          <MobileMenuButton open={drawerOpen} onToggle={() => setDrawerOpen(v => !v)} />
          <UtcClock />
          <div
            className={cn(
              'min-w-0',
              meta.live && 'border-l-2 pl-3',
            )}
            style={meta.live ? { borderColor: 'hsl(var(--primary) / 0.6)' } : undefined}
          >
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

        {/* Right: kill switch + mode badge + theme toggle + connection */}
        <div className="flex items-center gap-2">
          {settings?.kill_switch_active && (
            <span className="hidden sm:inline-flex items-center gap-1.5 text-[10px] font-semibold px-2 py-1 rounded-full bg-red-500/10 border border-red-500/30 text-red-400 uppercase tracking-[0.08em] shadow-sm shadow-red-500/10">
              <AlertOctagon className="w-3 h-3 animate-pulse-slow" />
              Kill Switch
            </span>
          )}

          <span className={cn(modeBadgeClass(mode), 'hidden sm:inline-flex')}>
            {mode}
          </span>

          {/* Theme toggle */}
          <button
            onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}
            className="flex items-center justify-center w-8 h-8 rounded-lg hover:bg-accent transition-colors"
            aria-label="Toggle theme"
          >
            {theme === 'dark'
              ? <Sun  className="w-4 h-4 text-muted-foreground hover:text-foreground transition-colors" />
              : <Moon className="w-4 h-4 text-muted-foreground hover:text-foreground transition-colors" />
            }
          </button>

          {/* Connection indicator */}
          <div
            className={cn(
              'flex items-center gap-1.5 text-[11px] font-medium px-2 py-1 rounded-md border',
              health?.status === 'ok'
                ? 'text-cyan-400 border-cyan-500/20 bg-cyan-500/5'
                : 'text-red-400 border-red-500/20 bg-red-500/5',
            )}
          >
            {health?.status === 'ok'
              ? <Wifi    className="w-3 h-3" />
              : <WifiOff className="w-3 h-3" />
            }
            <span className="hidden sm:inline">
              {health?.status === 'ok' ? 'Connected' : 'Offline'}
            </span>
          </div>
        </div>
      </header>

      <MobileDrawer open={drawerOpen} onClose={() => setDrawerOpen(false)} />
    </>
  )
}
