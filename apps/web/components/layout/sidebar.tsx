'use client'
import Link from 'next/link'
import { usePathname, useRouter } from 'next/navigation'
import {
  LayoutDashboard, Building2, LineChart, ListOrdered, Briefcase,
  ShieldAlert, Bell, FileBarChart, Settings, AlertOctagon,
  ScrollText, Activity, LogOut, FlaskConical, BookOpen,
  Menu, X, ChevronLeft, ChevronRight,
} from 'lucide-react'
import { cn } from '@/lib/utils'
import { useAuthStore } from '@/stores/auth'
import { useSettings, useOrders, usePositions, useAlerts } from '@/hooks/use-api'
import { useUIStore } from '@/stores/ui-store'
import { ShieldLogo } from './shield-logo'
import api from '@/services/api'
import { useEffect } from 'react'

// ── Nav groups ────────────────────────────────────────────────────────────────

const NAV_GROUPS = [
  {
    label: 'Trading',
    items: [
      { href: '/app/dashboard',   icon: LayoutDashboard, label: 'Dashboard' },
      { href: '/app/broker',      icon: Building2,       label: 'Broker' },
      { href: '/app/instruments', icon: Activity,        label: 'Instruments' },
      { href: '/app/strategies',  icon: LineChart,       label: 'Strategies' },
    ],
  },
  {
    label: 'Operations',
    items: [
      { href: '/app/orders',    icon: ListOrdered, label: 'Orders',        badge: 'orders' as const },
      { href: '/app/positions', icon: Briefcase,   label: 'Positions',     badge: 'positions' as const },
      { href: '/app/risk',      icon: ShieldAlert, label: 'Risk Controls' },
      { href: '/app/backtest',  icon: FlaskConical, label: 'Backtest' },
    ],
  },
  {
    label: 'Monitoring',
    items: [
      { href: '/app/alerts',  icon: Bell,         label: 'Alerts',        badge: 'alerts' as const },
      { href: '/app/reports', icon: FileBarChart,  label: 'Reports' },
      { href: '/app/journal', icon: BookOpen,      label: 'Trade Journal' },
      { href: '/app/audit',   icon: ScrollText,    label: 'Audit Log' },
    ],
  },
] as const

const SYSTEM_ITEMS = [
  { href: '/app/settings',  icon: Settings,      label: 'Settings' },
  { href: '/app/emergency', icon: AlertOctagon,  label: 'Emergency', danger: true },
]

// ── Badge counts hook ─────────────────────────────────────────────────────────

function useNavBadges() {
  const { data: orders }    = useOrders()
  const { data: positions } = usePositions()
  const { data: alerts }    = useAlerts({ is_read: false, limit: 99 })
  return {
    orders:    orders?.length    ?? 0,
    positions: positions?.length ?? 0,
    alerts:    alerts?.length    ?? 0,
  }
}

// ── Nav link ──────────────────────────────────────────────────────────────────

function NavLink({
  href, icon: Icon, label, active, danger, badgeCount, collapsed, onClick,
}: {
  href: string
  icon: React.ElementType
  label: string
  active: boolean
  danger?: boolean
  badgeCount?: number
  collapsed: boolean
  onClick?: () => void
}) {
  return (
    <Link
      href={href}
      onClick={onClick}
      title={collapsed ? label : undefined}
      className={cn(
        'nav-link',
        danger && !active && 'text-red-400/80 hover:!text-red-300',
        danger && active  && 'text-red-400 !bg-red-500/10',
        !danger && active && 'nav-link-active text-primary',
        collapsed && 'justify-center px-0',
      )}
    >
      <Icon
        className={cn(
          'flex-shrink-0',
          collapsed ? 'w-5 h-5' : 'w-[15px] h-[15px]',
          active && !danger && 'text-primary',
          danger && 'text-current',
        )}
      />
      {!collapsed && <span className="flex-1 truncate">{label}</span>}
      {!collapsed && badgeCount !== undefined && badgeCount > 0 && (
        <span
          className={cn(
            'nav-badge',
            href.includes('alerts')
              ? 'bg-primary text-primary-foreground'
              : 'bg-muted text-muted-foreground',
          )}
        >
          {badgeCount > 99 ? '99+' : badgeCount}
        </span>
      )}
    </Link>
  )
}

// ── Sidebar content ───────────────────────────────────────────────────────────

