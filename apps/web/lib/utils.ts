import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

// ── Number formatting ────────────────────────────────────────────────────────
function toFiniteNumber(value: number | string | null | undefined): number | null {
  if (value === null || value === undefined || value === '') return null
  const n = Number(value)
  return Number.isFinite(n) ? n : null
}

export function formatCurrency(value: number | string | null | undefined, currency = 'USD'): string {
  const n = toFiniteNumber(value)
  if (n === null) return '—'
  return new Intl.NumberFormat('en-US', {
    style: 'currency', currency,
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  }).format(n)
}

export function formatNumber(value: number | string | null | undefined, decimals = 2): string {
  const n = toFiniteNumber(value)
  if (n === null) return '—'
  return new Intl.NumberFormat('en-US', {
    minimumFractionDigits: decimals, maximumFractionDigits: decimals,
  }).format(n)
}

export function formatPercent(value: number | string | null | undefined): string {
  const n = toFiniteNumber(value)
  if (n === null) return '—'
  const sign = n >= 0 ? '+' : ''
  return `${sign}${n.toFixed(2)}%`
}

export function formatPnL(value: number | string | null | undefined, currency = 'USD'): string {
  const n = toFiniteNumber(value)
  if (n === null) return '—'
  const sign = n >= 0 ? '+' : ''
  return `${sign}${formatCurrency(n, currency)}`
}

export function pnlClass(value: number | string | null | undefined): string {
  const n = toFiniteNumber(value)
  if (n === null || n === 0) return 'pnl-neutral'
  return n > 0 ? 'pnl-positive' : 'pnl-negative'
}

// ── Date formatting ──────────────────────────────────────────────────────────
export function formatDate(date: string | null | undefined): string {
  if (!date) return '—'
  return new Intl.DateTimeFormat('en-US', {
    month: 'short', day: 'numeric', year: 'numeric',
    hour: '2-digit', minute: '2-digit',
  }).format(new Date(date))
}

export function formatDateShort(date: string | null | undefined): string {
  if (!date) return '—'
  return new Intl.DateTimeFormat('en-US', {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  }).format(new Date(date))
}

export function timeAgo(date: string | null | undefined): string {
  if (!date) return '—'
  const seconds = Math.floor((Date.now() - new Date(date).getTime()) / 1000)
  if (seconds < 60) return `${seconds}s ago`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`
  return `${Math.floor(seconds / 86400)}d ago`
}

// ── Status helpers ───────────────────────────────────────────────────────────
export function orderStatusColor(status: string): string {
  const map: Record<string, string> = {
    pending_intent: 'text-muted-foreground',
    submitted: 'text-blue-400',
    accepted: 'text-blue-400',
    filled: 'text-emerald-400',
    cancelled: 'text-muted-foreground',
    rejected: 'text-red-400',
    error: 'text-red-400',
  }
  return map[status] ?? 'text-muted-foreground'
}

export function orderStatusBg(status: string): string {
  const map: Record<string, string> = {
    pending_intent: 'bg-muted/50 text-muted-foreground',
    submitted: 'bg-blue-500/15 text-blue-400',
    accepted: 'bg-blue-500/15 text-blue-400',
    filled: 'bg-emerald-500/15 text-emerald-400',
    cancelled: 'bg-muted/50 text-muted-foreground',
    rejected: 'bg-red-500/15 text-red-400',
    error: 'bg-red-500/15 text-red-400',
  }
  return map[status] ?? 'bg-muted/50 text-muted-foreground'
}

export function executionQualityClass(grade: string | null | undefined): string {
  const map: Record<string, string> = {
    excellent: 'text-emerald-400',
    good: 'text-emerald-400',
    watch: 'text-amber-400',
    degraded: 'text-red-400',
    poor: 'text-red-400',
    pending: 'text-muted-foreground',
  }
  return map[grade ?? ''] ?? 'text-muted-foreground'
}

export function executionQualityBadge(grade: string | null | undefined): string {
  const map: Record<string, string> = {
    excellent: 'bg-emerald-500/15 text-emerald-400',
    good: 'bg-emerald-500/15 text-emerald-400',
    watch: 'bg-amber-500/15 text-amber-400',
    degraded: 'bg-red-500/15 text-red-400',
    poor: 'bg-red-500/15 text-red-400',
    pending: 'bg-muted/50 text-muted-foreground',
  }
  return map[grade ?? ''] ?? 'bg-muted/50 text-muted-foreground'
}

export function envBadgeClass(env: string): string {
  if (env === 'live') return 'badge-live'
  if (env === 'demo') return 'badge-demo'
  return 'badge-mock'
}

export function truncate(str: string, len = 16): string {
  return str.length > len ? `${str.slice(0, len)}…` : str
}
