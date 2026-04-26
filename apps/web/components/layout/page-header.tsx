import { cn } from '@/lib/utils'

interface PageHeaderProps {
  icon: React.ReactNode
  label: string
  sub?: React.ReactNode
  actions?: React.ReactNode
  className?: string
}

export function PageHeader({ icon, label, sub, actions, className }: PageHeaderProps) {
  return (
    <div className={cn('flex items-start justify-between gap-4', className)}>
      <div className="flex items-center gap-3">
        <div className="flex h-10 w-10 items-center justify-center rounded-xl border border-border/60 bg-primary/10 text-primary flex-shrink-0">
          {icon}
        </div>
        <div>
          <h2 className="text-xl font-semibold tracking-tight">{label}</h2>
          {sub && <p className="mt-0.5 text-xs text-muted-foreground">{sub}</p>}
        </div>
      </div>
      {actions && <div className="flex-shrink-0">{actions}</div>}
    </div>
  )
}