function SidebarContent({ onNavigate, forceExpanded = false }: { onNavigate?: () => void; forceExpanded?: boolean }) {
  const pathname   = usePathname()
  const router     = useRouter()
  const { logout } = useAuthStore()
  const { data: settings } = useSettings()
  const { sidebarCollapsed: storeCollapsed, toggleSidebar } = useUIStore()
  const sidebarCollapsed = forceExpanded ? false : storeCollapsed
  const badges     = useNavBadges()

  const getBadge = (key?: 'orders' | 'positions' | 'alerts') =>
    key ? badges[key] : undefined

  const handleLogout = async () => {
    await api.logout()
    logout()
    router.push('/auth/login')
    onNavigate?.()
  }

  return (
    <>
      {/* ── Logo ─────────────────────────────────────────────────────────── */}
      <div
        className={cn(
          'h-14 border-b border-border flex items-center flex-shrink-0',
          sidebarCollapsed ? 'justify-center px-0' : 'px-4 gap-3',
        )}
      >
        <ShieldLogo className="w-8 h-8 flex-shrink-0" />
        {!sidebarCollapsed && (
          <div className="min-w-0">
            <p className="text-[13px] font-semibold leading-tight tracking-tight">CashGuard</p>
            <p className="text-[10px] leading-tight mt-0.5 font-medium" style={{ color: 'hsl(var(--primary) / 0.6)' }}>
              Trading 212
            </p>
          </div>
        )}
      </div>

      {/* ── Kill switch warning ───────────────────────────────────────────── */}
      {settings?.kill_switch_active && !sidebarCollapsed && (
        <div className="mx-3 mt-3 px-3 py-2 rounded-lg bg-red-500/10 border border-red-500/30 text-[11px] text-red-400 flex items-center gap-2 flex-shrink-0 font-medium">
          <AlertOctagon className="w-3.5 h-3.5 flex-shrink-0 animate-pulse-slow" />
          Kill Switch Active
        </div>
      )}

      {/* ── Nav groups ───────────────────────────────────────────────────── */}
      <nav className="flex-1 overflow-y-auto min-h-0 scrollbar-none space-y-4"
           style={{ padding: sidebarCollapsed ? '12px 8px' : '12px' }}>
        {NAV_GROUPS.map((group) => (
          <div key={group.label}>
            {!sidebarCollapsed && (
              <p className="nav-section-header px-3 mb-1">{group.label}</p>
            )}
            <div className="space-y-0.5">
              {group.items.map((item) => (
                <NavLink
                  key={item.href}
                  href={item.href}
                  icon={item.icon}
                  label={item.label}
                  active={pathname.startsWith(item.href)}
                  badgeCount={getBadge((item as { badge?: 'orders' | 'positions' | 'alerts' }).badge)}
                  collapsed={sidebarCollapsed}
                  onClick={onNavigate}
                />
              ))}
            </div>
          </div>
        ))}

        {/* ── System group ───────────────────────────────────────────────── */}
        <div className="pt-2 border-t border-border/50">
          {!sidebarCollapsed && (
            <p className="nav-section-header px-3 mb-1">System</p>
          )}
          <div className="space-y-0.5">
            {SYSTEM_ITEMS.map((item) => (
              <NavLink
                key={item.href}
                href={item.href}
                icon={item.icon}
                label={item.label}
                active={pathname.startsWith(item.href)}
                danger={item.danger}
                collapsed={sidebarCollapsed}
                onClick={onNavigate}
              />
            ))}
          </div>
        </div>
      </nav>

      {/* ── Footer: collapse toggle + logout ─────────────────────────────── */}
      <div className="p-3 border-t border-border flex-shrink-0 space-y-1">
        {!forceExpanded && (
          <button
            onClick={toggleSidebar}
            title={sidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
            className={cn('nav-link w-full', sidebarCollapsed && 'justify-center px-0')}
          >
            {sidebarCollapsed
              ? <ChevronRight className="w-[15px] h-[15px]" />
              : <><ChevronLeft className="w-[15px] h-[15px]" /><span className="truncate">Collapse</span></>
            }
          </button>
        )}
        <button
          onClick={handleLogout}
          title={sidebarCollapsed ? 'Logout' : undefined}
          className={cn('nav-link w-full text-left', sidebarCollapsed && 'justify-center px-0')}
        >
          <LogOut className="w-[15px] h-[15px]" />
          {!sidebarCollapsed && <span className="truncate">Logout</span>}
        </button>
      </div>
    </>
  )
}

// ── Desktop sidebar ───────────────────────────────────────────────────────────

export function Sidebar() {
  const sidebarCollapsed = useUIStore((s) => s.sidebarCollapsed)

  return (
    <aside
      className={cn(
        'fixed left-0 top-0 h-full surface-1 border-r border-border flex-col z-30 hidden md:flex transition-[width] duration-200',
        sidebarCollapsed ? 'w-16' : 'w-56',
      )}
    >
      <SidebarContent />
    </aside>
  )
}

// ── Mobile hamburger button ───────────────────────────────────────────────────

export function MobileMenuButton({ open, onToggle }: { open: boolean; onToggle: () => void }) {
  return (
    <button
      onClick={onToggle}
      className="md:hidden flex items-center justify-center w-9 h-9 rounded-lg hover:bg-accent transition-colors"
      aria-label="Toggle menu"
    >
      {open ? <X className="w-5 h-5" /> : <Menu className="w-5 h-5" />}
    </button>
  )
}

// ── Mobile drawer ─────────────────────────────────────────────────────────────

export function MobileDrawer({ open, onClose }: { open: boolean; onClose: () => void }) {
  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', handleKey)
    return () => document.removeEventListener('keydown', handleKey)
  }, [onClose])

  useEffect(() => {
    document.body.style.overflow = open ? 'hidden' : ''
    return () => { document.body.style.overflow = '' }
  }, [open])

  if (!open) return null

  return (
    <>
      <div className="fixed inset-0 bg-black/60 backdrop-blur-sm z-40 md:hidden" onClick={onClose} />
      <aside className="fixed left-0 top-0 h-full w-64 bg-card border-r border-border flex flex-col z-50 md:hidden animate-slide-in">
        <SidebarContent onNavigate={onClose} forceExpanded />
      </aside>
    </>
  )
}
