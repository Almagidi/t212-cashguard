'use client'

import { useEffect, useState } from 'react'
import { usePathname, useRouter } from 'next/navigation'
import { Spinner } from '@/components/ui'
import api from '@/services/api'
import { useAuthStore } from '@/stores/auth'
import { captureOwnerScope, isCurrentOwnerScope } from '@/lib/owner-scope'

function buildLoginTarget(pathname: string): string {
  const next = pathname?.startsWith('/app') ? pathname : '/app/dashboard'
  return `/auth/login?next=${encodeURIComponent(next)}`
}

export function AuthGate({ children }: { children: React.ReactNode }) {
  const pathname = usePathname()
  const { replace } = useRouter()
  const setUser = useAuthStore((state) => state.setUser)
  const logout = useAuthStore((state) => state.logout)
  const [isChecking, setIsChecking] = useState(true)
  const [unavailable, setUnavailable] = useState(false)
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    let active = true

    const verifySession = async () => {
      const token = api.getToken()
      const scope = captureOwnerScope()

      if (!token) {
        logout()
        replace(buildLoginTarget(pathname))
        return
      }

      try {
        const me = await api.getMe()
        if (!active || api.getToken() !== token || !isCurrentOwnerScope(scope)) return
        setUser(me)
        setUnavailable(false)
        setIsChecking(false)
      } catch (err: unknown) {
        if (!active || api.getToken() !== token || !isCurrentOwnerScope(scope)) return
        const status = (err as { response?: { status?: number } })?.response?.status
        if (status === 401) {
          await api.logout().catch(() => {})
          if (!active || (!isCurrentOwnerScope(scope) && (api.getToken() !== null || useAuthStore.getState().user !== null))) return
          logout()
          replace(buildLoginTarget(pathname))
        } else {
          // Persisted identity is not authentication evidence. Keep private
          // observations and actions unavailable until this token is verified.
          setUnavailable(true)
          setIsChecking(false)
        }
      }
    }

    void verifySession()

    return () => {
      active = false
    }
  }, [attempt, logout, pathname, replace, setUser])

  if (isChecking) {
    return (
      <div className="min-h-screen bg-background flex items-center justify-center">
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Spinner className="w-4 h-4" />
          Verifying session...
        </div>
      </div>
    )
  }

  if (unavailable) {
    return (
      <div className="min-h-screen bg-background flex items-center justify-center p-4">
        <div role="alert" className="max-w-md space-y-3 text-sm text-foreground">
          <p>Session verification unavailable. Private controls remain unavailable until your session can be verified.</p>
          <button type="button" className="rounded border border-border px-3 py-2 focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary" onClick={() => { setIsChecking(true); setUnavailable(false); setAttempt((value) => value + 1) }}>Retry session verification</button>
        </div>
      </div>
    )
  }

  return <>{children}</>
}
