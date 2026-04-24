'use client'
import Link from 'next/link'
import { usePathname } from 'next/navigation'
import {
  LayoutDashboard, Building2, LineChart, ListOrdered, Briefcase,
  ShieldAlert, Bell, FileBarChart, Settings, AlertOctagon,
  ScrollText, Activity, LogOut, Zap, FlaskConical, BookOpen,
  Menu, X,
} from 'lucide-react'
import { cn } from '@/lib/utils'
import { useAuthStore } from '@/stores/auth'
import { useSettings } from '@/hooks/use-api'
import api from '@/services/api'
import { useRouter } from 'next/navigation'
import { useState, useEffect } from 'react'

const navItems = [
  { href: '/app/dashboard', icon: LayoutDashboard, label: 'Dashboard' },
  { href: '/app/broker', icon: Building2, label: 'Broker' },
  { href: '/app/instruments', icon: Activity, label: 'Instruments' },
  { href: '/app/strategies', icon: LineChart, label: 'Strategies' },
  { href: '/app/orders', icon: ListOrdered, label: 'Orders' },
  { href: '/app/positions', icon: Briefcase, label: 'Positions' },
  { href: '/app/risk', icon: ShieldAlert, label: 'Risk Controls' },
  { href: '/app/backtest', icon: FlaskConical, label: 'Backtest' },
  { href: '/app/alerts', icon: Bell, label: 'Alerts' },
  { href: '/app/reports', icon: FileBarChart, label: 'Reports' },
  { href: '/app/journal', icon: BookOpen, label: 'Trade Journal' },
  { href: '/app/audit', icon: ScrollText, label: 'Audit Log' },
]

const bottomItems = [
  { href: '/app/settings', icon: Settings, label: 'Settings' },
  { href: '/app/emergency', icon: AlertOctagon, label: 'Emergency' },
]

// Nav link helper
function NavLink({
  href, icon: Icon, label, active, danger, onClick,
}: {
  href: string
  icon: React.ElementType
  label: string
  active: boolean
  danger?: boolean
  onClick?: () => void
}) {
  return (
    <Link
      href={href}
      onClick={onClick}
      className={cn(
        'nav-link',
        danger && !active && 'text-red-400/80 hover:!text-red-300 hover:!bg-red-500/10',
        danger && active && 'text-red-300 bg-red-500/10',
        !danger && active && 'nav-link-active',
      )}
    >
      <Icon className={cn('w-[15px] h-[15px] flex-shrink-0', active && !danger && 'text-primary')} />
      <span className="truncate">{label}</span>
    </Link>
  )
}

// ── Sidebar nav content (shared between mobile + desktop) ─────────────────────

function SidebarContent({ onNavigate }: { onNavigate?: () => void }) {
  const pathname = usePathname()
  const { logout } = useAuthStore()
  const router = useRouter()
  const { data: settings } = useSettings()

  const handleLogout = async () => {
    await api.logout()
    logout()
    router.push('/auth/login')
    onNavigate?.()
  }

  return (
    <>
      {/* Logo */}
      <div className="px-5 h-14 border-b border-border flex items-center flex-shrink-0">
        <div className="flex items-center gap-2.5">
          <div className="w-8 h-8 rounded-lg bg-primary flex items-center justify-center shadow-lg shadow-primary/25 ring-1 ring-primary/30">
            <Zap className="w-[18px] h-[18px] text-white" strokeWidth={2.5} />
          </div>
          <div className="min-w-0">
            <p className="text-[13px] font-semibold leading-tight tracking-tight">CashGuard</p>
            <p className="text-[10px] text-muted-foreground/80 uppercase tracking-[0.12em] mt-0.5">Trading 212</p>
          </div>
        </div>
      </div>

      {/* Kill switch warning */}
      {settings?.kill_switch_active && (
        <div className="mx-3 mt-3 px-3 py-2 rounded-lg bg-red-500/10 border border-red-500/30 text-[11px] text-red-400 flex items-center gap-2 flex-shrink-0 font-medium">
          <AlertOctagon className="w-3.5 h-3.5 flex-shrink-0 animate-pulse-slow" />
          Kill Switch Active
        </div>
      )}

      {/* Nav */}
      <nav className="flex-1 overflow-y-auto py-4 px-3 min-h-0 scrollbar-none">
        <p className="section-title px-3 mb-2">Trading</p>
        <div className="space-y-0.5">
          {navItems.map((item) => (
            <NavLink
              key={item.href}
              href={item.href}
              icon={item.icon}
              label={item.label}
              active={pathname.startsWith(item.href)}
              onClick={onNavigate}
            />
          ))}
        </div>
        <div className="mt-5 pt-4 border-t border-border/60">
          <p className="section-title px-3 mb-2">System</p>
          <div className="space-y-0.5">
            {bottomItems.map((item) => (
              <NavLink
                key={item.href}
                href={item.href}
                icon={item.icon}
                label={item.label}
                active={pathname.startsWith(item.href)}
                danger={item.href.includes('emergency')}
                onClick={onNavigate}
              />
            ))}
          </div>
        </div>
      </nav>

      {/* Footer */}
      <div className="p-3 border-t border-border flex-shrink-0">
        <button
          onClick={handleLogout}
          className="nav-link w-full text-left"
        >
          <LogOut className="w-[15px] h-[15px]" />
          Logout
        </button>
      </div>
    </>
  )
}

// ── Desktop sidebar (fixed, hidden on mobile) ─────────────────────────────────

export function Sidebar() {
  return (
    <aside className="fixed left-0 top-0 h-full w-56 surface-1 border-r border-border flex-col z-30 hidden md:flex">
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

// ── Mobile drawer (slides in from left) ──────────────────────────────────────

export function MobileDrawer({ open, onClose }: { open: boolean; onClose: () => void }) {
  // Close on Escape
  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', handleKey)
    return () => document.removeEventListener('keydown', handleKey)
  }, [onClose])

  // Prevent body scroll when open
  useEffect(() => {
    if (open) {
      document.body.style.overflow = 'hidden'
    } else {
      document.body.style.overflow = ''
    }
    return () => { document.body.style.overflow = '' }
  }, [open])

  if (!open) return null

  return (
    <>
      {/* Backdrop */}
      <div
        className="fixed inset-0 bg-black/60 backdrop-blur-sm z-40 md:hidden"
        onClick={onClose}
      />
      {/* Drawer panel */}
      <aside className="fixed left-0 top-0 h-full w-64 bg-card border-r border-border flex flex-col z-50 md:hidden animate-slide-in">
        <SidebarContent onNavigate={onClose} />
      </aside>
    </>
  )
}
