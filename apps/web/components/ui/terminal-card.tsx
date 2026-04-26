import { cn } from '@/lib/utils'

interface TerminalCardProps {
  label: string
  value: React.ReactNode
  sub?: string
  variant?: 'cyan' | 'teal' | 'red'
  live?: boolean
  icon?: React.ReactNode
  className?: string
}

const variantDotClass = {
  cyan: 'bg-cyan-400',
  teal: 'bg-teal-400',
  red:  'bg-red-400',
}

export function TerminalCard({
  label,
  value,
  sub,
  variant = 'cyan',
  live,
  icon,
  className,
}: TerminalCardProps) {
  return (
    <div
      className={cn(
        'terminal-card',
        variant === 'cyan' && 'terminal-card-cyan',
        variant === 'teal' && 'terminal-card-teal',
        variant === 'red'  && 'terminal-card-red',
        'relative flex flex-col gap-1.5',
        className,
      )}
    >
      {live && (
        <span
          aria-label="live"
          className="absolute top-3 right-3 flex items-center justify-center"
        >
          <span className={cn('absolute w-2.5 h-2.5 rounded-full opacity-40 animate-ping', variantDotClass[variant])} />
          <span className={cn('relative w-1.5 h-1.5 rounded-full', variantDotClass[variant])} />
        </span>
      )}
      <div className="flex items-start justify-between gap-2">
        <div className="flex-1 min-w-0 space-y-1.5">
          <p className="stat-label">{label}</p>
          <div className="mono-value truncate leading-tight">{value}</div>
          {sub && <p className="stat-sub">{sub}</p>}
        </div>
        {icon && <div className="flex-shrink-0 mt-0.5">{icon}</div>}
      </div>
    </div>
  )
}
