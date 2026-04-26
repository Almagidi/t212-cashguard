'use client'
import { useState } from 'react'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { ShieldAlert, ShieldOff, Save, AlertTriangle } from 'lucide-react'
import { useRiskProfile, useUpdateRiskProfile, useRiskEvents, useKillSwitch, useSettings } from '@/hooks/use-api'
import { Button, Card, CardHeader, CardTitle, CardContent, Input, Label, Spinner, PageHeader } from '@/components/ui'
import { ConfirmDialog } from '@/components/shared/confirm-dialog'
import { formatDate, cn } from '@/lib/utils'

const schema = z.object({
  max_risk_per_trade_pct: z.coerce.number().min(0.1).max(10),
  max_daily_loss_pct: z.coerce.number().min(0.1).max(20),
  max_open_positions: z.coerce.number().int().min(1).max(50),
  max_position_size_pct: z.coerce.number().min(0.5).max(100),
  max_trades_per_day: z.coerce.number().int().min(1).max(200),
  stop_after_consecutive_losses: z.coerce.number().int().min(0).max(20),
  symbol_cooldown_seconds: z.coerce.number().int().min(0).max(86400),
  force_flat_eod: z.boolean(),
})
type FormData = z.infer<typeof schema>

const RISK_EVENT_COLORS: Record<string, string> = {
  kill_switch_on: 'text-red-400', kill_switch_off: 'text-emerald-400',
  cash_guard_block: 'text-amber-400', duplicate_order_block: 'text-amber-400',
  daily_loss_breach: 'text-red-400', max_positions_block: 'text-amber-400',
  position_size_block: 'text-amber-400', max_trades_block: 'text-amber-400',
}

