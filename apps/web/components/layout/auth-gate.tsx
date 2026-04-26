'use client'

import { useEffect, useState } from 'react'
import { usePathname, useRouter } from 'next/navigation'
import { Spinner } from '@/components/ui'
import api from '@/services/api'
import { useAuthStore } from '@/stores/auth'

function buildLoginTarget(pathname: string): string {
  const next = pathname?.startsWith('/app') ? pathname : '/app/dashboard'
  return `/auth/login?next=${encodeURIComponent(next)}`
}

export function AuthGate({ children }: { children: React.ReactNode }) {
  const pathname = usePathname()
  const router = useRouter()
  const setUser = useAuthStore((state) => state.setUser)
  const logout = useAuthStore((state) => state.logout)
  const [isChecking, setIsChecking] = useState(true)

  useEffect(() => {
    let active = true

    const verifySession = async () => {
      const token = api.getToken()

      if (!token) {
        logout()
        router.replace(buildLoginTarget(pathname))
        return
      }

      try {
        const me = await api.getMe()
        if (!active) return
        setUser(me)
        setIsChecking(false)
      } catch (err: unknown) {
        if (!active) return
        const status = (err as { response?: { status?: number } })?.response?.status
        if (status === 401) {
          await api.logout().catch(() => {})
          logout()
          router.replace(buildLoginTarget(pathname))
        } else {
          // Network or server error — token may still be valid, let the app render
          setIsChecking(false)
        }
      }
    }

    void verifySession()

    return () => {
      active = false
    }
  }, [logout, pathname, router, setUser])

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

  return <>{children}</>
}
