'use client'
import * as React from 'react'
import { cva, type VariantProps } from 'class-variance-authority'
import { cn } from '@/lib/utils'

/* ─────────────────────────────────────────────────────────────────────────────
   BUTTON
   ───────────────────────────────────────────────────────────────────────── */
const buttonVariants = cva(
  [
    'inline-flex items-center justify-center gap-2 whitespace-nowrap select-none',
    'rounded-lg text-sm font-medium tracking-tight',
    'ring-offset-background transition-[background-color,border-color,color,box-shadow,transform] duration-150',
    'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2',
    'disabled:pointer-events-none disabled:opacity-50',
    'active:scale-[0.98]',
  ].join(' '),
  {
    variants: {
      variant: {
        default:
          'bg-primary text-primary-foreground shadow-sm hover:bg-primary/90 hover:shadow-md',
        destructive:
          'bg-destructive text-destructive-foreground shadow-sm hover:bg-destructive/90',
        outline:
          'border border-border bg-transparent text-foreground hover:bg-accent hover:text-accent-foreground',
        secondary:
          'bg-secondary text-secondary-foreground border border-border/60 hover:bg-secondary/80',
        ghost:
          'text-muted-foreground hover:bg-accent hover:text-foreground',
        link:
          'text-primary underline-offset-4 hover:underline',
        danger:
          'bg-red-600 text-white shadow-sm hover:bg-red-500 focus-visible:ring-red-500',
        success:
          'bg-emerald-600 text-white shadow-sm hover:bg-emerald-500 focus-visible:ring-emerald-500',
      },
      size: {
        default: 'h-9 px-4 text-sm',
        sm: 'h-8 px-3 text-xs gap-1.5',
        xs: 'h-7 px-2.5 text-[11px] gap-1.5 rounded-md',
        lg: 'h-10 px-6 text-sm',
        icon: 'h-9 w-9',
        'icon-sm': 'h-8 w-8 rounded-md',
      },
    },
    defaultVariants: { variant: 'default', size: 'default' },
  }
)

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  loading?: boolean
}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, loading, children, disabled, ...props }, ref) => (
    <button
      ref={ref}
      className={cn(buttonVariants({ variant, size }), className)}
      disabled={disabled || loading}
      {...props}
    >
      {loading && <Spinner className="w-3.5 h-3.5" />}
      {children}
    </button>
  )
)
Button.displayName = 'Button'

/* ─────────────────────────────────────────────────────────────────────────────
   CARD
   ───────────────────────────────────────────────────────────────────────── */
export const Card = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    <div
      ref={ref}
      className={cn(
        'bg-card text-card-foreground rounded-xl border border-border',
        'shadow-[var(--elev-1)]',
        className
      )}
      {...props}
    />
  )
)
Card.displayName = 'Card'

export const CardHeader = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    <div
      ref={ref}
      className={cn('flex flex-col gap-1 px-5 pt-5 pb-3', className)}
      {...props}
    />
  )
)
CardHeader.displayName = 'CardHeader'

export const CardTitle = React.forwardRef<HTMLHeadingElement, React.HTMLAttributes<HTMLHeadingElement>>(
  ({ className, ...props }, ref) => (
    <h3
      ref={ref}
      className={cn(
        'text-sm font-semibold leading-none tracking-tight text-foreground',
        className
      )}
      {...props}
    />
  )
)
CardTitle.displayName = 'CardTitle'

export const CardDescription = React.forwardRef<HTMLParagraphElement, React.HTMLAttributes<HTMLParagraphElement>>(
  ({ className, ...props }, ref) => (
    <p
      ref={ref}
      className={cn('text-xs text-muted-foreground/90 leading-relaxed', className)}
      {...props}
    />
  )
)
CardDescription.displayName = 'CardDescription'

export const CardContent = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    <div ref={ref} className={cn('px-5 pb-5 pt-0', className)} {...props} />
  )
)
CardContent.displayName = 'CardContent'

