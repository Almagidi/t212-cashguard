'use client'
import { TrendingUp, TrendingDown, Minus, Zap } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { WsRegime } from '@/hooks/use-websocket'
import type { MarketRegime } from '@/types'

interface RegimeBadgeProps {
  regime: WsRegime | MarketRegime | null | undefined
  className?: string
  compact?: boolean
}

const REGIME_CONFIG = {
  trending_up: {
    icon: TrendingUp,
    label: 'Trending Up',
    ring: 'ring-emerald-500/30',
    bg: 'bg-emerald-500/10',
    text: 'text-emerald-400',
    dot: 'bg-emerald-400',
  },
  trending_down: {
    icon: TrendingDown,
    label: 'Trending Down',
    ring: 'ring-red-500/30',
    bg: 'bg-red-500/10',
    text: 'text-red-400',
    dot: 'bg-red-400',
  },
  ranging: {
    icon: Minus,
    label: 'Ranging',
    ring: 'ring-yellow-500/30',
    bg: 'bg-yellow-500/10',
    text: 'text-yellow-400',
    dot: 'bg-yellow-400',
  },
  high_volatility: {
    icon: Zap,
    label: 'High Volatility',
    ring: 'ring-orange-500/30',
    bg: 'bg-orange-500/10',
    text: 'text-orange-400',
    dot: 'bg-orange-400',
  },
  risk_off: {
    icon: TrendingDown,
    label: 'Risk-Off',
    ring: 'ring-rose-500/30',
    bg: 'bg-rose-500/10',
    text: 'text-rose-400',
    dot: 'bg-rose-400',
  },
  unsafe: {
    icon: Zap,
    label: 'Unsafe',
    ring: 'ring-red-500/35',
    bg: 'bg-red-500/12',
    text: 'text-red-400',
    dot: 'bg-red-400',
  },
  unknown: {
    icon: Minus,
    label: 'Unknown',
    ring: 'ring-zinc-500/25',
    bg: 'bg-zinc-500/10',
    text: 'text-zinc-300',
    dot: 'bg-zinc-300',
  },
} as const

export function RegimeBadge({ regime, className, compact = false }: RegimeBadgeProps) {
  if (!regime) {
    return (
      <div className={cn('flex items-center gap-1.5 text-xs text-muted-foreground', className)}>
        <div className="w-1.5 h-1.5 rounded-full bg-muted-foreground animate-pulse" />
        Loading regime…
      </div>
    )
  }

  const cfg = REGIME_CONFIG[regime.regime as keyof typeof REGIME_CONFIG] ?? REGIME_CONFIG.ranging
  const Icon = cfg.icon

  if (compact) {
    return (
      <div className={cn(
        'inline-flex items-center gap-1.5 px-2 py-1 rounded-full text-xs font-medium ring-1',
        cfg.bg, cfg.text, cfg.ring, className,
      )}>
        <div className={cn('w-1.5 h-1.5 rounded-full animate-pulse', cfg.dot)} />
        {cfg.label}
      </div>
    )
  }

  return (
    <div className={cn(
      'flex items-center gap-3 px-4 py-3 rounded-xl ring-1',
      cfg.bg, cfg.ring, className,
    )}>
      <div className={cn('p-2 rounded-lg', cfg.bg)}>
        <Icon className={cn('w-4 h-4', cfg.text)} />
      </div>
      <div className="flex-1 min-w-0">
        <p className={cn('text-sm font-semibold', cfg.text)}>{regime.label}</p>
        <p className="text-[10px] text-muted-foreground mt-0.5">
          ADX {regime.adx} · Vol {regime.vol_percentile}th pct
        </p>
      </div>
      <div className="text-right">
        <p className="text-xs text-muted-foreground">Confidence</p>
        <p className={cn('text-sm font-bold tabular-nums', cfg.text)}>
          {Math.round(regime.confidence * 100)}%
        </p>
      </div>
    </div>
  )
}
