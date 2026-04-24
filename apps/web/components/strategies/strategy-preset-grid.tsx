'use client'

import type { ReactNode } from 'react'
import {
  Activity,
  Clock3,
  Plus,
  RotateCcw,
  Target,
  TrendingUp,
  Waves,
} from 'lucide-react'

import { Badge, Button, Card, CardContent, CardHeader, CardTitle, Spinner } from '@/components/ui'
import { useCreateStrategyPreset, useStrategyPresets } from '@/hooks/use-api'
import type { StrategyPresetInfo, StrategyPresetKey } from '@/types'
import { cn } from '@/lib/utils'

const PRESET_ICON: Record<StrategyPresetKey, ReactNode> = {
  orb: <Activity className="w-4 h-4" />,
  opening_fade: <RotateCcw className="w-4 h-4" />,
  vwap_reclaim: <Waves className="w-4 h-4" />,
  closing_momentum: <TrendingUp className="w-4 h-4" />,
  intraday_periodicity: <Target className="w-4 h-4" />,
}

const PRESET_ACCENT: Record<StrategyPresetKey, string> = {
  orb: 'text-amber-400 bg-amber-500/10 border-amber-500/25',
  opening_fade: 'text-blue-400 bg-blue-500/10 border-blue-500/25',
  vwap_reclaim: 'text-emerald-400 bg-emerald-500/10 border-emerald-500/25',
  closing_momentum: 'text-fuchsia-400 bg-fuchsia-500/10 border-fuchsia-500/25',
  intraday_periodicity: 'text-cyan-400 bg-cyan-500/10 border-cyan-500/25',
}

function StrategyPresetCard({
  preset,
  compact,
}: {
  preset: StrategyPresetInfo
  compact: boolean
}) {
  const createPreset = useCreateStrategyPreset()

  return (
    <Card className="border-border/70 bg-background/70">
      <CardHeader className={cn(compact ? 'pb-2' : 'pb-3')}>
        <div className="flex items-start justify-between gap-3">
          <div className="flex items-start gap-3">
            <div className={cn('rounded-xl border p-2', PRESET_ACCENT[preset.key])}>
              {PRESET_ICON[preset.key]}
            </div>
            <div className="space-y-1">
              <CardTitle className="text-sm">{preset.label}</CardTitle>
              <p className="text-xs text-muted-foreground">{preset.style}</p>
            </div>
          </div>
          <Badge variant="outline">Dry-run first</Badge>
        </div>
      </CardHeader>
      <CardContent className={cn('space-y-3', compact ? 'pt-0' : 'pt-0')}>
        <p className="text-xs leading-relaxed text-muted-foreground">{preset.description}</p>

        <div className="grid gap-2 text-xs sm:grid-cols-2">
          <div className="rounded-lg border border-border/60 bg-muted/20 px-3 py-2">
            <p className="text-[10px] uppercase tracking-[0.16em] text-muted-foreground">Session</p>
            <p className="mt-1 flex items-center gap-1.5 font-medium text-foreground">
              <Clock3 className="w-3 h-3 text-muted-foreground" />
              {preset.session_window}
            </p>
          </div>
          <div className="rounded-lg border border-border/60 bg-muted/20 px-3 py-2">
            <p className="text-[10px] uppercase tracking-[0.16em] text-muted-foreground">Risk Template</p>
            <p className="mt-1 font-medium text-foreground">{preset.risk_template_name}</p>
          </div>
        </div>

        <div className="space-y-1.5">
          <p className="text-[10px] uppercase tracking-[0.16em] text-muted-foreground">Default Universe</p>
          <p className="text-xs text-muted-foreground">
            {preset.default_tickers.slice(0, compact ? 4 : 6).join(', ')}
            {preset.default_tickers.length > (compact ? 4 : 6) ? '…' : ''}
          </p>
        </div>

        <div className="rounded-lg border border-border/60 bg-muted/20 px-3 py-2.5">
          <p className="text-[10px] uppercase tracking-[0.16em] text-muted-foreground">Demo Guardrails</p>
          <p className="mt-1 text-xs leading-relaxed text-muted-foreground">{preset.risk_summary}</p>
        </div>

        <Button
          size="sm"
          className="w-full"
          loading={createPreset.isPending}
          onClick={() => createPreset.mutate({ key: preset.key })}
        >
          <Plus className="w-3.5 h-3.5" />
          Add {preset.label}
        </Button>
      </CardContent>
    </Card>
  )
}

export function StrategyPresetGrid({
  title = 'Strategy Presets',
  description = 'Create pre-tuned demo strategies without editing JSON by hand.',
  compact = false,
}: {
  title?: string
  description?: string
  compact?: boolean
}) {
  const { data: presets = [], isLoading } = useStrategyPresets()

  return (
    <section className="space-y-3">
      <div>
        <h3 className="text-sm font-semibold">{title}</h3>
        <p className="mt-1 text-xs text-muted-foreground">{description}</p>
      </div>

      {isLoading ? (
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Spinner className="w-4 h-4" /> Loading preset catalog…
        </div>
      ) : (
        <div className={cn('grid gap-3', compact ? 'md:grid-cols-2 xl:grid-cols-3' : 'md:grid-cols-2 xl:grid-cols-3')}>
          {presets.map((preset) => (
            <StrategyPresetCard key={preset.key} preset={preset} compact={compact} />
          ))}
        </div>
      )}
    </section>
  )
}