export const CardFooter = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    <div
      ref={ref}
      className={cn('flex items-center gap-2 px-5 py-3 border-t border-border/60', className)}
      {...props}
    />
  )
)
CardFooter.displayName = 'CardFooter'

/* ─────────────────────────────────────────────────────────────────────────────
   INPUT
   ───────────────────────────────────────────────────────────────────────── */
export interface InputProps extends React.InputHTMLAttributes<HTMLInputElement> {}
export const Input = React.forwardRef<HTMLInputElement, InputProps>(
  ({ className, ...props }, ref) => (
    <input
      ref={ref}
      className={cn(
        'flex h-9 w-full rounded-lg border border-input bg-background/60 px-3 py-1 text-sm',
        'shadow-[inset_0_1px_0_rgba(255,255,255,0.02)]',
        'file:border-0 file:bg-transparent file:text-sm file:font-medium',
        'placeholder:text-muted-foreground/70',
        'transition-[border-color,box-shadow] duration-150',
        'focus-visible:outline-none focus-visible:border-primary/70 focus-visible:ring-2 focus-visible:ring-primary/20',
        'disabled:cursor-not-allowed disabled:opacity-50',
        className
      )}
      {...props}
    />
  )
)
Input.displayName = 'Input'

/* Textarea */
export interface TextareaProps extends React.TextareaHTMLAttributes<HTMLTextAreaElement> {}
export const Textarea = React.forwardRef<HTMLTextAreaElement, TextareaProps>(
  ({ className, ...props }, ref) => (
    <textarea
      ref={ref}
      className={cn(
        'flex min-h-[80px] w-full rounded-lg border border-input bg-background/60 px-3 py-2 text-sm',
        'placeholder:text-muted-foreground/70',
        'transition-[border-color,box-shadow] duration-150',
        'focus-visible:outline-none focus-visible:border-primary/70 focus-visible:ring-2 focus-visible:ring-primary/20',
        'disabled:cursor-not-allowed disabled:opacity-50',
        className
      )}
      {...props}
    />
  )
)
Textarea.displayName = 'Textarea'

/* ─────────────────────────────────────────────────────────────────────────────
   LABEL
   ───────────────────────────────────────────────────────────────────────── */
export const Label = React.forwardRef<HTMLLabelElement, React.LabelHTMLAttributes<HTMLLabelElement>>(
  ({ className, ...props }, ref) => (
    <label
      ref={ref}
      className={cn(
        'text-xs font-medium leading-none text-foreground/90',
        'peer-disabled:cursor-not-allowed peer-disabled:opacity-70',
        className
      )}
      {...props}
    />
  )
)
Label.displayName = 'Label'

/* ─────────────────────────────────────────────────────────────────────────────
   BADGE
   ───────────────────────────────────────────────────────────────────────── */
const badgeVariants = cva(
  'inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.08em] transition-colors',
  {
    variants: {
      variant: {
        default: 'border-primary/25 bg-primary/10 text-primary',
        secondary: 'border-border bg-secondary text-secondary-foreground',
        destructive: 'border-red-500/25 bg-red-500/10 text-red-400',
        success: 'border-emerald-500/25 bg-emerald-500/10 text-emerald-400',
        warning: 'border-amber-500/25 bg-amber-500/10 text-amber-400',
        info: 'border-blue-500/25 bg-blue-500/10 text-blue-400',
        outline: 'border-border bg-transparent text-muted-foreground',
      },
    },
    defaultVariants: { variant: 'default' },
  }
)
export interface BadgeProps
  extends React.HTMLAttributes<HTMLDivElement>,
    VariantProps<typeof badgeVariants> {}
export const Badge = ({ className, variant, ...props }: BadgeProps) => (
  <div className={cn(badgeVariants({ variant }), className)} {...props} />
)