export default function RiskPage() {
  const { data: profile, isLoading } = useRiskProfile()
  const { data: events = [] } = useRiskEvents({ limit: 50 })
  const { data: settings } = useSettings()
  const updateProfile = useUpdateRiskProfile()
  const killSwitch = useKillSwitch()
  const [showKillSwitch, setShowKillSwitch] = useState(false)
  const ksActive = settings?.kill_switch_active ?? false

  const { register, handleSubmit, formState: { errors, isDirty } } = useForm<FormData>({
    resolver: zodResolver(schema),
    values: profile ? {
      max_risk_per_trade_pct: Number(profile.max_risk_per_trade_pct),
      max_daily_loss_pct: Number(profile.max_daily_loss_pct),
      max_open_positions: profile.max_open_positions,
      max_position_size_pct: Number(profile.max_position_size_pct),
      max_trades_per_day: profile.max_trades_per_day,
      stop_after_consecutive_losses: profile.stop_after_consecutive_losses,
      symbol_cooldown_seconds: profile.symbol_cooldown_seconds,
      force_flat_eod: profile.force_flat_eod,
    } : undefined,
  })

  const onSubmit = (data: FormData) => updateProfile.mutate(data as any)

  if (isLoading) return <div className="flex items-center gap-2 text-muted-foreground text-sm"><Spinner className="w-4 h-4" />Loading...</div>

  return (
    <div className="max-w-3xl space-y-6">
      <PageHeader
        icon={<ShieldAlert className="h-5 w-5" />}
        label="Risk Controls"
        sub="Limits, guards, and circuit breakers"
      />

      {/* Kill switch status */}
      <div className={cn(
        'flex items-center justify-between p-4 rounded-xl border transition-colors',
        ksActive
          ? 'bg-red-500/8 border-red-500/30'
          : 'bg-card border-border'
      )}>
        <div className="flex items-center gap-3">
          <div className={cn(
            'w-10 h-10 rounded-xl flex items-center justify-center border',
            ksActive
              ? 'bg-red-500/15 border-red-500/30 text-red-400'
              : 'bg-emerald-500/10 border-emerald-500/20 text-emerald-400'
          )}>
            {ksActive ? <ShieldOff className="w-4 h-4" /> : <ShieldAlert className="w-4 h-4" />}
          </div>
          <div>
            <p className={cn('text-sm font-semibold', ksActive && 'text-red-300')}>Kill Switch</p>
            <p className="text-xs text-muted-foreground mt-0.5">
              {ksActive ? 'All automated trading is halted' : 'System operating normally'}
            </p>
          </div>
        </div>
        <Button
          variant={ksActive ? 'outline' : 'danger'}
          size="sm"
          onClick={() => setShowKillSwitch(true)}
          loading={killSwitch.isPending}
        >
          {ksActive ? 'Deactivate' : 'Activate Kill Switch'}
        </Button>
      </div>

      {/* Risk profile form */}
      <Card>
        <CardHeader>
          <CardTitle>Risk Profile — {profile?.name}</CardTitle>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit(onSubmit)} className="space-y-5">
            <div className="grid grid-cols-2 gap-4">
              <Field label="Max Risk Per Trade (%)" error={errors.max_risk_per_trade_pct?.message}>
                <Input type="number" step="0.1" {...register('max_risk_per_trade_pct')} />
              </Field>
              <Field label="Max Daily Loss (%)" error={errors.max_daily_loss_pct?.message}>
                <Input type="number" step="0.1" {...register('max_daily_loss_pct')} />
              </Field>
              <Field label="Max Open Positions" error={errors.max_open_positions?.message}>
                <Input type="number" {...register('max_open_positions')} />
              </Field>
              <Field label="Max Position Size (%)" error={errors.max_position_size_pct?.message}>
                <Input type="number" step="0.5" {...register('max_position_size_pct')} />
              </Field>
              <Field label="Max Trades Per Day" error={errors.max_trades_per_day?.message}>
                <Input type="number" {...register('max_trades_per_day')} />
              </Field>
              <Field label="Stop After N Consecutive Losses" error={errors.stop_after_consecutive_losses?.message}>
                <Input type="number" {...register('stop_after_consecutive_losses')} />
              </Field>
              <Field label="Symbol Cooldown (seconds)" error={errors.symbol_cooldown_seconds?.message}>
                <Input type="number" {...register('symbol_cooldown_seconds')} />
              </Field>
              <Field label="Force Flatten at End of Day">
                <div className="flex items-center gap-2 mt-2">
                  <input type="checkbox" id="force_flat_eod" {...register('force_flat_eod')} className="w-4 h-4 rounded" />
                  <label htmlFor="force_flat_eod" className="text-sm text-muted-foreground">Enabled</label>
                </div>
              </Field>
            </div>

            <Button type="submit" size="sm" loading={updateProfile.isPending} disabled={!isDirty}>
              <Save className="w-3.5 h-3.5" />
              Save Changes
            </Button>
          </form>
        </CardContent>
      </Card>

      {/* Risk events */}
      <Card>
        <CardHeader><CardTitle>Risk Events</CardTitle></CardHeader>
        <CardContent>
          {events.length === 0 ? (
            <p className="text-sm text-muted-foreground py-4 text-center">No risk events recorded.</p>
          ) : (
            <div className="space-y-0">
              {events.map(ev => (
                <div key={ev.id} className="flex items-start justify-between py-2.5 border-b border-border/50 last:border-0 gap-4">
                  <div className="flex items-start gap-2">
                    <AlertTriangle className={cn('w-3.5 h-3.5 mt-0.5 flex-shrink-0', RISK_EVENT_COLORS[ev.event_type] ?? 'text-muted-foreground')} />
                    <div>
                      <p className={cn('text-xs font-medium', RISK_EVENT_COLORS[ev.event_type])}>{ev.event_type.replace(/_/g, ' ')}</p>
                      {ev.message && <p className="text-xs text-muted-foreground mt-0.5">{ev.message}</p>}
                      {ev.ticker && <p className="text-xs text-muted-foreground">{ev.ticker}</p>}
                    </div>
                  </div>
                  <p className="text-xs text-muted-foreground flex-shrink-0">{formatDate(ev.occurred_at)}</p>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      <ConfirmDialog
        open={showKillSwitch}
        onClose={() => setShowKillSwitch(false)}
        onConfirm={() => { killSwitch.mutate({ active: !ksActive }); setShowKillSwitch(false) }}
        title={ksActive ? 'Deactivate kill switch?' : '⛔ Activate kill switch?'}
        description={ksActive
          ? 'Deactivating allows strategies to resume trading.'
          : 'This immediately halts ALL automated trading. No new orders will be placed until deactivated.'}
        confirmLabel={ksActive ? 'Deactivate' : 'Activate Kill Switch'}
        dangerous={!ksActive}
        loading={killSwitch.isPending}
      />
    </div>
  )
}

function Field({ label, error, children }: { label: string; error?: string; children: React.ReactNode }) {
  return (
    <div className="space-y-1.5">
      <Label>{label}</Label>
      {children}
      {error && <p className="text-xs text-red-400">{error}</p>}
    </div>
  )
}
