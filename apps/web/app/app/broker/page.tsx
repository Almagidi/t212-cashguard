'use client'
import { useState } from 'react'
import { useForm, useWatch } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { CheckCircle2, XCircle, Plug, PlugZap, Unplug, AlertTriangle, ShieldAlert, KeyRound, Globe2 } from 'lucide-react'
import { useBrokerStatus, useTestBroker, useDisconnectBroker, useAccount } from '@/hooks/use-api'
import { Button, Card, CardHeader, CardTitle, CardContent, Input, Label, Badge, TerminalCard, Spinner, PageHeader } from '@/components/ui'
import { ConfirmDialog } from '@/components/shared/confirm-dialog'
import { formatDate, formatCurrency, cn } from '@/lib/utils'
import api from '@/services/api'
import toast from 'react-hot-toast'
import { useQueryClient } from '@tanstack/react-query'
import type { BrokerDiagnostics, BrokerTestResult } from '@/types'

const schema = z.object({
  api_key: z.string().min(1, 'API key required'),
  api_secret: z.string().min(1, 'API secret required'),
  environment: z.enum(['demo', 'live']),
})
type FormData = z.infer<typeof schema>

function extractBrokerError(error: any): { message: string; diagnostics: BrokerDiagnostics | null } {
  const detail = error?.response?.data?.detail
  if (typeof detail === 'string' && detail.trim()) {
    return { message: detail, diagnostics: null }
  }
  if (detail && typeof detail === 'object') {
    return {
      message: typeof detail.message === 'string' && detail.message.trim() ? detail.message : 'Connection failed',
      diagnostics: detail.diagnostics ?? null,
    }
  }
  return { message: 'Connection failed', diagnostics: null }
}

function BrokerDiagnosticsPanel({ diagnostics }: { diagnostics: BrokerDiagnostics }) {
  const iconByCause = {
    wrong_environment: AlertTriangle,
    invalid_credentials: KeyRound,
    ip_restriction: Globe2,
  } as const

  return (
    <Card className="border-amber-500/20 bg-amber-500/5">
      <CardHeader>
        <div className="flex items-start gap-3">
          <ShieldAlert className="w-5 h-5 text-amber-700 dark:text-amber-300 mt-0.5" />
          <div>
            <CardTitle className="text-base">{diagnostics.title}</CardTitle>
            <p className="text-sm text-muted-foreground mt-1">{diagnostics.summary}</p>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid gap-3 md:grid-cols-3">
          {diagnostics.causes.map((cause) => {
            const Icon = iconByCause[cause.key]
            return (
              <div key={cause.key} className="rounded-lg border border-border/70 bg-background/60 p-3">
                <div className="flex items-center justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <Icon className="w-4 h-4 text-amber-700 dark:text-amber-300" />
                    <p className="text-sm font-medium">{cause.label}</p>
                  </div>
                  <span className={cn(
                    'text-[11px] px-2 py-0.5 rounded-full font-medium',
                    cause.likelihood === 'likely'
                      ? 'bg-amber-500/15 text-amber-800 dark:text-amber-300'
                      : 'bg-sky-500/15 text-sky-700 dark:text-sky-300'
                  )}>
                    {cause.likelihood === 'likely' ? 'Likely' : 'Possible'}
                  </span>
                </div>
                <p className="text-xs text-muted-foreground mt-2 leading-5">{cause.detail}</p>
              </div>
            )
          })}
        </div>

        <div className="rounded-lg border border-border/70 bg-background/60 p-3 text-sm">
          <p className="font-medium">What the app confirmed</p>
          <div className="grid gap-2 mt-2 text-xs text-muted-foreground md:grid-cols-3">
            <div>
              <span className="text-foreground">Environment:</span> {diagnostics.environment}
            </div>
            <div>
              <span className="text-foreground">Broker host:</span> {diagnostics.broker_host}
            </div>
            <div>
              <span className="text-foreground">Response:</span> HTTP {diagnostics.http_status}
            </div>
          </div>
          <p className="text-xs text-muted-foreground mt-3">{diagnostics.note}</p>
        </div>
      </CardContent>
    </Card>
  )
}

