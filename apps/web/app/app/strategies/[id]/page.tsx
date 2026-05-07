'use client'
import { use, useEffect, useMemo, useState } from 'react'
import { useForm } from 'react-hook-form'
import { ArrowLeft, Play, Pause, Save, Clock, TrendingUp, ArrowUpRight, ArrowDownRight, FlaskConical, ShieldCheck, CheckCircle2, AlertTriangle } from 'lucide-react'
import {
  usePortfolioStrategyAttribution,
  usePortfolioStrategyMonitoring,
  useStrategy,
  useStrategyIntelligence,
  useStrategyPromotion,
  useToggleStrategy,
  useUpdateStrategyPromotion,
} from '@/hooks/use-api'
import { Button, Card, CardHeader, CardTitle, CardContent, Badge, Input, Label, Spinner, EmptyState } from '@/components/ui'
import { formatCurrency, formatDate, formatPnL, pnlClass, cn } from '@/lib/utils'
import api from '@/services/api'
import toast from 'react-hot-toast'
import { useQueryClient } from '@tanstack/react-query'
import Link from 'next/link'
import { EquityCurve } from '@/components/charts/equity-curve'
import { RegimeBadge } from '@/components/dashboard/regime-badge'

const PORTFOLIO_STRATEGY_COPY: Record<string, string> = {
  buy_hold_core: 'Low-turnover diversified equity sleeve. Best for demo automation when you want a stable baseline instead of frequent trading.',
  equal_weight_rebalance: 'Periodic equal-weight rebalance across a curated stock or ETF basket. Good when you want concentration control and systematic top-up/sell-down rules.',
  cross_sectional_momentum: 'Monthly long-only winner rotation. Higher regime risk and turnover than the core sleeve, but stronger tactical behavior when trends persist.',
  low_volatility_tilt: 'Favors calmer names within the configured universe. Useful when you want smoother demo automation and lower swing intensity.',
  trend_following_tactical: 'Moving-average timing overlay that can hold partial cash. Best when drawdown control matters more than always being fully invested.',
}