/* ─────────────────────────────────────────────────────────────────────────────
   SPINNER
   ───────────────────────────────────────────────────────────────────────── */
export const Spinner = ({ className }: { className?: string }) => (
  <svg
    className={cn('animate-spin text-current', className)}
    xmlns="http://www.w3.org/2000/svg"
    fill="none"
    viewBox="0 0 24 24"
    aria-hidden="true"
  >
    <circle className="opacity-20" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" />
    <path
      className="opacity-90"
      fill="currentColor"
      d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"
    />
  </svg>
)

/* ─────────────────────────────────────────────────────────────────────────────
   SEPARATOR
   ───────────────────────────────────────────────────────────────────────── */
export const Separator = ({
  className,
  orientation = 'horizontal',
  ...props
}: React.HTMLAttributes<HTMLDivElement> & { orientation?: 'horizontal' | 'vertical' }) => (
  <div
    className={cn(
      'shrink-0 bg-border',
      orientation === 'horizontal' ? 'h-px w-full' : 'h-full w-px',
      className
    )}
    {...props}
  />
)

/* ─────────────────────────────────────────────────────────────────────────────
   EMPTY STATE
   ───────────────────────────────────────────────────────────────────────── */
export const EmptyState = ({
  icon,
  title,
  description,
  action,
  className,
}: {
  icon?: React.ReactNode
  title: string
  description?: string
  action?: React.ReactNode
  className?: string
}) => (
  <div
    className={cn(
      'flex flex-col items-center justify-center gap-3 py-16 text-center px-6',
      className
    )}
  >
    {icon && (
      <div className="w-11 h-11 rounded-xl bg-muted/40 border border-border/60 flex items-center justify-center text-muted-foreground/60">
        {icon}
      </div>
    )}
    <div className="space-y-1 max-w-sm">
      <p className="text-sm font-semibold text-foreground">{title}</p>
      {description && (
        <p className="text-xs text-muted-foreground leading-relaxed">{description}</p>
      )}
    </div>
    {action && <div className="pt-1">{action}</div>}
  </div>
)

export { TerminalCard } from './terminal-card'

/* ─────────────────────────────────────────────────────────────────────────────
   STAT CARD
   ───────────────────────────────────────────────────────────────────────── */
export const StatCard = ({
  label,
  value,
  sub,
  trend,
  icon,
  className,
}: {
  label: string
  value: React.ReactNode
  sub?: React.ReactNode
  trend?: 'up' | 'down' | 'neutral'
  icon?: React.ReactNode
  className?: string
}) => (
  <div className={cn('stat-card group', className)}>
    <div className="flex items-start justify-between gap-2">
      <p className="stat-label">{label}</p>
      {icon && (
        <div className="text-muted-foreground/50 group-hover:text-muted-foreground/80 transition-colors">
          {icon}
        </div>
      )}
    </div>
    <p
      className={cn(
        'stat-value',
        trend === 'up' && 'text-emerald-400',
        trend === 'down' && 'text-red-400'
      )}
    >
      {value}
    </p>
    {sub && <p className="stat-sub">{sub}</p>}
  </div>
)

/* ─────────────────────────────────────────────────────────────────────────────
   SKELETON
   ───────────────────────────────────────────────────────────────────────── */
export const Skeleton = ({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) => (
  <div className={cn('skeleton', className)} {...props} />
)

/* ─────────────────────────────────────────────────────────────────────────────
   KBD
   ───────────────────────────────────────────────────────────────────────── */
export const Kbd = ({ className, children, ...props }: React.HTMLAttributes<HTMLElement>) => (
  <kbd
    className={cn(
      'inline-flex items-center justify-center px-1.5 py-0.5 min-w-[20px] h-5',
      'text-[10px] font-mono font-medium text-muted-foreground',
      'rounded border border-border bg-muted/40',
      className
    )}
    {...props}
  >
    {children}
  </kbd>
)
