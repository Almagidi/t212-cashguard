'use client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from 'next-themes'
import toast, { Toaster } from 'react-hot-toast'
import { useState, useEffect, useRef, useLayoutEffect } from 'react'
import { useAuthStore } from '@/stores/auth'

const PWA_CACHE_PREFIX = 'cashguard-'

async function clearCashGuardCaches() {
  if (typeof window === 'undefined' || !('caches' in window)) return

  const cacheNames = await window.caches.keys()
  await Promise.all(
    cacheNames
      .filter((cacheName) => cacheName.startsWith(PWA_CACHE_PREFIX))
      .map((cacheName) => window.caches.delete(cacheName)),
  )
}

async function unregisterCashGuardServiceWorkers() {
  if (typeof window === 'undefined' || !('serviceWorker' in navigator)) return

  const registrations = await navigator.serviceWorker.getRegistrations()
  await Promise.all(
    registrations
      .filter((registration) => {
        const workerUrls = [
          registration.active?.scriptURL,
          registration.waiting?.scriptURL,
          registration.installing?.scriptURL,
        ].filter(Boolean)

        return workerUrls.some((url) => url?.includes('/sw.js'))
      })
      .map((registration) => registration.unregister()),
  )
}

export function Providers({ children }: { children: React.ReactNode }) {
  useEffect(() => {
    const synchronizeToken = (event: StorageEvent) => {
      if (event.storageArea === localStorage && (event.key === null || (event.key === 'cg_token' && event.oldValue !== event.newValue))) {
        useAuthStore.getState().advanceSession()
      }
    }
    window.addEventListener('storage', synchronizeToken)
    return () => window.removeEventListener('storage', synchronizeToken)
  }, [])

  // Register service worker for PWA support
  useEffect(() => {
    if (!('serviceWorker' in navigator)) return

    const enablePwa = process.env.NEXT_PUBLIC_ENABLE_PWA === 'true'

    const configureServiceWorker = async () => {
      if (!enablePwa || process.env.NODE_ENV !== 'production') {
        await unregisterCashGuardServiceWorkers()
        await clearCashGuardCaches()
        return
      }

      await navigator.serviceWorker.register('/sw.js').catch(() => {
        // SW registration failure is non-fatal
      })
    }

    void configureServiceWorker()
  }, [])

  const owner = useAuthStore((state) => state.user?.id ?? 'anonymous')
  const revision = useAuthStore((state) => state.sessionRevision)
  useLayoutEffect(() => () => {
    // Remove the old session's global notices during cleanup, before paint and
    // before the new session's layout effects can emit its own notices.
    toast.remove()
  }, [owner, revision])

  return (
    <ThemeProvider attribute="class" defaultTheme="dark" enableSystem disableTransitionOnChange>
        <OwnerQueries key={`${owner}:${revision}`}>{children}</OwnerQueries>
        <Toaster
          position="top-right"
          toastOptions={{
            style: {
              background: 'hsl(var(--card))',
              color: 'hsl(var(--foreground))',
              border: '1px solid hsl(var(--border))',
              borderRadius: '10px',
              fontSize: '13px',
              boxShadow: 'var(--elev-2)',
            },
          }}
        />
    </ThemeProvider>
  )
}

function OwnerQueries({ children }: { children: React.ReactNode }) {
  const [queryClient] = useState(() => new QueryClient({
    defaultOptions: {
      queries: {
        // 15 s default: long enough to avoid redundant refetches on fast
        // navigation, short enough that live data still feels fresh.
        staleTime: 15_000,
        // Keep unused query data for 5 minutes before garbage-collecting it,
        // so navigating back to a page feels instant.
        gcTime: 5 * 60_000,
        // Do not retry on 4xx errors — retrying 401s repeatedly fires the
        // interceptor multiple times before the async verify logic finishes.
        retry: (failCount, error: unknown) => {
          const status = (error as { response?: { status?: number } })?.response?.status
          if (status && status >= 400 && status < 500) return false   // no retry on client errors
          return failCount < 2                                         // up to 2 retries for network/5xx
        },
        // Prevent component error boundaries from swallowing the whole page on
        // a single failing background query (e.g. an optional dashboard widget).
        throwOnError: false,
        refetchOnWindowFocus: false,
        refetchOnReconnect: true,
      },
      mutations: {
        // Mutations that fail with client errors should surface via onError handlers
        retry: false,
      },
    },
  }))

  const lease = useRef({ generation: 0 })
  useEffect(() => {
    const lifecycle = lease.current
    const currentLease = ++lifecycle.generation
    return () => {
      // StrictMode immediately replays this effect; do not clear a remounted
      // client's active queries during that development-only lifecycle probe.
      queueMicrotask(() => {
        if (lifecycle.generation !== currentLease) return
        void queryClient.cancelQueries()
        queryClient.clear()
      })
    }
  }, [queryClient])
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
}