export default function StrategyDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params)
  const qc = useQueryClient()
  const { data: strategy, isLoading } = useStrategy(id)
  const toggle = useToggleStrategy()
  const [saving, setSaving] = useState(false)
  const [runningDry, setRunningDry] = useState(false)
  const [promotionActionPending, setPromotionActionPending] = useState<string | null>(null)
  const [signals, setSignals] = useState<any[]>([])
  const [loadingSignals, setLoadingSignals] = useState(false)
  const [paramsText, setParamsText] = useState('{}')
  const isPortfolioStrategy = !!strategy && strategy.type in PORTFOLIO_STRATEGY_COPY
  const { data: promotion, isLoading: promotionLoading } = useStrategyPromotion(id)
  const { data: intelligence, isLoading: intelligenceLoading } = useStrategyIntelligence(id)
  const { data: monitoring, isLoading: monitoringLoading } = usePortfolioStrategyMonitoring(id, isPortfolioStrategy)
  const { data: attribution, isLoading: attributionLoading } = usePortfolioStrategyAttribution(id, isPortfolioStrategy)
  const promotionMutation = useUpdateStrategyPromotion()
  const attributionCurve = useMemo(
    () => (attribution?.timeline ?? []).map((point) => ({ date: point.date.slice(5), pnl: point.equity_pnl })),
    [attribution],
  )

  const { register, handleSubmit, formState: { isDirty } } = useForm({
    values: strategy ? {
      name: strategy.name,
      description: strategy.description ?? '',
      session_start: strategy.session_start,
      session_end: strategy.session_end,
      allowed_tickers: strategy.allowed_tickers.join(', '),
      is_live: strategy.is_live,
    } : undefined,
  })

  useEffect(() => {
    if (!strategy) return
    setParamsText(JSON.stringify(strategy.params ?? {}, null, 2))
  }, [strategy])

  const onSubmit = async (data: any) => {
    setSaving(true)
    try {
      let parsedParams: Record<string, unknown> | undefined
      try {
        parsedParams = JSON.parse(paramsText)
      } catch {
        toast.error('Strategy parameters must be valid JSON')
        return
      }
      await api.updateStrategy(id, {
        name: data.name,
        description: data.description || undefined,
        session_start: data.session_start,
        session_end: data.session_end,
        allowed_tickers: data.allowed_tickers.split(',').map((t: string) => t.trim()).filter(Boolean),
        is_live: !!data.is_live,
        params: parsedParams,
      })
      qc.invalidateQueries({ queryKey: ['strategies'] })
      qc.invalidateQueries({ queryKey: ['strategies', id] })
      qc.invalidateQueries({ queryKey: ['strategies', id, 'promotion-status'] })
      toast.success('Strategy updated')
    } catch {
      toast.error('Update failed')
    } finally {
      setSaving(false)
    }
  }

  const loadSignals = async () => {
    setLoadingSignals(true)
    try {
      const data = await api.getStrategySignals(id)
      setSignals(data)
    } finally {
      setLoadingSignals(false)
    }
  }

  const runDry = async () => {
    setRunningDry(true)
    try {
      const result = await api.dryRunStrategy(id)
      const orderCount = Number((result.summary?.dry_run_orders as number | undefined) ?? 0)
      toast.success(orderCount > 0 ? `Dry run complete: ${orderCount} simulated orders` : result.message)
      qc.invalidateQueries({ queryKey: ['orders'] })
      qc.invalidateQueries({ queryKey: ['strategies'] })
      qc.invalidateQueries({ queryKey: ['strategies', id] })
      qc.invalidateQueries({ queryKey: ['strategies', id, 'promotion-status'] })
      qc.invalidateQueries({ queryKey: ['strategies', id, 'portfolio-monitoring'] })
      qc.invalidateQueries({ queryKey: ['strategies', id, 'portfolio-attribution'] })
      await loadSignals()
    } catch {
      toast.error('Dry run failed')
    } finally {
      setRunningDry(false)
    }
  }

  if (isLoading) return <div className="flex items-center gap-2 text-muted-foreground text-sm"><Spinner className="w-4 h-4" />Loading...</div>
  if (!strategy) return <p className="text-muted-foreground">Strategy not found.</p>

  const hasParamChanges = paramsText !== JSON.stringify(strategy.params ?? {}, null, 2)
  const presetMetadata = (strategy.params?.preset_metadata as Record<string, unknown> | undefined) ?? {}
  const executionMetadata = (strategy.params?.execution_metadata as Record<string, unknown> | undefined) ?? {}
  const riskProfile = strategy.risk_profile ?? null
  const riskTemplateName = riskProfile?.name ?? (typeof presetMetadata.risk_template_name === 'string' ? presetMetadata.risk_template_name : null)
  const presetLabel = typeof presetMetadata.preset_label === 'string' ? presetMetadata.preset_label : null
  const presetCreatedAt = typeof executionMetadata.created_from_preset_at === 'string' ? executionMetadata.created_from_preset_at : null
  const presetCreatedBy = typeof executionMetadata.created_from_preset_by === 'string' ? executionMetadata.created_from_preset_by : null
  const lastDryRunBy = typeof executionMetadata.last_dry_run_requested_by === 'string' ? executionMetadata.last_dry_run_requested_by : null
  const demoChecks = promotion?.checks.filter((item) => item.phase === 'demo') ?? []
  const liveChecks = promotion?.checks.filter((item) => item.phase === 'live') ?? []
  const promotionStageLabel = promotion?.current_stage === 'live_approved'
    ? 'Live approved'
    : promotion?.current_stage === 'demo'
      ? 'Demo promoted'
      : 'Dry run'
  const percent = (value: number) => `${(value * 100).toFixed(0)}%`

  const runPromotionAction = async (
    action: 'record_dry_run_review' | 'promote_to_demo' | 'record_demo_review' | 'promote_to_live' | 'demote_to_dry_run' | 'revoke_live_promotion',
  ) => {
    setPromotionActionPending(action)
    try {
      await promotionMutation.mutateAsync({ id, action })
      qc.invalidateQueries({ queryKey: ['strategies'] })
      qc.invalidateQueries({ queryKey: ['strategies', id] })
      qc.invalidateQueries({ queryKey: ['strategies', id, 'promotion-status'] })
      qc.invalidateQueries({ queryKey: ['strategies', id, 'portfolio-monitoring'] })
      qc.invalidateQueries({ queryKey: ['settings'] })
      qc.invalidateQueries({ queryKey: ['settings', 'live-readiness'] })
    } finally {
      setPromotionActionPending(null)
    }
  }

  return (
    <div className="max-w-3xl space-y-6 animate-fade-in">
      {/* Back + header */}
      <div className="flex items-center gap-3">
        <Link href="/app/strategies">
          <Button variant="ghost" size="icon"><ArrowLeft className="w-4 h-4" /></Button>
        </Link>
        <div className="flex-1">
          <div className="flex items-center gap-2">
            <h2 className="text-lg font-semibold">{strategy.name}</h2>
            <Badge variant={strategy.is_enabled ? 'success' : 'outline'}>
              {strategy.is_enabled ? 'Active' : 'Inactive'}
            </Badge>
            {strategy.is_live ? <span className="badge-live">LIVE</span> : <span className="badge-mock">PAPER / MOCK</span>}
          </div>
          <p className="text-xs text-muted-foreground mt-0.5 capitalize">{strategy.type.replace(/_/g, ' ')} · Last signal: {strategy.last_signal_at ? formatDate(strategy.last_signal_at) : 'Never'}</p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={runDry} loading={runningDry}>
            <FlaskConical className="w-3.5 h-3.5" />Run Paper {isPortfolioStrategy ? 'Rebalance' : 'Check'}
          </Button>
          <Button
            variant="outline" size="sm"
            onClick={() => toggle.mutate({ id: strategy.id, enable: !strategy.is_enabled })}
            loading={toggle.isPending}
          >
            {strategy.is_enabled ? <><Pause className="w-3.5 h-3.5" />Pause</> : <><Play className="w-3.5 h-3.5" />Enable</>}
          </Button>
        </div>
      </div>

      {isPortfolioStrategy && (
        <Card className="border-primary/20 bg-primary/[0.03]">
          <CardContent className="p-5">
            <p className="text-xs font-semibold uppercase tracking-[0.2em] text-primary/80">Portfolio Automation</p>
            <p className="mt-2 text-sm text-muted-foreground leading-relaxed">
              {PORTFOLIO_STRATEGY_COPY[strategy.type]}
            </p>
            <p className="mt-3 text-xs text-muted-foreground">
              This strategy is executed by the portfolio rebalance worker. Keep `is_live` off while validating dry-run orders, then enable it in `demo` mode before considering `live`.
            </p>
          </CardContent>
        </Card>
      )}

      <div className="grid gap-4 lg:grid-cols-[1.05fr_0.95fr]">
        <Card>
          <CardHeader>
            <CardTitle>Risk Template</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            {riskProfile ? (
              <>
                <div className="flex items-center justify-between gap-3">
                  <div>
                    <p className="text-sm font-semibold">{riskProfile.name}</p>
                    <p className="mt-1 text-xs text-muted-foreground">
                      {presetLabel ? `${presetLabel} preset` : 'Attached strategy risk profile'}
                    </p>
                  </div>
                  <Badge variant={riskProfile.name.toLowerCase().includes('demo') ? 'success' : 'outline'}>
                    {riskProfile.name.toLowerCase().includes('demo') ? 'Demo tuned' : 'Custom'}
                  </Badge>
                </div>
                <div className="grid gap-3 sm:grid-cols-2">
                  <div className="rounded-lg border border-border/60 bg-muted/20 px-3 py-2.5">
                    <p className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground">Risk Per Trade</p>
                    <p className="mt-1 text-lg font-semibold">{riskProfile.max_risk_per_trade_pct}%</p>
                  </div>
                  <div className="rounded-lg border border-border/60 bg-muted/20 px-3 py-2.5">
                    <p className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground">Daily Loss Cap</p>
                    <p className="mt-1 text-lg font-semibold">{riskProfile.max_daily_loss_pct}%</p>
                  </div>
                  <div className="rounded-lg border border-border/60 bg-muted/20 px-3 py-2.5">
                    <p className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground">Open Positions</p>
                    <p className="mt-1 text-lg font-semibold">{riskProfile.max_open_positions}</p>
                  </div>
                  <div className="rounded-lg border border-border/60 bg-muted/20 px-3 py-2.5">
                    <p className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground">Position Size</p>
                    <p className="mt-1 text-lg font-semibold">{riskProfile.max_position_size_pct}%</p>
                  </div>
                  <div className="rounded-lg border border-border/60 bg-muted/20 px-3 py-2.5">
                    <p className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground">Trades / Day</p>
                    <p className="mt-1 text-lg font-semibold">{riskProfile.max_trades_per_day}</p>
                  </div>
                  <div className="rounded-lg border border-border/60 bg-muted/20 px-3 py-2.5">
                    <p className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground">Cooldown</p>
                    <p className="mt-1 text-lg font-semibold">{Math.round(riskProfile.symbol_cooldown_seconds / 60)}m</p>
                  </div>
                  <div className="rounded-lg border border-border/60 bg-muted/20 px-3 py-2.5">
                    <p className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground">Loss Streak Halt</p>
                    <p className="mt-1 text-lg font-semibold">{riskProfile.stop_after_consecutive_losses}</p>
                  </div>
                  <div className="rounded-lg border border-border/60 bg-muted/20 px-3 py-2.5">
                    <p className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground">Force Flat EOD</p>
                    <p className="mt-1 text-lg font-semibold">{riskProfile.force_flat_eod ? 'Yes' : 'No'}</p>
                  </div>
                </div>
                <div className="rounded-lg border border-border/60 bg-muted/15 px-3 py-3">
                  <p className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground">Preset Provenance</p>
                  <div className="mt-2 grid gap-2 text-xs sm:grid-cols-2">
                    <div>
                      <p className="text-muted-foreground">Preset</p>
                      <p className="mt-0.5 font-medium">{presetLabel ?? 'Custom strategy'}</p>
                    </div>
                    <div>
                      <p className="text-muted-foreground">Attached Template</p>
                      <p className="mt-0.5 font-medium">{riskTemplateName ?? 'None'}</p>
                    </div>
                    <div>
                      <p className="text-muted-foreground">Created From Preset</p>
                      <p className="mt-0.5 font-medium">{presetCreatedAt ? formatDate(presetCreatedAt) : 'Not recorded'}</p>
                    </div>
                    <div>
                      <p className="text-muted-foreground">Created By</p>
                      <p className="mt-0.5 font-medium">{presetCreatedBy ?? 'Not recorded'}</p>
                    </div>
                  </div>
                </div>
              </>
            ) : (
              <EmptyState
                icon={<ShieldCheck className="w-10 h-10" />}
                title="No risk template attached"
                description="This strategy should have an attached risk profile before moving from dry-run to demo execution."
              />
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Promotion Pipeline</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            {promotionLoading || !promotion ? (
              <div className="flex items-center gap-2 text-sm text-muted-foreground">
                <Spinner className="w-4 h-4" /> Loading promotion status…
              </div>
            ) : (
              <>
                <p className="text-xs leading-relaxed text-muted-foreground">
                  This strategy has to earn each execution stage. Dry-run evidence, demo soak quality, and manual review all have to line up before the next step unlocks.
                </p>
                <div className="rounded-lg border border-border/60 bg-muted/20 px-3 py-2.5">
                  <p className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground">Current Stage</p>
                  <p className={cn(
                    'mt-1 text-sm font-semibold',
                    promotion.current_stage === 'live_approved'
                      ? 'text-emerald-400'
                      : promotion.current_stage === 'demo'
                        ? 'text-sky-400'
                        : 'text-amber-300',
                  )}>
                    {promotionStageLabel}
                  </p>
                  <p className="mt-2 text-xs text-muted-foreground">
                    Broker execution flag: {promotion.broker_execution_enabled ? 'Enabled' : 'Dry-run only'}.
                    {' '}Recommended next action: {promotion.recommended_next_action ?? 'None'}.
                  </p>
                  <p className="mt-2 text-xs text-muted-foreground">
                    Last dry-run owner: {lastDryRunBy ?? 'Not recorded yet'}
                  </p>
                </div>

                <div className="grid gap-3 sm:grid-cols-2">
                  <div className="rounded-lg border border-border/60 bg-muted/15 px-3 py-2.5">
                    <p className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground">Dry-Run Evidence</p>
                    <p className="mt-1 text-sm font-semibold">
                      {promotion.metrics.dry_run_signal_count} signals · {promotion.metrics.dry_run_days} day(s)
                    </p>
                    <p className="mt-2 text-xs text-muted-foreground">
                      Review recorded: {promotion.metrics.dry_run_reviewed_at ? formatDate(promotion.metrics.dry_run_reviewed_at) : 'Not yet'}
                    </p>
                  </div>
                  <div className="rounded-lg border border-border/60 bg-muted/15 px-3 py-2.5">
                    <p className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground">Demo Soak</p>
                    <p className="mt-1 text-sm font-semibold">
                      {promotion.metrics.demo_order_count} orders · fill {percent(promotion.metrics.demo_fill_rate)}
                    </p>
                    <p className="mt-2 text-xs text-muted-foreground">
                      Error rate {percent(promotion.metrics.demo_error_rate)} · risk blocks {promotion.metrics.demo_risk_block_count}/{promotion.metrics.demo_signal_count || 0}
                    </p>
                  </div>
                </div>

                <div className="space-y-2">
                  <p className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground">Demo Promotion Checks</p>
                  {demoChecks.map((item) => (
                    <div key={item.key} className="rounded-lg border border-border/60 px-3 py-2.5">
                      <div className="flex items-start gap-2">
                        {item.status === 'pass' ? (
                          <CheckCircle2 className="mt-0.5 w-4 h-4 text-emerald-400" />
                        ) : (
                          <AlertTriangle className="mt-0.5 w-4 h-4 text-amber-400" />
                        )}
                        <div className="min-w-0">
                          <p className="text-sm font-medium">{item.label}</p>
                          <p className="mt-1 text-xs text-muted-foreground">{item.detail}</p>
                        </div>
                      </div>
                    </div>
                  ))}
                </div>

                <div className="space-y-2">
                  <p className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground">Live Approval Checks</p>
                  {liveChecks.map((item) => (
                    <div key={item.key} className="rounded-lg border border-border/60 px-3 py-2.5">
                      <div className="flex items-start gap-2">
                        {item.status === 'pass' ? (
                          <CheckCircle2 className="mt-0.5 w-4 h-4 text-emerald-400" />
                        ) : (
                          <AlertTriangle className="mt-0.5 w-4 h-4 text-amber-400" />
                        )}
                        <div className="min-w-0">
                          <p className="text-sm font-medium">{item.label}</p>
                          <p className="mt-1 text-xs text-muted-foreground">{item.detail}</p>
                        </div>
                      </div>
                    </div>
                  ))}
                </div>

                <div className="rounded-lg border border-border/60 bg-muted/20 px-3 py-2.5">
                  <p className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground">Current Blockers</p>
                  {promotion.blockers.length === 0 ? (
                    <p className="mt-2 text-sm text-emerald-400">No active blockers. The current gate is clear.</p>
                  ) : (
                    <div className="mt-2 space-y-2">
                      {promotion.blockers.slice(0, 5).map((blocker) => (
                        <p key={blocker} className="text-xs text-muted-foreground leading-relaxed">• {blocker}</p>
                      ))}
                    </div>
                  )}
                </div>

                <div className="grid gap-2">
                  {!promotion.metrics.dry_run_reviewed_at && (
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={promotion.metrics.dry_run_signal_count <= 0}
                      loading={promotionActionPending === 'record_dry_run_review'}
                      onClick={() => runPromotionAction('record_dry_run_review')}
                    >
                      <CheckCircle2 className="w-3.5 h-3.5" />
                      Record Dry-Run Review
                    </Button>
                  )}
                  {!promotion.demo_execution_enabled && (
                    <Button
                      size="sm"
                      disabled={!promotion.eligible_for_demo}
                      loading={promotionActionPending === 'promote_to_demo'}
                      onClick={() => runPromotionAction('promote_to_demo')}
                    >
                      <Play className="w-3.5 h-3.5" />
                      Turn On Demo Broker Execution
                    </Button>
                  )}
                  {promotion.demo_execution_enabled && !promotion.metrics.demo_reviewed_at && (
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={promotion.metrics.demo_order_count <= 0}
                      loading={promotionActionPending === 'record_demo_review'}
                      onClick={() => runPromotionAction('record_demo_review')}
                    >
                      <CheckCircle2 className="w-3.5 h-3.5" />
                      Record Demo Review
                    </Button>
                  )}
                  {!promotion.live_execution_approved && (
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={!promotion.eligible_for_live}
                      loading={promotionActionPending === 'promote_to_live'}
                      onClick={() => runPromotionAction('promote_to_live')}
                    >
                      <ShieldCheck className="w-3.5 h-3.5" />
                      Approve Strategy For Live
                    </Button>
                  )}
                  {promotion.demo_execution_enabled && (
                    <Button
                      size="sm"
                      variant="ghost"
                      loading={promotionActionPending === 'demote_to_dry_run'}
                      onClick={() => runPromotionAction('demote_to_dry_run')}
                    >
                      <Pause className="w-3.5 h-3.5" />
                      Move Back To Dry-Run
                    </Button>
                  )}
                  {promotion.live_execution_approved && (
                    <Button
                      size="sm"
                      variant="ghost"
                      loading={promotionActionPending === 'revoke_live_promotion'}
                      onClick={() => runPromotionAction('revoke_live_promotion')}
                    >
                      <AlertTriangle className="w-3.5 h-3.5" />
                      Revoke Live Approval
                    </Button>
                  )}
                </div>
              </>
            )}
          </CardContent>
        </Card>
      </div>

      <div className="grid gap-4 lg:grid-cols-[0.9fr_1.1fr]">
        <Card>
          <CardHeader>
            <CardTitle>Trade Decision Context</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            {intelligenceLoading || !intelligence ? (
              <div className="flex items-center gap-2 text-sm text-muted-foreground">
                <Spinner className="w-4 h-4" /> Loading strategy intelligence…
              </div>
            ) : (
              <>
                <div className="space-y-3">
                  <div>
                    <p className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground">Current Regime</p>
                    <div className="mt-2">
                      <RegimeBadge regime={intelligence.regime} />
                    </div>
                    {intelligence.regime.detail && (
                      <p className="mt-2 text-xs text-muted-foreground leading-relaxed">{intelligence.regime.detail}</p>
                    )}
                  </div>
                  <div className="rounded-lg border border-border/60 bg-muted/20 px-3 py-2.5">
                    <div className="flex items-start justify-between gap-3">
                      <div>
                        <p className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground">Feed Health</p>
                        <p className={cn(
                          'mt-1 text-sm font-semibold capitalize',
                          intelligence.feed_health.status === 'ok' || intelligence.feed_health.status === 'fallback'
                            ? 'text-emerald-400'
                            : intelligence.feed_health.status === 'unknown'
                              ? 'text-muted-foreground'
                              : 'text-amber-400',
                        )}>
                          {intelligence.feed_health.status}
                        </p>
                      </div>
                      <Badge variant={intelligence.feed_health.status === 'ok' || intelligence.feed_health.status === 'fallback' ? 'success' : 'warning'}>
                        {intelligence.feed_health.provider}
                      </Badge>
                    </div>
                    <p className="mt-2 text-xs text-muted-foreground leading-relaxed">{intelligence.feed_health.detail}</p>
                  </div>
                </div>

                <div className="rounded-lg border border-border/60 bg-muted/15 px-3 py-2.5">
                  <p className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground">Recent Risk Blocks</p>
                  {intelligence.recent_risk_blocks.length === 0 ? (
                    <p className="mt-2 text-xs text-muted-foreground">No recent intelligence or risk blocks recorded for this strategy’s watchlist.</p>
                  ) : (
                    <div className="mt-2 space-y-2">
                      {intelligence.recent_risk_blocks.slice(0, 4).map((event) => (
                        <div key={event.id} className="rounded-md border border-border/40 px-2.5 py-2">
                          <div className="flex items-center justify-between gap-3">
                            <span className="text-xs font-medium text-foreground">{event.ticker ?? 'market-wide'}</span>
                            <span className="text-[11px] uppercase tracking-[0.14em] text-amber-300">{event.event_type.replace(/_/g, ' ')}</span>
                          </div>
                          <p className="mt-1 text-xs text-muted-foreground">{event.message ?? 'Risk block recorded.'}</p>
                        </div>
                      ))}
                    </div>
                  )}
                </div>

                <div className="rounded-lg border border-border/60 bg-muted/15 px-3 py-2.5">
                  <div className="flex items-center justify-between gap-3">
                    <p className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground">Allocation Decisions</p>
                    <Badge variant={intelligence.recent_allocation_decisions.some((decision) => decision.status === 'rejected') ? 'warning' : 'success'}>
                      {intelligence.recent_allocation_decisions.length} recent
                    </Badge>
                  </div>
                  {intelligence.recent_allocation_decisions.length === 0 ? (
                    <p className="mt-2 text-xs text-muted-foreground">No allocator evidence recorded yet. Future signals will show whether they won capital, lost to portfolio limits, or failed score thresholds.</p>
                  ) : (
                    <div className="mt-2 space-y-2">
                      {intelligence.recent_allocation_decisions.slice(0, 4).map((decision, index) => (
                        <div key={`${decision.generated_at ?? index}-${decision.ticker}`} className="rounded-md border border-border/40 px-2.5 py-2">
                          <div className="flex items-center justify-between gap-3">
                            <span className="text-xs font-medium text-foreground">{decision.ticker} · {decision.side.toUpperCase()}</span>
                            <span className={cn(
                              'text-[11px] uppercase tracking-[0.14em]',
                              decision.status === 'allocated' ? 'text-emerald-400' : 'text-amber-300',
                            )}>
                              {decision.status === 'allocated' ? 'won allocation' : 'lost allocation'} · {(decision.score * 100).toFixed(0)}
                            </span>
                          </div>
                          <p className="mt-1 text-xs text-muted-foreground">{decision.reason}</p>
                          <p className="mt-1 text-[11px] text-muted-foreground">
                            Gross {decision.projected_gross_exposure_pct.toFixed(1)}% / cap {decision.regime_cap_pct.toFixed(1)}% · symbol {decision.projected_symbol_exposure_pct.toFixed(1)}%
                          </p>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              </>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Watchlist Readiness</CardTitle>
          </CardHeader>
          <CardContent>
            {intelligenceLoading || !intelligence ? (
              <div className="flex items-center gap-2 text-sm text-muted-foreground">
                <Spinner className="w-4 h-4" /> Loading watchlist state…
              </div>
            ) : intelligence.watchlist.length === 0 ? (
              <EmptyState
                title="No ranked watchlist yet"
                description="Run the morning scanner or wait for the next scheduled scan to populate strategy-specific decision context."
              />
            ) : (
              <div className="space-y-3">
                {intelligence.watchlist.slice(0, 6).map((candidate) => (
                  <div key={candidate.ticker} className="rounded-xl border border-border/60 bg-muted/10 p-3">
                    <div className="flex items-start justify-between gap-3">
                      <div>
                        <div className="flex items-center gap-2">
                          <p className="text-sm font-semibold">{candidate.ticker}</p>
                          <Badge variant={candidate.trade_safe ? 'success' : 'warning'}>
                            {candidate.trade_safe ? 'eligible' : 'blocked'}
                          </Badge>
                        </div>
                        <p className="mt-1 text-xs text-muted-foreground">
                          Score {candidate.score.toFixed(1)}
                          {candidate.pre_market_rvol != null ? ` · RVOL ${candidate.pre_market_rvol.toFixed(2)}x` : ''}
                          {candidate.gap_pct != null ? ` · Gap ${candidate.gap_pct >= 0 ? '+' : ''}${candidate.gap_pct.toFixed(2)}%` : ''}
                        </p>
                      </div>
                      <span className={cn(
                        'text-[11px] uppercase tracking-[0.16em]',
                        candidate.feed_status === 'ok' || candidate.feed_status === 'fallback' ? 'text-emerald-400' : 'text-amber-300',
                      )}>
                        {candidate.feed_status}
                      </span>
                    </div>
                    <p className="mt-2 text-xs text-muted-foreground leading-relaxed">
                      {candidate.blocked_reason ?? candidate.reason ?? 'No additional routing note recorded.'}
                    </p>
                    {(candidate.catalyst_event_type || candidate.catalyst_summary) && (
                      <div className="mt-2 rounded-lg border border-border/40 bg-background/40 px-2.5 py-2">
                        <p className="text-[11px] uppercase tracking-[0.16em] text-muted-foreground">
                          {candidate.catalyst_event_type ? candidate.catalyst_event_type.replace(/_/g, ' ') : 'Catalyst context'}
                          {candidate.catalyst_score != null ? ` · ${candidate.catalyst_score.toFixed(2)}` : ''}
                        </p>
                        {candidate.catalyst_summary && (
                          <p className="mt-1 text-xs text-muted-foreground">{candidate.catalyst_summary}</p>
                        )}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      {isPortfolioStrategy && (
        <div className="grid gap-4 lg:grid-cols-[1.25fr_0.95fr]">
          <Card>
            <CardHeader>
              <CardTitle>Rebalance Monitoring</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              {monitoringLoading || !monitoring ? (
                <div className="flex items-center gap-2 text-sm text-muted-foreground">
                  <Spinner className="w-4 h-4" /> Loading rebalance state…
                </div>
              ) : (
                <>
                  <div className="grid gap-3 sm:grid-cols-2">
                    <div className="rounded-lg border border-border/60 bg-muted/20 px-3 py-2.5">
                      <p className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground">Last Status</p>
                      <div className="mt-1 flex items-center gap-2">
                        <Badge
                          variant={
                            monitoring.last_status === 'rebalanced'
                              ? 'success'
                              : monitoring.last_status === 'skipped'
                                ? 'warning'
                                : monitoring.last_status === 'error'
                                  ? 'destructive'
                                  : 'outline'
                          }
                        >
                          {monitoring.last_status ?? 'unknown'}
                        </Badge>
                        {monitoring.last_mode && (
                          <span className="text-xs text-muted-foreground uppercase">{monitoring.last_mode}</span>
                        )}
                      </div>
                      {monitoring.last_reason && (
                        <p className="mt-2 text-xs text-muted-foreground">{monitoring.last_reason}</p>
                      )}
                    </div>
                    <div className="rounded-lg border border-border/60 bg-muted/20 px-3 py-2.5">
                      <p className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground">Last Rebalance</p>
                      <p className="mt-1 text-sm font-medium">
                        {monitoring.last_rebalance_at ? formatDate(monitoring.last_rebalance_at) : 'Not yet rebalanced'}
                      </p>
                      <p className="mt-2 text-xs text-muted-foreground">
                        Run: {monitoring.last_run_at ? formatDate(monitoring.last_run_at) : 'Never'}
                      </p>
                    </div>
                    <div className="rounded-lg border border-border/60 bg-muted/20 px-3 py-2.5">
                      <p className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground">Orders Submitted</p>
                      <p className="mt-1 text-lg font-semibold">{monitoring.last_orders_submitted}</p>
                      <p className="mt-2 text-xs text-muted-foreground">Dry-run orders: {monitoring.last_dry_run_orders}</p>
                    </div>
                    <div className="rounded-lg border border-border/60 bg-muted/20 px-3 py-2.5">
                      <p className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground">Risk Blocks</p>
                      <p className={cn('mt-1 text-lg font-semibold', monitoring.last_risk_blocks > 0 ? 'text-amber-400' : 'text-emerald-400')}>
                        {monitoring.last_risk_blocks}
                      </p>
                      <p className="mt-2 text-xs text-muted-foreground">
                        Allocator blocks: {monitoring.last_allocation_blocks}
                      </p>
                    </div>
                  </div>

                  <div className="rounded-lg border border-border/60 bg-muted/15 px-3 py-2.5">
                    <div className="flex items-center justify-between gap-3">
                      <Label>Portfolio Allocator</Label>
                      <Badge variant={monitoring.last_allocation_blocks > 0 ? 'warning' : 'success'}>
                        {monitoring.last_allocation_decisions.length} decisions
                      </Badge>
                    </div>
                    {monitoring.last_allocation_decisions.length === 0 ? (
                      <p className="mt-2 text-xs text-muted-foreground">No rebalance allocation decisions stored yet.</p>
                    ) : (
                      <div className="mt-2 space-y-2">
                        {monitoring.last_allocation_decisions.slice(0, 5).map((decision, index) => (
                          <div key={`${decision.generated_at ?? index}-${decision.ticker}`} className="rounded-lg border border-border/40 px-3 py-2.5">
                            <div className="flex items-start justify-between gap-3">
                              <div>
                                <p className="text-sm font-medium">
                                  {decision.ticker} · <span className={decision.side === 'buy' ? 'text-emerald-400' : 'text-red-400'}>{decision.side.toUpperCase()}</span>
                                </p>
                                <p className="mt-1 text-xs text-muted-foreground">{decision.reason}</p>
                              </div>
                              <Badge variant={decision.status === 'allocated' ? 'success' : 'warning'}>
                                {decision.status === 'allocated' ? 'won' : 'lost'} {(decision.score * 100).toFixed(0)}
                              </Badge>
                            </div>
                            <p className="mt-2 text-[11px] text-muted-foreground">
                              Gross {decision.projected_gross_exposure_pct.toFixed(1)}% / cap {decision.regime_cap_pct.toFixed(1)}% · symbol {decision.projected_symbol_exposure_pct.toFixed(1)}%
                            </p>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>

                  <div className="space-y-2">
                    <div className="flex items-center justify-between">
                      <Label>Weights</Label>
                      <span className="text-xs text-muted-foreground">Target vs current sleeve allocations</span>
                    </div>
                    {monitoring.weights.length === 0 ? (
                      <p className="text-xs text-muted-foreground">No weight snapshot saved yet. Run a dry rebalance first.</p>
                    ) : (
                      <div className="space-y-2">
                        {monitoring.weights.map((weight) => (
                          <div key={weight.ticker} className="rounded-lg border border-border/60 px-3 py-2.5">
                            <div className="flex items-center justify-between gap-3">
                              <span className="text-sm font-medium">{weight.ticker}</span>
                              <span className={cn(
                                'text-xs font-medium',
                                (weight.delta_weight ?? 0) > 0.002 ? 'text-emerald-400' : (weight.delta_weight ?? 0) < -0.002 ? 'text-red-400' : 'text-muted-foreground',
                              )}>
                                {weight.delta_weight == null ? '—' : `${(weight.delta_weight * 100).toFixed(2)}% gap`}
                              </span>
                            </div>
                            <div className="mt-2 grid grid-cols-3 gap-2 text-xs">
                              <div>
                                <p className="text-muted-foreground">Target</p>
                                <p className="mt-0.5 font-medium">{weight.target_weight == null ? '—' : `${(weight.target_weight * 100).toFixed(2)}%`}</p>
                              </div>
                              <div>
                                <p className="text-muted-foreground">Current</p>
                                <p className="mt-0.5 font-medium">{weight.current_weight == null ? '—' : `${(weight.current_weight * 100).toFixed(2)}%`}</p>
                              </div>
                              <div>
                                <p className="text-muted-foreground">Drift</p>
                                <p className="mt-0.5 font-medium">{weight.delta_weight == null ? '—' : `${(weight.delta_weight * 100).toFixed(2)}%`}</p>
                              </div>
                            </div>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                </>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Recent Rebalance Orders</CardTitle>
            </CardHeader>
            <CardContent>
              {monitoringLoading || !monitoring ? (
                <div className="flex items-center gap-2 text-sm text-muted-foreground">
                  <Spinner className="w-4 h-4" /> Loading recent orders…
                </div>
              ) : monitoring.recent_orders.length === 0 ? (
                <EmptyState title="No rebalance orders yet" description="Run a dry rebalance or wait for the scheduled worker to record the next portfolio action." />
              ) : (
                <div className="space-y-0">
                  {monitoring.recent_orders.map((order) => (
                    <div key={order.order_id} className="border-b border-border/50 py-3 last:border-0">
                      <div className="flex items-start justify-between gap-3">
                        <div>
                          <p className="text-sm font-medium">
                            {order.ticker} · <span className={cn(order.side === 'buy' ? 'text-emerald-400' : 'text-red-400')}>{order.side.toUpperCase()}</span>
                          </p>
                          <p className="mt-1 text-xs text-muted-foreground">
                            Qty {order.quantity.toFixed(4)}{order.avg_fill_price ? ` @ ${order.avg_fill_price.toFixed(2)}` : ''}
                          </p>
                          <p className="mt-1 text-xs text-muted-foreground">
                            Target weight {order.target_weight == null ? '—' : `${(order.target_weight * 100).toFixed(2)}%`}
                          </p>
                          {order.allocation_reason && (
                            <p className="mt-1 text-xs text-muted-foreground line-clamp-2">
                              Allocation: {order.allocation_reason}
                            </p>
                          )}
                        </div>
                        <div className="text-right">
                          <Badge variant={order.status === 'filled' ? 'success' : order.status === 'error' ? 'destructive' : 'outline'}>
                            {order.status}
                          </Badge>
                          {order.allocation_status && (
                            <p className={cn(
                              'mt-1 text-[11px]',
                              order.allocation_status === 'allocated' ? 'text-emerald-400' : 'text-amber-300',
                            )}>
                              {order.allocation_status === 'allocated' ? 'won' : 'lost'}
                              {order.allocation_score != null ? ` ${(order.allocation_score * 100).toFixed(0)}` : ''}
                            </p>
                          )}
                          <p className="mt-1 text-[11px] text-muted-foreground">{order.is_dry_run ? 'dry run' : 'broker submitted'}</p>
                          <p className="mt-1 text-[11px] text-muted-foreground">{formatDate(order.created_at)}</p>
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>
        </div>
      )}

      {isPortfolioStrategy && (
        <div className="grid gap-4 lg:grid-cols-[1.25fr_0.95fr]">
          <Card>
            <CardHeader>
              <CardTitle>Sleeve Attribution</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              {attributionLoading || !attribution ? (
                <div className="flex items-center gap-2 text-sm text-muted-foreground">
                  <Spinner className="w-4 h-4" /> Loading sleeve P&amp;L…
                </div>
              ) : attribution.timeline.length === 0 ? (
                <EmptyState
                  title="No rebalance attribution yet"
                  description="Run a rebalance first so CashGuard can replay the sleeve ledger and chart the resulting P&L."
                />
              ) : (
                <>
                  <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
                    <div className="rounded-lg border border-border/60 bg-muted/20 px-3 py-2.5">
                      <p className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground">Total P&amp;L</p>
                      <p className={cn('mt-1 text-lg font-semibold', pnlClass(attribution.total_pnl))}>
                        {formatPnL(attribution.total_pnl)}
                      </p>
                    </div>
                    <div className="rounded-lg border border-border/60 bg-muted/20 px-3 py-2.5">
                      <p className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground">Realized</p>
                      <p className={cn('mt-1 text-lg font-semibold', pnlClass(attribution.realized_pnl))}>
                        {formatPnL(attribution.realized_pnl)}
                      </p>
                    </div>
                    <div className="rounded-lg border border-border/60 bg-muted/20 px-3 py-2.5">
                      <p className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground">Unrealized</p>
                      <p className={cn('mt-1 text-lg font-semibold', pnlClass(attribution.unrealized_pnl))}>
                        {formatPnL(attribution.unrealized_pnl)}
                      </p>
                    </div>
                    <div className="rounded-lg border border-border/60 bg-muted/20 px-3 py-2.5">
                      <p className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground">Market Value</p>
                      <p className="mt-1 text-lg font-semibold">{formatCurrency(attribution.current_market_value)}</p>
                    </div>
                    <div className="rounded-lg border border-border/60 bg-muted/20 px-3 py-2.5">
                      <p className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground">Sleeve Return</p>
                      <p className={cn('mt-1 text-lg font-semibold', pnlClass(attribution.total_return_pct))}>
                        {attribution.total_return_pct >= 0 ? '+' : ''}{attribution.total_return_pct.toFixed(2)}%
                      </p>
                    </div>
                    <div className="rounded-lg border border-border/60 bg-muted/20 px-3 py-2.5">
                      <p className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground">Benchmark</p>
                      <p className="mt-1 text-sm font-semibold">{attribution.benchmark_name}</p>
                      <p className={cn('mt-1 text-xs', pnlClass(attribution.benchmark_return_pct))}>
                        {attribution.benchmark_return_pct >= 0 ? '+' : ''}{attribution.benchmark_return_pct.toFixed(2)}%
                      </p>
                    </div>
                    <div className="rounded-lg border border-border/60 bg-muted/20 px-3 py-2.5">
                      <p className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground">Alpha vs Benchmark</p>
                      <p className={cn('mt-1 text-lg font-semibold', pnlClass(attribution.alpha_vs_benchmark_pct))}>
                        {attribution.alpha_vs_benchmark_pct >= 0 ? '+' : ''}{attribution.alpha_vs_benchmark_pct.toFixed(2)}%
                      </p>
                    </div>
                    <div className="rounded-lg border border-border/60 bg-muted/20 px-3 py-2.5">
                      <p className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground">Max Drawdown</p>
                      <p className="mt-1 text-lg font-semibold text-red-400">-{attribution.max_drawdown_pct.toFixed(2)}%</p>
                      <p className="mt-1 text-xs text-muted-foreground">
                        Bench -{attribution.benchmark_max_drawdown_pct.toFixed(2)}%
                      </p>
                    </div>
                  </div>

                  <div className="rounded-xl border border-border/60 bg-card/40 p-3">
                    <div className="mb-2 flex items-center justify-between gap-3">
                      <div>
                        <p className="text-sm font-medium">Historical Rebalance Timeline</p>
                        <p className="text-xs text-muted-foreground">
                          Replayed from recorded rebalance fills and current daily marks
                        </p>
                      </div>
                      <div className="text-right text-xs text-muted-foreground">
                        <p>{attribution.rebalance_days} rebalance day{attribution.rebalance_days !== 1 ? 's' : ''}</p>
                        <p>{attribution.order_count} filled order{attribution.order_count !== 1 ? 's' : ''}</p>
                      </div>
                    </div>
                    <EquityCurve data={attributionCurve} height={220} showGrid />
                  </div>
                </>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Ticker Attribution</CardTitle>
            </CardHeader>
            <CardContent>
              {attributionLoading || !attribution ? (
                <div className="flex items-center gap-2 text-sm text-muted-foreground">
                  <Spinner className="w-4 h-4" /> Loading ticker contributions…
                </div>
              ) : attribution.ticker_attribution.length === 0 ? (
                <EmptyState
                  title="No holdings attributed yet"
                  description="Once the sleeve has filled rebalance orders, symbol-level contribution will appear here."
                />
              ) : (
                <div className="space-y-0">
                  {attribution.ticker_attribution.map((ticker) => (
                    <div key={ticker.ticker} className="border-b border-border/50 py-3 last:border-0">
                      <div className="flex items-start justify-between gap-3">
                        <div>
                          <p className="text-sm font-medium">{ticker.ticker}</p>
                          <p className="mt-1 text-xs text-muted-foreground">
                            {ticker.quantity.toFixed(4)} sh · avg {ticker.avg_cost.toFixed(2)} · mark {ticker.market_price.toFixed(2)}
                          </p>
                        </div>
                        <div className="text-right">
                          <p className={cn('text-sm font-semibold', pnlClass(ticker.total_pnl))}>
                            {formatPnL(ticker.total_pnl)}
                          </p>
                          <p className="mt-1 text-[11px] text-muted-foreground">{ticker.weight_pct.toFixed(1)}% sleeve weight</p>
                        </div>
                      </div>
                      <div className="mt-2 grid grid-cols-3 gap-2 text-xs">
                        <div>
                          <p className="text-muted-foreground">Realized</p>
                          <p className={cn('mt-0.5 font-medium', pnlClass(ticker.realized_pnl))}>{formatPnL(ticker.realized_pnl)}</p>
                        </div>
                        <div>
                          <p className="text-muted-foreground">Unrealized</p>
                          <p className={cn('mt-0.5 font-medium', pnlClass(ticker.unrealized_pnl))}>{formatPnL(ticker.unrealized_pnl)}</p>
                        </div>
                        <div>
                          <p className="text-muted-foreground">Market Value</p>
                          <p className="mt-0.5 font-medium">{formatCurrency(ticker.market_value)}</p>
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>
        </div>
      )}

      {isPortfolioStrategy && (
        <Card>
          <CardHeader>
            <CardTitle>Rebalance Event Timeline</CardTitle>
          </CardHeader>
          <CardContent>
            {attributionLoading || !attribution ? (
              <div className="flex items-center gap-2 text-sm text-muted-foreground">
                <Spinner className="w-4 h-4" /> Loading rebalance history…
              </div>
            ) : attribution.rebalance_events.length === 0 ? (
              <EmptyState
                title="No rebalance events yet"
                description="When the sleeve records filled rebalance orders, CashGuard will show the before/after weight shifts here."
              />
            ) : (
              <div className="space-y-3">
                {attribution.rebalance_events.map((event) => (
                  <div key={event.date} className="rounded-xl border border-border/60 bg-muted/15 p-4">
                    <div className="flex items-start justify-between gap-3">
                      <div>
                        <p className="text-sm font-semibold">{formatDate(event.date)}</p>
                        <p className="mt-1 text-xs text-muted-foreground">
                          {event.order_count} order{event.order_count !== 1 ? 's' : ''} · turnover {formatCurrency(event.turnover_notional)}
                        </p>
                      </div>
                      <div className="text-right">
                        <p className={cn('text-sm font-semibold', pnlClass(event.total_pnl_after))}>{formatPnL(event.total_pnl_after)}</p>
                        <p className="mt-1 text-[11px] text-muted-foreground">P&amp;L after rebalance</p>
                      </div>
                    </div>
                    <div className="mt-3 space-y-2">
                      {event.weights.map((weight) => (
                        <div key={`${event.date}-${weight.ticker}`} className="rounded-lg border border-border/40 px-3 py-2.5">
                          <div className="flex items-center justify-between gap-3">
                            <span className="text-sm font-medium">{weight.ticker}</span>
                            <span className="text-xs text-muted-foreground">
                              Target {weight.target_weight == null ? '—' : `${(weight.target_weight * 100).toFixed(2)}%`}
                            </span>
                          </div>
                          <div className="mt-2 grid gap-2 text-xs sm:grid-cols-4">
                            <div>
                              <p className="text-muted-foreground">Before</p>
                              <p className="mt-0.5 font-medium">{weight.before_weight == null ? '—' : `${(weight.before_weight * 100).toFixed(2)}%`}</p>
                            </div>
                            <div>
                              <p className="text-muted-foreground">After</p>
                              <p className="mt-0.5 font-medium">{weight.after_weight == null ? '—' : `${(weight.after_weight * 100).toFixed(2)}%`}</p>
                            </div>
                            <div>
                              <p className="text-muted-foreground">Gap Before</p>
                              <p className={cn('mt-0.5 font-medium', (weight.before_gap ?? 0) >= 0 ? 'text-amber-300' : 'text-red-400')}>
                                {weight.before_gap == null ? '—' : `${(weight.before_gap * 100).toFixed(2)}%`}
                              </p>
                            </div>
                            <div>
                              <p className="text-muted-foreground">Gap After</p>
                              <p className={cn('mt-0.5 font-medium', Math.abs(weight.after_gap ?? 0) <= 0.002 ? 'text-emerald-400' : (weight.after_gap ?? 0) >= 0 ? 'text-amber-300' : 'text-red-400')}>
                                {weight.after_gap == null ? '—' : `${(weight.after_gap * 100).toFixed(2)}%`}
                              </p>
                            </div>
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {/* Strategy info */}
      <div className="grid grid-cols-2 gap-4">
        <Card className="p-4">
          <p className="text-xs text-muted-foreground">Session Window</p>
          <p className="text-sm font-medium mt-1 flex items-center gap-1.5">
            <Clock className="w-3.5 h-3.5" />{strategy.session_start} – {strategy.session_end}
          </p>
        </Card>
        <Card className="p-4">
          <p className="text-xs text-muted-foreground">Symbols</p>
          <p className="text-sm font-medium mt-1 flex items-center gap-1.5">
            <TrendingUp className="w-3.5 h-3.5" />
            {strategy.allowed_tickers.length > 0 ? strategy.allowed_tickers.join(', ') : 'None configured'}
          </p>
        </Card>
        <Card className="p-4">
          <p className="text-xs text-muted-foreground">EOD Flatten</p>
          <p className={cn('text-sm font-medium mt-1', strategy.eod_flatten ? 'text-emerald-400' : 'text-muted-foreground')}>
            {strategy.eod_flatten ? 'Enabled' : 'Disabled'}
          </p>
        </Card>
        <Card className="p-4">
          <p className="text-xs text-muted-foreground">Extended Hours</p>
          <p className={cn('text-sm font-medium mt-1', strategy.extended_hours ? 'text-emerald-400' : 'text-muted-foreground')}>
            {strategy.extended_hours ? 'Enabled' : 'Disabled'}
          </p>
        </Card>
      </div>

      {/* Edit form */}
      <Card>
        <CardHeader><CardTitle>Configuration</CardTitle></CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit(onSubmit)} className="space-y-4">
            <div className="space-y-1.5">
              <Label>Strategy Name</Label>
              <Input {...register('name')} />
            </div>
            <div className="space-y-1.5">
              <Label>Description</Label>
              <Input {...register('description')} placeholder="Optional description" />
            </div>
            <div className="grid grid-cols-2 gap-4">
              <div className="space-y-1.5">
                <Label>Session Start (HH:MM)</Label>
                <Input {...register('session_start')} placeholder="09:30" />
              </div>
              <div className="space-y-1.5">
                <Label>Session End (HH:MM)</Label>
                <Input {...register('session_end')} placeholder="16:00" />
              </div>
            </div>
            <div className="space-y-1.5">
              <Label>Allowed Tickers (comma separated)</Label>
              <Input {...register('allowed_tickers')} placeholder="AAPL, MSFT, TSLA" />
            </div>
            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <Label htmlFor="is_live">Execution Mode</Label>
                <label className="flex items-center gap-2 text-xs text-muted-foreground">
                  <input id="is_live" type="checkbox" className="h-4 w-4" {...register('is_live')} />
                  Submit broker orders when this strategy runs
                </label>
              </div>
              <p className="text-xs text-muted-foreground">
                Off = dry-run only. On = real execution in the current app mode (`demo` or `live`).
              </p>
            </div>
            <div className="space-y-1.5">
              <Label>Strategy Parameters (JSON)</Label>
              <textarea
                value={paramsText}
                onChange={(event) => setParamsText(event.target.value)}
                rows={Math.max(8, Math.min(18, paramsText.split('\n').length + 2))}
                className="flex w-full rounded-md border border-input bg-transparent px-3 py-2 text-sm font-mono shadow-sm transition-colors placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                placeholder='{"capital_fraction": 0.5, "min_trade_value": 50}'
              />
              {isPortfolioStrategy && (
                <p className="text-xs text-muted-foreground">
                  Useful portfolio fields: `capital_fraction`, `min_trade_value`, `min_weight_delta_pct`, and the strategy-specific lookback settings already used in research.
                </p>
              )}
            </div>
            {Object.keys(strategy.params).length > 0 && (
              <div className="rounded-md border border-border/60 bg-muted/20 px-3 py-2 text-xs text-muted-foreground">
                Last saved params are loaded above. Dry-run this strategy after edits to confirm the rebalance plan looks sensible.
              </div>
            )}
            <Button type="submit" size="sm" loading={saving} disabled={!isDirty && !hasParamChanges}>
              <Save className="w-3.5 h-3.5" />Save Changes
            </Button>
          </form>
        </CardContent>
      </Card>

      {/* Signals */}
      <Card>
        <CardHeader>
          <div className="flex items-center justify-between">
            <CardTitle>Signal History</CardTitle>
            <Button variant="outline" size="sm" onClick={loadSignals} loading={loadingSignals}>
              Load Signals
            </Button>
          </div>
        </CardHeader>
        <CardContent>
          {signals.length === 0 ? (
            <EmptyState title="No signals loaded" description="Click Load Signals to fetch recent signals for this strategy." />
          ) : (
            <div className="space-y-0">
              {signals.map((sig: any) => (
                <div key={sig.id} className="flex items-center justify-between py-2.5 border-b border-border/50 last:border-0">
                  <div className="flex items-center gap-2">
                    {sig.side === 'buy'
                      ? <ArrowUpRight className="w-4 h-4 text-emerald-400" />
                      : <ArrowDownRight className="w-4 h-4 text-red-400" />}
                    <div>
                      <p className="text-sm font-medium">{sig.ticker} · <span className="capitalize text-muted-foreground">{sig.signal_type}</span></p>
                      {sig.reason && <p className="text-xs text-muted-foreground">{sig.reason}</p>}
                    </div>
                  </div>
                  <div className="text-right">
                    <Badge variant={sig.status === 'executed' ? 'success' : sig.risk_rejected ? 'destructive' : 'outline'}>
                      {sig.risk_rejected ? 'risk rejected' : sig.status}
                    </Badge>
                    <p className="text-xs text-muted-foreground mt-1">{formatDate(sig.generated_at)}</p>
                  </div>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
