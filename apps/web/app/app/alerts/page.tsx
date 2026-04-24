'use client'
import { Bell, CheckCheck } from 'lucide-react'
import { useAlerts } from '@/hooks/use-api'
import { Button, Card, CardContent, Badge, Spinner, EmptyState } from '@/components/ui'
import { formatDate, cn } from '@/lib/utils'
import { useQueryClient } from '@tanstack/react-query'
import api from '@/services/api'
import toast from 'react-hot-toast'

const SEVERITY_STYLES: Record<string, string> = {
  info: 'bg-blue-500/15 text-blue-400 border-blue-500/20',
  warning: 'bg-amber-500/15 text-amber-400 border-amber-500/20',
  error: 'bg-red-500/15 text-red-400 border-red-500/20',
  critical: 'bg-red-600/20 text-red-300 border-red-600/30',
}

export default function AlertsPage() {
  const qc = useQueryClient()
  const { data: alerts = [], isLoading } = useAlerts({ limit: 100 })
  const unread = alerts.filter(a => !a.is_read).length

  const sendTest = async () => {
    await api.sendTestAlert()
    qc.invalidateQueries({ queryKey: ['alerts'] })
    toast.success('Test alert sent')
  }

  const markRead = async (id: string) => {
    await api.markAlertRead(id)
    qc.invalidateQueries({ queryKey: ['alerts'] })
  }

  return (
    <div className="max-w-3xl space-y-5">
      <div className="flex items-center justify-between gap-4 flex-wrap">
        <div>
          <h2 className="text-xl font-semibold tracking-tight">Alerts</h2>
          <p className="text-[13px] text-muted-foreground mt-1">
            {unread > 0 ? (
              <span><span className="text-primary font-medium">{unread} unread</span> · {alerts.length} total</span>
            ) : (
              <span>{alerts.length} total · All caught up</span>
            )}
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={sendTest}>
          <Bell className="w-3.5 h-3.5" />
          Send Test
        </Button>
      </div>

      {isLoading ? (
        <Card>
          <CardContent className="flex items-center gap-2 text-muted-foreground text-sm py-12 justify-center">
            <Spinner className="w-4 h-4" /> Loading…
          </CardContent>
        </Card>
      ) : alerts.length === 0 ? (
        <Card>
          <CardContent className="p-0">
            <EmptyState
              icon={<Bell className="w-5 h-5" />}
              title="No alerts"
              description="System alerts will appear here."
            />
          </CardContent>
        </Card>
      ) : (
        <div className="space-y-2">
          {alerts.map(alert => (
            <Card
              key={alert.id}
              className={cn(
                'transition-colors',
                !alert.is_read && 'border-primary/30 bg-primary/[0.025]'
              )}
            >
              <CardContent className="p-4">
                <div className="flex items-start justify-between gap-3">
                  <div className="flex items-start gap-3 flex-1 min-w-0">
                    {!alert.is_read && (
                      <span className="mt-1.5 w-1.5 h-1.5 rounded-full bg-primary flex-shrink-0 animate-pulse-slow" />
                    )}
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2 flex-wrap">
                        <p className="text-sm font-semibold text-foreground">{alert.title}</p>
                        <span className={cn(
                          'text-[9px] px-1.5 py-0.5 rounded border font-bold uppercase tracking-wider',
                          SEVERITY_STYLES[alert.severity]
                        )}>
                          {alert.severity}
                        </span>
                      </div>
                      <p className="text-xs text-muted-foreground mt-1.5 leading-relaxed">{alert.message}</p>
                      <p className="text-[11px] text-muted-foreground/70 mt-2 tabular-nums">
                        {formatDate(alert.created_at)}
                      </p>
                    </div>
                  </div>
                  {!alert.is_read && (
                    <Button
                      variant="ghost"
                      size="icon-sm"
                      className="flex-shrink-0"
                      onClick={() => markRead(alert.id)}
                      title="Mark as read"
                    >
                      <CheckCheck className="w-3.5 h-3.5" />
                    </Button>
                  )}
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </div>
  )
}
