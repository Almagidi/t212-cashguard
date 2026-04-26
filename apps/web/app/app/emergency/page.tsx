'use client'
import { useState } from 'react'
import { AlertOctagon, XCircle, TrendingDown, Power, PowerOff } from 'lucide-react'
import {
  useSettings, useEmergencyKillSwitch, useEmergencyAutoTradingOff,
  useEmergencyAutoTradingOn, useEmergencyCancelAll, useEmergencyFlattenAll,
} from '@/hooks/use-api'
import { Button, Card, CardContent, Spinner, PageHeader } from '@/components/ui'
import { ConfirmDialog } from '@/components/shared/confirm-dialog'
import { cn } from '@/lib/utils'

interface EmergencyAction {
  id: string
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
    id: 'kill_switch',
    label: 'Kill Switch',
    description: 'Immediately halt ALL automated trading. No new orders will be placed. Existing positions are kept.',
    confirmTitle: '⛔ Activate Kill Switch?',
    confirmDesc: 'All automated trading will halt immediately. No new orders will be placed until you manually deactivate the kill switch.',
    confirmLabel: 'Activate Kill Switch',
    icon: AlertOctagon,
    variant: 'danger',
  },
  {
    id: 'auto_trading_off',
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

  const killSwitch = useEmergencyKillSwitch()
  const autoOff = useEmergencyAutoTradingOff()
  const autoOn = useEmergencyAutoTradingOn()
  const cancelAll = useEmergencyCancelAll()
  const flattenAll = useEmergencyFlattenAll()

  const execute = async (id: string) => {
    setPending(null)
    if (id === 'kill_switch') await killSwitch.mutateAsync()
    if (id === 'auto_trading_off') await autoOff.mutateAsync()
    if (id === 'cancel_all') await cancelAll.mutateAsync()
    if (id === 'flatten_all') await flattenAll.mutateAsync()
  }

  const isBusy = killSwitch.isPending || autoOff.isPending || cancelAll.isPending || flattenAll.isPending

  if (isLoading) return <div className="flex items-center gap-2 text-muted-foreground text-sm"><Spinner className="w-4 h-4" />Loading...</div>

  return (
    <div className="max-w-3xl space-y-6">
      <PageHeader
        icon={<AlertOctagon className="h-5 w-5" />}
        label="Emergency Controls"
        sub="Kill switch and rapid unwind · All actions are audit-logged"
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
              These actions execute immediately and most cannot be undone. Use only in emergencies. All actions are logged to the audit trail.
            </p>
          </div>
        </div>
      </div>

      {/* Current status */}
      <div className="grid grid-cols-2 gap-4">
        <div className={cn(
          'stat-card',
          settings?.kill_switch_active && 'border-red-500/30 bg-red-500/5'
        )}>
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
        <div className="stat-card">
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
              >
                Execute
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
    </div>
  )
}
