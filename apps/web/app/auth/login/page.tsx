'use client'
import { Suspense, useState } from 'react'
import { useRouter, useSearchParams } from 'next/navigation'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { Zap, Lock, Mail, Eye, EyeOff } from 'lucide-react'
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
    <div className="min-h-screen bg-background flex items-center justify-center p-4 relative overflow-hidden">
      {/* Ambient background effects */}
      <div className="absolute inset-0 bg-grid opacity-[0.25] pointer-events-none" />
      <div className="absolute inset-0 bg-radial-fade pointer-events-none" />
      <div className="absolute -top-1/2 left-1/2 -translate-x-1/2 w-[900px] h-[900px] rounded-full bg-primary/5 blur-3xl pointer-events-none" />

      <div className="w-full max-w-[380px] relative animate-fade-in">
        {/* Logo */}
        <div className="flex flex-col items-center mb-8">
          <div className="w-14 h-14 rounded-2xl bg-primary flex items-center justify-center mb-4 shadow-xl shadow-primary/30 ring-1 ring-primary/40 relative">
            <div className="absolute inset-0 rounded-2xl bg-gradient-to-b from-white/15 to-transparent" />
            <Zap className="w-7 h-7 text-white relative" strokeWidth={2.5} />
          </div>
          <h1 className="text-2xl font-semibold tracking-tight">CashGuard Trader</h1>
          <p className="text-[13px] text-muted-foreground mt-1.5">
            Trading 212 · Local-first · Cash-only
          </p>
        </div>

        <Card className="p-7 shadow-[var(--elev-3)] backdrop-blur-sm bg-card/95">
          <div className="mb-5">
            <h2 className="text-base font-semibold tracking-tight">Sign in</h2>
            <p className="text-xs text-muted-foreground mt-1">
              Access your trading dashboard
            </p>
          </div>

          <form onSubmit={handleSubmit(onSubmit)} className="space-y-4">
            <div className="space-y-1.5">
              <Label htmlFor="email">Email</Label>
              <div className="relative">
                <Mail className="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-muted-foreground/70 pointer-events-none" />
                <Input
                  id="email"
                  type="text"
                  autoComplete="username"
                  className="pl-9 h-10"
                  placeholder="admin@localhost"
                  {...register('email')}
                />
              </div>
              {errors.email && (
                <p className="text-xs text-red-400 mt-1">{errors.email.message}</p>
              )}
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="password">Password</Label>
              <div className="relative">
                <Lock className="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-muted-foreground/70 pointer-events-none" />
                <Input
                  id="password"
                  type={showPass ? 'text' : 'password'}
                  className="pl-9 pr-9 h-10"
                  placeholder="••••••••"
                  {...register('password')}
                />
                <button
                  type="button"
                  onClick={() => setShowPass(!showPass)}
                  className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground/70 hover:text-foreground transition-colors"
                  tabIndex={-1}
                >
                  {showPass ? <EyeOff className="w-3.5 h-3.5" /> : <Eye className="w-3.5 h-3.5" />}
                </button>
              </div>
              {errors.password && (
                <p className="text-xs text-red-400 mt-1">{errors.password.message}</p>
              )}
            </div>

            <Button type="submit" className="w-full h-10 mt-1" loading={loading}>
              Sign in
            </Button>
          </form>
        </Card>

        <div className="mt-6 flex items-center justify-center gap-2 text-[11px] text-muted-foreground/70">
          <div className="w-1 h-1 rounded-full bg-emerald-500/80" />
          <span>Default credentials: admin@localhost · change-me</span>
        </div>
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
          <p className="text-sm text-muted-foreground mt-1">Trading 212 · Local-first · Cash-only</p>
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
