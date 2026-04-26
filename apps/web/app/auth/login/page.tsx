'use client'
import { Suspense, useState } from 'react'
import { useRouter, useSearchParams } from 'next/navigation'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { Zap, Lock, Mail, Eye, EyeOff, ShieldCheck, Server, Activity } from 'lucide-react'
import toast from 'react-hot-toast'
import { Button, Card, Input, Label } from '@/components/ui'
import { useAuthStore } from '@/stores/auth'
import api from '@/services/api'

const schema = z.object({
  email: z.string().min(1, 'Email required'),
  password: z.string().min(1, 'Password required'),
})
type FormData = z.infer<typeof schema>

function LoginPageContent() {
  const router = useRouter()
  const searchParams = useSearchParams()
  const { setUser } = useAuthStore()
  const [showPass, setShowPass] = useState(false)
  const [loading, setLoading] = useState(false)

  const { register, handleSubmit, formState: { errors } } = useForm<FormData>({
    resolver: zodResolver(schema),
    defaultValues: { email: 'admin@localhost', password: '' },
  })

  const onSubmit = async (data: FormData) => {
    setLoading(true)
    try {
      await api.login(data)
      const me = await api.getMe()
      setUser(me)
      const nextPath = searchParams.get('next')
      const safePath = nextPath?.startsWith('/app/') ? nextPath : '/app/dashboard'
      router.push(safePath)
    } catch (e: any) {
      const detail = e?.response?.data?.detail
      let msg = 'Login failed'
      if (typeof detail === 'string') msg = detail
      else if (Array.isArray(detail)) msg = detail[0]?.msg || 'Login failed'
      else if (detail?.msg) msg = detail.msg
      toast.error(msg)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="relative grid min-h-screen overflow-hidden bg-background lg:grid-cols-[minmax(0,0.92fr)_minmax(420px,1fr)]">
      <section className="relative hidden min-h-screen border-r border-border bg-card lg:flex">
        <div className="absolute inset-0 bg-grid opacity-[0.18] pointer-events-none" />
        <div className="relative z-10 flex w-full flex-col p-10">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-lg border border-primary/25 bg-primary/10 text-primary">
              <Zap className="h-5 w-5" strokeWidth={2.4} />
            </div>
            <div>
              <p className="text-sm font-semibold leading-tight">CashGuard</p>
              <p className="mt-0.5 text-[11px] text-muted-foreground">Trading 212 control room</p>
            </div>
          </div>

          <div className="mt-auto max-w-md space-y-7">
            <div>
              <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-primary">Local stack</p>
              <h2 className="mt-3 text-3xl font-semibold tracking-tight">Cash-only automation with every guard visible.</h2>
              <p className="mt-4 text-sm leading-6 text-muted-foreground">
                Broker state, risk limits, emergency controls, and seeded verification data stay close to the dashboard.
              </p>
            </div>

            <div className="grid gap-3">
              <AuthSignal icon={ShieldCheck} label="Cash-only invariant" value="No leverage path" />
              <AuthSignal icon={Server} label="Seeded stack" value="Postgres, Redis, worker, API" />
              <AuthSignal icon={Activity} label="Runtime checks" value="Dashboard, broker, risk, reports" />
            </div>
          </div>
        </div>
      </section>

      <main className="flex min-h-screen items-center justify-center px-4 py-10 sm:px-6 lg:px-10">
        <div className="w-full max-w-[400px] animate-fade-in">
          <div className="mb-8 flex flex-col items-center text-center lg:items-start lg:text-left">
            <div className="mb-4 flex h-12 w-12 items-center justify-center rounded-lg border border-primary/25 bg-primary text-primary-foreground shadow-lg shadow-primary/20">
              <Zap className="h-6 w-6" strokeWidth={2.4} />
            </div>
            <h1 className="text-2xl font-semibold tracking-tight">CashGuard Trader</h1>
            <p className="mt-1.5 text-[13px] text-muted-foreground">
              Trading 212 - local-first - cash-only
            </p>
          </div>

          <Card className="rounded-lg p-6 shadow-[var(--elev-2)] sm:p-7">
            <div className="mb-6">
              <h2 className="text-base font-semibold tracking-tight">Sign in</h2>
              <p className="mt-1 text-xs text-muted-foreground">
                Access your trading dashboard
              </p>
            </div>

            <form onSubmit={handleSubmit(onSubmit)} className="space-y-4">
              <div className="space-y-1.5">
                <Label htmlFor="email">Email</Label>
                <div className="relative">
                  <Mail className="absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground/70 pointer-events-none" />
                  <Input
                    id="email"
                    type="text"
                    autoComplete="username"
                    className="h-10 pl-9"
                    placeholder="admin@localhost"
                    {...register('email')}
                  />
                </div>
                {errors.email && (
                  <p className="mt-1 text-xs text-red-400">{errors.email.message}</p>
                )}
              </div>

              <div className="space-y-1.5">
                <Label htmlFor="password">Password</Label>
                <div className="relative">
                  <Lock className="absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground/70 pointer-events-none" />
                  <Input
                    id="password"
                    type={showPass ? 'text' : 'password'}
                    className="h-10 pl-9 pr-9"
                    placeholder="Password"
                    {...register('password')}
                  />
                  <button
                    type="button"
                    onClick={() => setShowPass(!showPass)}
                    className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground/70 hover:text-foreground transition-colors"
                    tabIndex={-1}
                    aria-label={showPass ? 'Hide password' : 'Show password'}
                  >
                    {showPass ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
                  </button>
                </div>
                {errors.password && (
                  <p className="mt-1 text-xs text-red-400">{errors.password.message}</p>
                )}
              </div>

              <Button type="submit" className="mt-1 h-10 w-full" loading={loading}>
                Sign in
              </Button>
            </form>
          </Card>

          <p className="mt-5 text-center text-[11px] text-muted-foreground/75 lg:text-left">
            Default credentials: admin@localhost - change-me
          </p>
        </div>
      </main>
    </div>
  )
}

function AuthSignal({
  icon: Icon,
  label,
  value,
}: {
  icon: React.ElementType
  label: string
  value: string
}) {
  return (
    <div className="flex items-center gap-3 rounded-lg border border-border/70 bg-background/55 px-3 py-3">
      <div className="flex h-8 w-8 items-center justify-center rounded-md border border-border/70 bg-card text-primary">
        <Icon className="h-4 w-4" />
      </div>
      <div>
        <p className="text-xs font-medium text-foreground">{label}</p>
        <p className="mt-0.5 text-[11px] text-muted-foreground">{value}</p>
      </div>
    </div>
  )
}

function LoginFallback() {
  return (
    <div className="min-h-screen bg-background flex items-center justify-center p-4">
      <div className="w-full max-w-sm">
        <div className="flex flex-col items-center mb-8">
          <div className="w-12 h-12 rounded-xl bg-primary flex items-center justify-center mb-3 shadow-lg shadow-primary/20">
            <Zap className="w-6 h-6 text-white" />
          </div>
          <h1 className="text-xl font-semibold">CashGuard Trader</h1>
          <p className="text-sm text-muted-foreground mt-1">Trading 212 - local-first - cash-only</p>
        </div>
      </div>
    </div>
  )
}

export default function LoginPage() {
  return (
    <Suspense fallback={<LoginFallback />}>
      <LoginPageContent />
    </Suspense>
  )
}