export default function BrokerPage() {
  const qc = useQueryClient()
  const { data: status, isLoading } = useBrokerStatus()
  const { data: account } = useAccount()
  const testMutation = useTestBroker()
  const disconnectMutation = useDisconnectBroker()
  const [connecting, setConnecting] = useState(false)
  const [showDisconnect, setShowDisconnect] = useState(false)
  const [liveWarning, setLiveWarning] = useState(false)
  const [connectDiagnostics, setConnectDiagnostics] = useState<BrokerDiagnostics | null>(null)

  const { control, register, handleSubmit, formState: { errors } } = useForm<FormData>({
    resolver: zodResolver(schema),
    defaultValues: { environment: 'demo' },
  })

  const env = useWatch({ control, name: 'environment' })
  const testResult = testMutation.data as BrokerTestResult | undefined
  const isMockRuntime = (process.env.NEXT_PUBLIC_APP_MODE || 'mock') === 'mock' || status?.environment === 'mock'

  const onSubmit = async (data: FormData) => {
    const sanitized = {
      ...data,
      api_key: data.api_key.trim(),
      api_secret: data.api_secret.trim(),
    }

    if (sanitized.environment === 'live') { setLiveWarning(true); return }
    setConnecting(true)
    try {
      await api.connectBroker(sanitized)
      setConnectDiagnostics(null)
      qc.invalidateQueries({ queryKey: ['broker'] })
      toast.success('Broker connected')
    } catch (e: any) {
      const errorState = extractBrokerError(e)
      setConnectDiagnostics(errorState.diagnostics)
      toast.error(errorState.message)
    } finally { setConnecting(false) }
  }

  const diagnosticsToShow = connectDiagnostics ?? testResult?.diagnostics ?? null

  return (
    <div className="max-w-3xl space-y-6">
      <PageHeader
        icon={<PlugZap className="h-5 w-5" />}
        label="Broker Account"
        sub={isMockRuntime ? 'Mock broker status and Trading 212 credential setup boundary' : 'Trading 212 connection and account details'}
      />

      {/* Cash-only enforcement notice */}
      <div className="flex items-start gap-3 p-4 bg-amber-500/8 border border-amber-500/25 rounded-xl text-amber-800 dark:text-amber-300">
        <div className="w-8 h-8 rounded-lg bg-amber-500/15 border border-amber-500/25 flex items-center justify-center flex-shrink-0">
          <ShieldAlert className="w-4 h-4" />
        </div>
        <div className="text-sm">
          <p className="font-semibold">Cash-Only Mode Enforced</p>
          <p className="text-amber-700 dark:text-amber-300/80 mt-1 leading-relaxed">
            This application only trades using existing cash in your Trading 212 account. No deposits, leverage, or bank connections are ever made.
          </p>
        </div>
      </div>

      {status?.credential_state === 'reconnect_required' && (
        <div className="flex items-start gap-3 p-4 bg-red-500/10 border border-red-500/20 rounded-lg text-red-700 dark:text-red-300">
          <AlertTriangle className="w-4 h-4 flex-shrink-0 mt-0.5" />
          <div className="text-sm">
            <p className="font-medium">Broker reconnection required</p>
            <p className="text-red-800/80 dark:text-red-200/80 mt-0.5">
              {status.recovery_hint || 'Saved broker credentials can no longer be decrypted. Re-enter your Trading 212 API key and secret below to restore broker-backed features.'}
            </p>
          </div>
        </div>
      )}

      {diagnosticsToShow && <BrokerDiagnosticsPanel diagnostics={diagnosticsToShow} />}

      {isMockRuntime && (
        <div className="flex items-start gap-3 p-4 bg-sky-500/8 border border-sky-500/25 rounded-xl text-sky-800 dark:text-sky-200">
          <div className="w-8 h-8 rounded-lg bg-sky-500/15 border border-sky-500/25 flex items-center justify-center flex-shrink-0">
            <ShieldAlert className="w-4 h-4" />
          </div>
          <div className="text-sm">
            <p className="font-semibold">Mock broker runtime active</p>
            <p className="text-sky-700 dark:text-sky-200/80 mt-1 leading-relaxed">
              Broker status is synthetic in mock mode. Testing or connecting here does not validate with Trading 212 or prove a real broker account is connected.
            </p>
          </div>
        </div>
      )}

      {/* Current status */}
      {isLoading ? (
        <div className="flex items-center gap-2 text-muted-foreground text-sm"><Spinner className="w-4 h-4" /> Loading...</div>
      ) : status ? (
        <Card>
          <CardHeader>
            <div className="flex items-center justify-between">
              <CardTitle>{isMockRuntime ? 'Mock Broker Status' : 'Active Connection'}</CardTitle>
              <div className="flex items-center gap-2">
                <span className={cn(
                  'text-xs px-2 py-0.5 rounded-full font-medium',
                  status.credential_state === 'reconnect_required'
                    ? 'bg-amber-500/15 text-amber-800 dark:text-amber-300'
                    : status.is_active && status.last_test_ok
                      ? 'bg-emerald-500/15 text-emerald-400'
                      : 'bg-red-500/15 text-red-400'
                )}>
                  {status.credential_state === 'reconnect_required'
                    ? '● Reconnect Required'
                    : isMockRuntime
                      ? '● Mock Adapter'
                      : status.is_active && status.last_test_ok
                        ? '● Connected'
                      : '● Disconnected'}
                </span>
                <span className={cn('text-xs', status.environment === 'mock' ? 'badge-mock' : status.environment === 'demo' ? 'badge-demo' : 'badge-live')}>
                  {status.environment.toUpperCase()}
                </span>
              </div>
            </div>
          </CardHeader>
          <CardContent>
            <div className="grid grid-cols-2 gap-4 mb-4">
              <div>
                <p className="text-xs text-muted-foreground">Account ID</p>
                <p className="text-sm font-mono mt-0.5">{status.account_id ?? '—'}</p>
              </div>
              <div>
                <p className="text-xs text-muted-foreground">Currency</p>
                <p className="text-sm font-medium mt-0.5">{status.account_currency ?? '—'}</p>
              </div>
              <div>
                <p className="text-xs text-muted-foreground">Last Tested</p>
                <p className="text-sm mt-0.5">{formatDate(status.last_test_at)}</p>
              </div>
              <div>
                <p className="text-xs text-muted-foreground">Last Sync</p>
                <p className="text-sm mt-0.5">{formatDate(status.last_sync_at)}</p>
              </div>
            </div>

            {account && (
              <div className="grid grid-cols-3 gap-3 mb-4 pt-4 border-t border-border">
                <TerminalCard label="Total Value" value={formatCurrency(account.total_value, account.currency)} variant="cyan" />
                <TerminalCard label="Cash" value={formatCurrency(account.cash, account.currency)} variant="cyan" />
                <TerminalCard label="Available to Trade" value={formatCurrency(account.free_funds, account.currency)} variant="teal" />
              </div>
            )}

            <div className="flex gap-2 pt-2">
              <Button variant="outline" size="sm" onClick={() => testMutation.mutate()} loading={testMutation.isPending}>
                <PlugZap className="w-3.5 h-3.5" />
                {isMockRuntime ? 'Test Mock Adapter' : 'Test Connection'}
              </Button>
              <Button variant="outline" size="sm" className="text-red-400 hover:text-red-300" onClick={() => setShowDisconnect(true)}>
                <Unplug className="w-3.5 h-3.5" />
                Disconnect
              </Button>
            </div>

            {testResult && (
              <div className={cn('mt-3 p-3 rounded-md text-sm flex items-center gap-2', testResult.is_ok ? 'bg-emerald-500/10 text-emerald-400' : 'bg-red-500/10 text-red-400')}>
                {testResult.is_ok ? <CheckCircle2 className="w-4 h-4" /> : <XCircle className="w-4 h-4" />}
                {testResult.is_ok ? 'Connection test passed' : testResult.error}
              </div>
            )}
          </CardContent>
        </Card>
      ) : null}

      {/* Connect form */}
        <Card>
          <CardHeader>
          <CardTitle>{status?.credential_state === 'reconnect_required' ? 'Reconnect Trading 212' : 'Connect Trading 212'}</CardTitle>
          </CardHeader>
        <CardContent>
          {isMockRuntime && (
            <p className="mb-4 rounded-lg border border-sky-500/20 bg-sky-500/5 px-3 py-2 text-xs text-sky-800 dark:text-sky-200">
              In mock mode this form is not part of the paper-trading demo path. Switch the backend to APP_MODE=demo before using real Trading 212 demo credentials.
            </p>
          )}
          <form onSubmit={handleSubmit(onSubmit)} className="space-y-4">
            <div className="space-y-1.5">
              <Label>Environment</Label>
              <div className="flex gap-3">
                {(['demo', 'live'] as const).map(e => (
                  <label key={e} className={cn(
                    'flex items-center gap-2 px-4 py-2.5 rounded-lg border cursor-pointer text-sm font-medium transition-all',
                    env === e
                      ? 'border-primary/50 bg-primary/10 text-primary shadow-sm ring-2 ring-primary/10'
                      : 'border-border bg-background/40 hover:border-border/90 hover:bg-muted/40'
                  )}>
                    <input type="radio" value={e} {...register('environment')} className="sr-only" />
                    {e === 'live' && <span className="w-2 h-2 rounded-full bg-red-400" />}
                    {e === 'demo' && <span className="w-2 h-2 rounded-full bg-blue-400" />}
                    {e.charAt(0).toUpperCase() + e.slice(1)}
                    {e === 'live' && <Badge variant="destructive" className="text-[10px] ml-1">Restricted</Badge>}
                  </label>
                ))}
              </div>
              <p className="text-xs text-muted-foreground">
                Trading 212 demo and live credentials are environment-specific. A live API key will not authenticate against the demo endpoint, and a demo key will not authenticate against live.
              </p>
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="api_key">API Key</Label>
              <Input id="api_key" placeholder="Your Trading 212 API key" {...register('api_key')} />
              {errors.api_key && <p className="text-xs text-red-400">{errors.api_key.message}</p>}
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="api_secret">API Secret</Label>
              <Input id="api_secret" type="password" placeholder="Your Trading 212 API secret" {...register('api_secret')} />
              {errors.api_secret && <p className="text-xs text-red-400">{errors.api_secret.message}</p>}
              <p className="text-xs text-muted-foreground">Credentials are trimmed before testing, encrypted at rest, and never stored in plaintext.</p>
            </div>

            <Button type="submit" loading={connecting}>
              <Plug className="w-3.5 h-3.5" />
              Connect Broker
            </Button>
          </form>
        </CardContent>
      </Card>

      <ConfirmDialog
        open={showDisconnect}
        onClose={() => setShowDisconnect(false)}
        onConfirm={() => { disconnectMutation.mutate(); setShowDisconnect(false) }}
        title="Disconnect broker?"
        description="This will deactivate the connection. No orders will be affected. You can reconnect at any time."
        confirmLabel="Disconnect"
        dangerous
      />
    </div>
  )
}
