'use client'
import { useState } from 'react'
import { AlertOctagon, CheckCircle2, ShieldCheck, XCircle, TrendingDown, Power, PowerOff } from 'lucide-react'
import {
  useSettings, useEmergencyKillSwitch, useEmergencyDisableKillSwitch, useEmergencyAutoTradingOff,
  useEmergencyAutoTradingOn, useEmergencyCancelAll, useEmergencyFlattenAll,
} from '@/hooks/use-api'
import { Button, Card, CardContent, Spinner, PageHeader } from '@/components/ui'
import { ConfirmDialog } from '@/components/shared/confirm-dialog'
import { cn } from '@/lib/utils'

interface EmergencyAction {
  id: string
  testId: string
  label: string
  description: string
  confirmTitle: string
  confirmDesc: string
  confirmLabel: string
  icon: React.ElementType
  variant: 'danger' | 'warning'
}

const ACTIONS: EmergencyAction[] = [
  {
    id: 'auto_trading_off',
    testId: 'emergency-action-disable-auto-trading',
    label: 'Disable Auto Trading',
    description: 'Turn off automatic strategy execution. Manual orders are still allowed.',
    confirmTitle: 'Disable auto trading?',
    confirmDesc: 'Strategies will stop executing automatically. You can re-enable this from the dashboard.',
    confirmLabel: 'Disable Auto Trading',
    icon: PowerOff,
    variant: 'warning',
  },
  {
    id: 'cancel_all',
    testId: 'emergency-action-cancel-orders',
    label: 'Cancel All Pending Orders',
    description: 'Cancel all pending, submitted, and working orders at the broker.',
    confirmTitle: 'Cancel all pending orders?',
    confirmDesc: 'All pending and working orders will be cancelled at the broker. Filled orders are not affected.',
    confirmLabel: 'Cancel All Pending',
    icon: XCircle,
    variant: 'danger',
  },
  {
    id: 'flatten_all',
    testId: 'emergency-action-flatten-positions',
    label: 'Flatten All Positions',
    description: 'Close ALL open positions via market sell orders. This will realise any unrealised P&L.',
    confirmTitle: '⚠️ Flatten all positions?',
    confirmDesc: 'ALL open positions will be closed via market sell orders immediately. This realises P&L and cannot be undone.',
    confirmLabel: 'Flatten All Positions',
    icon: TrendingDown,
    variant: 'danger',
  },
]

