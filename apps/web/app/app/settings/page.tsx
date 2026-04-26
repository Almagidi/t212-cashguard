'use client'
import { useState } from 'react'
import { useForm } from 'react-hook-form'
import { BellRing, CheckCircle2, Lock, MessageSquare, Monitor, Moon, Save, ShieldAlert, Sun, Unlock, XCircle } from 'lucide-react'
import { useTheme } from 'next-themes'
import { useLiveReadiness, useSettings, useTelegramStatus, useTelegramTestAlert, useUpdateLiveReadiness, useUpdateSettings } from '@/hooks/use-api'
import { Button, Card, CardHeader, CardTitle, CardContent, Input, Label, Spinner } from '@/components/ui'
import { cn } from '@/lib/utils'
import type { LiveReadinessAction } from '@/types'

type PreferencesForm = {
  theme: 'dark' | 'light'
  timezone: string
  daily_stats_reset_time: string
}

function formatVerifiedAt(value: string | null | undefined) {
  if (!value) return 'Not recorded'
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(new Date(value))
}

export default function SettingsPage() {
  const { data: settings, isLoading } = useSettings()
  const { data: liveReadiness, isLoading: readinessLoading } = useLiveReadiness()
  const { data: telegramStatus, isLoading: telegramLoading } = useTelegramStatus()
  const telegramTestAlert = useTelegramTestAlert()
  const updateSettings = useUpdateSettings()
  const updateLiveReadiness = useUpdateLiveReadiness()
  const [pendingAction, setPendingAction] = useState<LiveReadinessAction | null>(null)
  const { theme, setTheme } = useTheme()

  const { register, handleSubmit, watch, setValue, formState: { isDirty } } = useForm<PreferencesForm>({
    values: settings ? {
      theme: settings.theme === 'light' ? 'light' : 'dark',
      timezone: settings.timezone,
      daily_stats_reset_time: settings.daily_stats_reset_time,
    } : undefined,
  })

  const formTheme = watch('theme')

  const onSubmit = (data: PreferencesForm) => updateSettings.mutate(data)

  const runReadinessAction = (action: LiveReadinessAction) => {
    setPendingAction(action)
    updateLiveReadiness.mutate(
      { action },
      { onSettled: () => setPendingAction(null) }
    )
  }

  if (isLoading) return (
    <div className="flex items-center gap-2 text-muted-foreground text-sm">
      <Spinner className="w-4 h-4" /> Loading…
    </div>
  )

  return (
    <div className="max-w-2xl space-y-6">
      <div>
        <h2 className="text-xl font-semibold tracking-tight">Settings</h2>
        <p className="text-[13px] text-muted-foreground mt-1">
          Application preferences, integrations, and live-trading readiness
        </p>
      </div>

      {/* Appearance */}
      <Card>
        <CardHeader>
          <CardTitle>Appearance</CardTitle>
        </CardHeader>
        <CardContent className="space-y-1">
          {(
            [
              { label: 'Light',  value: 'light',  Icon: Sun  },
              { label: 'Dark',   value: 'dark',   Icon: Moon },
              { label: 'System', value: 'system', Icon: Monitor },
            ] as const
          ).map(({ label, value, Icon }) => (
            <button
              key={value}
              type="button"
              onClick={() => setTheme(value)}
              className={cn(
                'kv-row w-full text-left cursor-pointer rounded-lg px-2 transition-colors',
                theme === value
                  ? 'text-primary font-medium'
                  : 'text-muted-foreground hover:text-foreground',
              )}
            >
              <span className="flex items-center gap-2 text-[13px]">
                <Icon className="w-4 h-4" />
                {label}
              </span>
              {theme === value && (
                <span className="text-[10px] font-semibold uppercase tracking-[0.08em] text-primary">
                  Active
                </span>
              )}
            </button>
          ))}
        </CardContent>
      </Card>

      {/* App info */}
      <Card>
        <CardHeader>
          <CardTitle>Application</CardTitle>
        </CardHeader>
        <CardContent>
          <dl className="space-y-0">
            <div className="kv-row">
              <dt className="text-muted-foreground">Version</dt>
              <dd className="font-mono text-foreground">1.0.0</dd>
            </div>
            <div className="kv-row">
              <dt className="text-muted-foreground">Mode</dt>
              <dd className="font-medium capitalize text-foreground">{process.env.NEXT_PUBLIC_APP_MODE ?? 'mock'}</dd>
            </div>
            <div className="kv-row">
              <dt className="text-muted-foreground">Cash-Only Mode</dt>
              <dd className="inline-flex items-center gap-1.5 text-emerald-400 font-medium">
                <CheckCircle2 className="w-3.5 h-3.5" />
                Always Enforced
              </dd>
            </div>
            <div className="kv-row">
              <dt className="text-muted-foreground">Market Data</dt>
              <dd className="text-foreground">{settings?.market_data_provider}</dd>
            </div>
          </dl>
        </CardContent>
      </Card>

      {/* Preferences */}
      <Card>
        <CardHeader><CardTitle>Preferences</CardTitle></CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit(onSubmit)} className="space-y-5">
            {/* Theme */}
            <div className="space-y-2">
              <Label>Theme</Label>
              <div className="flex gap-2">
                {(['dark', 'light'] as const).map(t => (
                  <button
                    key={t} type="button"
                    onClick={() => setValue('theme', t, { shouldDirty: true })}
                    className={cn(
                      'flex items-center gap-2 px-3 py-2 rounded-md border text-sm transition-colors',
                      formTheme === t ? 'border-primary bg-primary/10 text-primary' : 'border-border hover:border-border/80 text-muted-foreground'
                    )}
                  >
                    {t === 'dark' ? <Moon className="w-3.5 h-3.5" /> : <Sun className="w-3.5 h-3.5" />}
                    {t.charAt(0).toUpperCase() + t.slice(1)}
                  </button>
                ))}
              </div>
            </div>

            {/* Timezone */}
            <div className="space-y-1.5">
              <Label htmlFor="timezone">Timezone</Label>
              <Input id="timezone" placeholder="America/New_York" {...register('timezone')} />
              <p className="text-xs text-muted-foreground">Used for session times and EOD flatten.</p>
            </div>

            {/* Daily reset */}
            <div className="space-y-1.5">
              <Label htmlFor="daily_stats_reset_time">Daily Stats Reset Time (HH:MM)</Label>
              <Input id="daily_stats_reset_time" placeholder="00:00" {...register('daily_stats_reset_time')} />
            </div>

            <Button type="submit" size="sm" loading={updateSettings.isPending} disabled={!isDirty}>
              <Save className="w-3.5 h-3.5" />
              Save Settings
            </Button>
          </form>
        </CardContent>
      </Card>

      <Card className="border-border/80">
        <CardHeader><CardTitle>Live Readiness</CardTitle></CardHeader>
        <CardContent className="space-y-5">
          {readinessLoading ? (
            <div className="flex items-center gap-2 text-muted-foreground text-sm">
              <Spinner className="w-4 h-4" />
              Evaluating live readiness...
            </div>
          ) : liveReadiness ? (
            <>
              <div className="grid gap-3 sm:grid-cols-3">
                <div className="rounded-lg border border-border bg-secondary/20 p-3">
                  <p className="text-xs uppercase tracking-[0.18em] text-muted-foreground">Server Mode</p>
                  <p className={cn('mt-2 text-sm font-medium', liveReadiness.mode === 'live' ? 'text-amber-300' : 'text-muted-foreground')}>
                    {liveReadiness.mode.toUpperCase()}
                  </p>
                </div>
                <div className="rounded-lg border border-border bg-secondary/20 p-3">
                  <p className="text-xs uppercase tracking-[0.18em] text-muted-foreground">Execution Flag</p>
                  <p className={cn('mt-2 text-sm font-medium', liveReadiness.live_execution_enabled ? 'text-emerald-400' : 'text-rose-400')}>
                    {liveReadiness.live_execution_enabled ? 'Enabled' : 'Disabled'}
                  </p>
                </div>
                <div className="rounded-lg border border-border bg-secondary/20 p-3">
                  <p className="text-xs uppercase tracking-[0.18em] text-muted-foreground">Status</p>
                  <p className={cn('mt-2 text-sm font-medium', liveReadiness.ready_for_live ? 'text-emerald-400' : liveReadiness.eligible_for_unlock ? 'text-amber-300' : 'text-rose-400')}>
                    {liveReadiness.ready_for_live ? 'Ready' : liveReadiness.eligible_for_unlock ? 'Ready to Unlock' : 'Blocked'}
                  </p>
                </div>
              </div>

              {liveReadiness.blockers.length > 0 && (
                <div className="rounded-lg border border-amber-500/30 bg-amber-500/10 p-4">
                  <p className="text-sm font-medium text-amber-200">Current blockers</p>
                  <div className="mt-2 space-y-2">
                    {liveReadiness.blockers.map((blocker) => (
                      <p key={blocker} className="text-xs text-amber-100/90">
                        {blocker}
                      </p>
                    ))}
                  </div>
                </div>
              )}

              <div className="space-y-3">
                {liveReadiness.checks.map((check) => (
                  <div
                    key={check.key}
                    className="flex items-start justify-between gap-3 rounded-lg border border-border bg-secondary/10 p-3"
                  >
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        {check.status === 'pass' ? (
                          <CheckCircle2 className="h-4 w-4 shrink-0 text-emerald-400" />
                        ) : (
                          <XCircle className="h-4 w-4 shrink-0 text-rose-400" />
                        )}
                        <p className="text-sm font-medium">{check.label}</p>
                      </div>
                      <p className="mt-1 text-xs text-muted-foreground">{check.detail}</p>
                    </div>
                    <p className="shrink-0 text-[11px] text-muted-foreground">
                      {formatVerifiedAt(check.verified_at)}
                    </p>
                  </div>
                ))}
              </div>

              <div className="space-y-3">
                <Label>Checklist Actions</Label>
                <div className="flex flex-wrap gap-2">
                  <Button
                    type="button"
                    size="sm"
                    variant="secondary"
                    loading={pendingAction === 'record_demo_validation'}
                    onClick={() => runReadinessAction('record_demo_validation')}
                  >
                    Record Demo Review
                  </Button>
                  <Button
                    type="button"
                    size="sm"
                    variant="secondary"
                    loading={pendingAction === 'record_broker_test'}
                    disabled={liveReadiness.checks.find((check) => check.key === 'live_broker_test_recent')?.status !== 'pass'}
                    onClick={() => runReadinessAction('record_broker_test')}
                  >
                    Record Broker Review
                  </Button>
                  <Button
                    type="button"
                    size="sm"
                    variant="secondary"
                    loading={pendingAction === 'record_telegram_test'}
                    disabled={liveReadiness.checks.find((check) => check.key === 'telegram_ready')?.status !== 'pass'}
                    onClick={() => runReadinessAction('record_telegram_test')}
                  >
                    Record Telegram Review
                  </Button>
                  <Button
                    type="button"
                    size="sm"
                    variant="secondary"
                    loading={pendingAction === 'record_kill_switch_test'}
                    onClick={() => runReadinessAction('record_kill_switch_test')}
                  >
                    Record Kill Switch Drill
                  </Button>
                  {liveReadiness.live_trading_unlocked ? (
                    <Button
                      type="button"
                      size="sm"
                      variant="secondary"
                      loading={pendingAction === 'lock_live'}
                      onClick={() => runReadinessAction('lock_live')}
                    >
                      <Lock className="h-3.5 w-3.5" />
                      Relock Live Trading
                    </Button>
                  ) : (
                    <Button
                      type="button"
                      size="sm"
                      loading={pendingAction === 'unlock_live'}
                      disabled={!liveReadiness.eligible_for_unlock}
                      onClick={() => runReadinessAction('unlock_live')}
                    >
                      <Unlock className="h-3.5 w-3.5" />
                      Unlock Live Trading
                    </Button>
                  )}
                </div>
                <p className="text-xs text-muted-foreground">
                  Live auto-trading is blocked on the server until every checklist item passes and an admin explicitly unlocks it.
                </p>
              </div>
            </>
          ) : (
            <p className="text-sm text-muted-foreground">Live readiness status is unavailable.</p>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader><CardTitle>Telegram Supervision</CardTitle></CardHeader>
        <CardContent className="space-y-5">
          {telegramLoading ? (
            <div className="flex items-center gap-2 text-muted-foreground text-sm">
              <Spinner className="w-4 h-4" />
              Loading Telegram status...
            </div>
          ) : telegramStatus ? (
            <>
              <div className="grid gap-3 sm:grid-cols-2">
                <div className="rounded-lg border border-border bg-secondary/20 p-3">
                  <div className="flex items-center gap-2 text-xs uppercase tracking-[0.18em] text-muted-foreground">
                    <MessageSquare className="h-3.5 w-3.5" />
                    Bot
                  </div>
                  <p className={cn('mt-2 text-sm font-medium', telegramStatus.bot_configured ? 'text-emerald-400' : 'text-rose-400')}>
                    {telegramStatus.bot_configured ? 'Configured' : 'Missing token'}
                  </p>
                  <p className="mt-1 text-xs text-muted-foreground">
                    Webhook secret {telegramStatus.webhook_secret_configured ? 'configured' : 'not configured'}.
                  </p>
                </div>

                <div className="rounded-lg border border-border bg-secondary/20 p-3">
                  <div className="flex items-center gap-2 text-xs uppercase tracking-[0.18em] text-muted-foreground">
                    <ShieldAlert className="h-3.5 w-3.5" />
                    Control
                  </div>
                  <p className={cn('mt-2 text-sm font-medium', telegramStatus.control_enabled ? 'text-emerald-400' : 'text-amber-400')}>
                    {telegramStatus.control_enabled ? 'Enabled' : 'Allowlist incomplete'}
                  </p>
                  <p className="mt-1 text-xs text-muted-foreground">
                    {telegramStatus.allowed_chat_count} chat allowlist entries, {telegramStatus.allowed_user_count} user allowlist entries.
                  </p>
                </div>
              </div>

              <div className="rounded-lg border border-border bg-secondary/10 p-4">
                <div className="flex items-center justify-between gap-4">
                  <div>
                    <p className="text-sm font-medium">Delivery</p>
                    <p className="mt-1 text-xs text-muted-foreground">
                      Alert chat {telegramStatus.alert_chat_configured ? 'configured' : 'not configured'}.
                      Confirmation window: {telegramStatus.confirmation_window_seconds}s.
                    </p>
                  </div>
                  <Button
                    type="button"
                    size="sm"
                    variant="secondary"
                    onClick={() => telegramTestAlert.mutate()}
                    loading={telegramTestAlert.isPending}
                    disabled={!telegramStatus.bot_configured || !telegramStatus.alert_chat_configured}
                  >
                    <BellRing className="h-3.5 w-3.5" />
                    Send Test Alert
                  </Button>
                </div>
              </div>

              <div className="space-y-2">
                <Label>Supported Commands</Label>
                <div className="flex flex-wrap gap-2">
                  {telegramStatus.supported_commands.map((command) => (
                    <span
                      key={command}
                      className="rounded-full border border-border bg-secondary/20 px-2.5 py-1 text-xs text-foreground/90"
                    >
                      {command}
                    </span>
                  ))}
                </div>
                <p className="text-xs text-muted-foreground">
                  Sensitive commands require a short-lived confirmation code and are limited to allowlisted chats/users.
                </p>
              </div>
            </>
          ) : (
            <p className="text-sm text-muted-foreground">Telegram status is unavailable.</p>
          )}
        </CardContent>
      </Card>

      {/* Safety notice */}
      <Card className="border-amber-500/20">
        <CardContent className="p-4">
          <p className="text-xs text-amber-400 font-medium mb-1">Safety Guarantees</p>
          <ul className="text-xs text-muted-foreground space-y-1 list-disc list-inside">
            <li>Cash-only mode is permanently enforced — cannot be disabled via UI</li>
            <li>Live trading requires explicit environment flags plus a completed readiness checklist</li>
            <li>No deposit, withdrawal, or bank integration code exists in this application</li>
            <li>All credentials are encrypted at rest with AES-256</li>
          </ul>
        </CardContent>
      </Card>
    </div>
  )
}