export default function EmergencyPage() {
  const { data: settings, isLoading } = useSettings()
  const [pending, setPending] = useState<string | null>(null)
  const [recoveryMessage, setRecoveryMessage] = useState<string | null>(null)
  const [recoveryError, setRecoveryError] = useState<string | null>(null)
  const isMockRuntime = (process.env.NEXT_PUBLIC_APP_MODE || 'mock') === 'mock'

  const killSwitch = useEmergencyKillSwitch()
  const disableKillSwitch = useEmergencyDisableKillSwitch()
  const autoOff = useEmergencyAutoTradingOff()
  const autoOn = useEmergencyAutoTradingOn()
  const cancelAll = useEmergencyCancelAll()
  const flattenAll = useEmergencyFlattenAll()

  const execute = async (id: string) => {
    setPending(null)
    setRecoveryError(null)
    if (id === 'kill_switch') {
      setRecoveryMessage(null)
      await killSwitch.mutateAsync()
    }
    if (id === 'disable_kill_switch') {
      try {
        await disableKillSwitch.mutateAsync()
        setRecoveryMessage('Kill switch disabled. Auto-trading remains OFF until manually re-enabled.')
      } catch {
        setRecoveryMessage(null)
        setRecoveryError('Failed to disable kill switch. Check backend connectivity and try again.')
      }
    }
    if (id === 'auto_trading_off') await autoOff.mutateAsync()
    if (id === 'cancel_all') await cancelAll.mutateAsync()
    if (id === 'flatten_all') await flattenAll.mutateAsync()
  }

  const isBusy = killSwitch.isPending || disableKillSwitch.isPending || autoOff.isPending || autoOn.isPending || cancelAll.isPending || flattenAll.isPending
  const killSwitchActive = Boolean(settings?.kill_switch_active)

  if (isLoading) return <div className="flex items-center gap-2 text-muted-foreground text-sm"><Spinner className="w-4 h-4" />Loading...</div>

  return (
    <div className="max-w-3xl space-y-6">
      <PageHeader
        icon={<AlertOctagon className="h-5 w-5" />}
        label="Emergency Controls"
        sub={isMockRuntime ? 'Mock-mode kill switch and simulated unwind · All actions are audit-logged' : 'Kill switch and rapid unwind · All actions are audit-logged'}
      />

      {/* Warning header */}
      <div className="p-4 bg-red-500/8 border border-red-500/25 rounded-xl">
        <div className="flex items-start gap-3">
          <div className="w-9 h-9 rounded-lg bg-red-500/15 border border-red-500/30 flex items-center justify-center flex-shrink-0">
            <AlertOctagon className="w-4 h-4 text-red-400" />
          </div>
          <div>
            <p className="text-sm font-semibold text-red-300">Immediate and irreversible</p>
            <p className="text-xs text-red-300/70 mt-1 leading-relaxed">
              These actions execute immediately and most cannot be undone. {isMockRuntime ? 'In mock mode they affect local simulated state only: no real broker order is placed and no real funds are moved.' : 'In demo/live modes broker-backed actions may contact the configured broker.'} All actions are logged to the audit trail.
            </p>
          </div>
        </div>
      </div>

      {/* Current status */}
      <div className="grid grid-cols-2 gap-4">
        <div className={cn(
          'stat-card',
          settings?.kill_switch_active && 'border-red-500/30 bg-red-500/5'
        )} data-testid="kill-switch-status">
          <p className="stat-label">Kill Switch</p>
          <div className="flex items-center gap-2 mt-2">
            <span className={cn(
              'w-2 h-2 rounded-full',
              settings?.kill_switch_active ? 'bg-red-400 animate-pulse-slow' : 'bg-emerald-400'
            )} />
            <p className={cn(
              'text-base font-semibold',
              settings?.kill_switch_active ? 'text-red-400' : 'text-emerald-400'
            )}>
              {settings?.kill_switch_active ? 'ACTIVE' : 'Inactive'}
            </p>
          </div>
        </div>
        <div className="stat-card" data-testid="auto-trading-status">
          <p className="stat-label">Auto Trading</p>
          <div className="flex items-center gap-2 mt-2">
            <span className={cn(
              'w-2 h-2 rounded-full',
              settings?.auto_trading_enabled ? 'bg-emerald-400 animate-pulse-slow' : 'bg-muted-foreground/40'
            )} />
            <p className={cn(
              'text-base font-semibold',
              settings?.auto_trading_enabled ? 'text-emerald-400' : 'text-muted-foreground'
            )}>
              {settings?.auto_trading_enabled ? 'Enabled' : 'Disabled'}
            </p>
          </div>
        </div>
      </div>

      {recoveryMessage && (
        <div className="rounded-lg border border-emerald-500/25 bg-emerald-500/10 px-4 py-3 text-sm text-emerald-200" data-testid="kill-switch-recovery-message">
          <div className="flex items-start gap-2">
            <CheckCircle2 className="mt-0.5 h-4 w-4 flex-shrink-0" />
            <p>{recoveryMessage}</p>
          </div>
        </div>
      )}

      {recoveryError && (
        <div className="rounded-lg border border-red-500/25 bg-red-500/10 px-4 py-3 text-sm text-red-200">
          {recoveryError}
        </div>
      )}

      {/* Re-enable auto trading */}
      {!settings?.auto_trading_enabled && !settings?.kill_switch_active && (
        <Card className="border-emerald-500/20 bg-emerald-500/[0.03]">
          <CardContent className="p-4 flex items-center justify-between gap-4">
            <div className="flex items-start gap-3">
              <div className="w-9 h-9 rounded-lg bg-emerald-500/10 border border-emerald-500/25 flex items-center justify-center flex-shrink-0">
                <Power className="w-4 h-4 text-emerald-400" />
              </div>
              <div>
                <p className="text-sm font-semibold">Auto trading is disabled</p>
                <p className="text-xs text-muted-foreground mt-0.5">Re-enable to allow strategies to execute trades.</p>
              </div>
            </div>
            <Button variant="success" size="sm" onClick={() => autoOn.mutate()} loading={autoOn.isPending}>
              <Power className="w-3.5 h-3.5" />
              Re-enable
            </Button>
          </CardContent>
        </Card>
      )}

      {/* Kill switch activation / recovery */}
      <Card className={cn(
        'transition-all',
        killSwitchActive
          ? 'border-blue-500/25 bg-blue-500/[0.04]'
          : 'hover:border-red-500/40 hover:shadow-[var(--elev-2)]',
      )}>
        <CardContent className="p-4 flex items-start justify-between gap-4">
          <div className="flex items-start gap-3 min-w-0">
            <div className={cn(
              'w-10 h-10 rounded-xl flex items-center justify-center flex-shrink-0 border',
              killSwitchActive
                ? 'bg-blue-500/10 border-blue-500/25 text-blue-300'
                : 'bg-red-500/10 border-red-500/25 text-red-400',
            )}>
              {killSwitchActive ? <ShieldCheck className="w-4 h-4" /> : <AlertOctagon className="w-4 h-4" />}
            </div>
            <div className="min-w-0">
              <p className="text-sm font-semibold">{killSwitchActive ? 'Kill Switch Recovery' : 'Kill Switch'}</p>
              <p className="text-xs text-muted-foreground mt-1 leading-relaxed">
                {killSwitchActive
                  ? 'Disable the kill switch separately from auto-trading. This clears the emergency block but auto-trading remains OFF until a separate manual re-enable.'
                  : 'Immediately halt ALL automated trading. No new orders will be placed. Existing positions are kept.'}
              </p>
              {isMockRuntime && (
                <p className="mt-2 text-xs text-blue-200/80">
                  Mock mode: this changes local simulated state only. No real broker order is placed and no real funds are moved.
                </p>
              )}
            </div>
          </div>
          <Button
            variant={killSwitchActive ? 'outline' : 'danger'}
            size="sm"
            className={cn('flex-shrink-0', killSwitchActive && 'border-blue-500/35 text-blue-200 hover:bg-blue-500/10')}
            onClick={() => setPending(killSwitchActive ? 'disable_kill_switch' : 'kill_switch')}
            disabled={isBusy}
            loading={killSwitchActive ? disableKillSwitch.isPending : killSwitch.isPending}
            data-testid={killSwitchActive ? 'disable-kill-switch-button' : 'activate-kill-switch-button'}
          >
            {killSwitchActive ? 'Disable Kill Switch' : 'Activate Kill Switch'}
          </Button>
        </CardContent>
      </Card>

      <Card className="border-amber-500/20 bg-amber-500/[0.03]">
        <CardContent className="p-4">
          <p className="text-sm font-semibold text-amber-200">Operator recovery sequence</p>
          <ol className="mt-3 space-y-2 text-xs leading-relaxed text-muted-foreground">
            <li>1. Disable kill switch.</li>
            <li>2. Confirm mock/broker status.</li>
            <li>3. Confirm market data health.</li>
            <li>4. Confirm risk system health.</li>
            <li>5. Re-enable auto-trading separately only if appropriate.</li>
          </ol>
        </CardContent>
      </Card>

      {/* Action cards */}
      <div className="space-y-3">
        <p className="section-title">Emergency Actions</p>
        {ACTIONS.map(action => (
          <Card key={action.id} className={cn(
            'transition-all',
            action.variant === 'danger' && 'hover:border-red-500/40 hover:shadow-[var(--elev-2)]'
          )}>
            <CardContent className="p-4 flex items-start justify-between gap-4">
              <div className="flex items-start gap-3 min-w-0">
                <div className={cn(
                  'w-10 h-10 rounded-xl flex items-center justify-center flex-shrink-0 border',
                  action.variant === 'danger'
                    ? 'bg-red-500/10 border-red-500/25 text-red-400'
                    : 'bg-amber-500/10 border-amber-500/25 text-amber-400'
                )}>
                  <action.icon className="w-4 h-4" />
                </div>
                <div className="min-w-0">
                  <p className="text-sm font-semibold">{action.label}</p>
                  <p className="text-xs text-muted-foreground mt-1 leading-relaxed">{action.description}</p>
                </div>
              </div>
              <Button
                variant={action.variant === 'danger' ? 'danger' : 'outline'}
                size="sm"
                className="flex-shrink-0"
                onClick={() => setPending(action.id)}
                disabled={isBusy}
                data-testid={action.testId}
              >
                {action.confirmLabel}
              </Button>
            </CardContent>
          </Card>
        ))}
      </div>

      {/* Confirm dialogs */}
      {ACTIONS.map(action => (
        <ConfirmDialog
          key={action.id}
          open={pending === action.id}
          onClose={() => setPending(null)}
          onConfirm={() => execute(action.id)}
          title={action.confirmTitle}
          description={action.confirmDesc}
          confirmLabel={action.confirmLabel}
          dangerous={action.variant === 'danger'}
          loading={isBusy}
        />
      ))}
      <ConfirmDialog
        open={pending === 'kill_switch'}
        onClose={() => setPending(null)}
        onConfirm={() => execute('kill_switch')}
        title="⛔ Activate Kill Switch?"
        description="All automated trading will halt immediately. No new orders will be placed until you manually disable the kill switch. Auto-trading will remain off after recovery."
        confirmLabel="Activate Kill Switch"
        dangerous
        loading={isBusy}
        confirmButtonTestId="confirm-activate-kill-switch-button"
      />
      <ConfirmDialog
        open={pending === 'disable_kill_switch'}
        onClose={() => setPending(null)}
        onConfirm={() => execute('disable_kill_switch')}
        title="Disable Kill Switch?"
        description="This clears the emergency kill switch only. Auto-trading remains OFF until manually re-enabled through the separate auto-trading control."
        confirmLabel="Disable Kill Switch"
        loading={isBusy}
        confirmButtonTestId="confirm-disable-kill-switch-button"
      />
    </div>
  )
}
