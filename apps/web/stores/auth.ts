import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import type { User } from '@/types'
import { scrubPaperIntentPayloads } from '@/lib/paper-operation-intent'

interface AuthState {
  user: User | null
  isAuthenticated: boolean
  verifiedToken: string | null
  sessionRevision: number
  advanceSession: () => void
  setUser: (user: User | null) => void
  logout: () => void
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set) => ({
      user: null,
      isAuthenticated: false,
      verifiedToken: null,
      sessionRevision: 0,
      advanceSession: () => set((state) => ({ sessionRevision: state.sessionRevision + 1, verifiedToken: null })),
      setUser: (user) => set((state) => {
        const verifiedToken = user && typeof window !== 'undefined' ? localStorage.getItem('cg_token') : null
        return {
          user,
          isAuthenticated: !!user,
          verifiedToken,
          sessionRevision: state.sessionRevision + (state.user?.id !== user?.id || state.user?.is_admin !== user?.is_admin || state.user?.is_active !== user?.is_active || state.verifiedToken !== verifiedToken ? 1 : 0),
        }
      }),
      logout: () => {
        if (typeof window !== 'undefined') {
          try { scrubPaperIntentPayloads(sessionStorage) } catch { /* storage can be unavailable */ }
        }
        set((state) => ({
          user: null,
          isAuthenticated: false,
          verifiedToken: null,
          sessionRevision: state.sessionRevision + (state.user || state.isAuthenticated ? 1 : 0),
        }))
        if (typeof window !== 'undefined') localStorage.removeItem('cg_token')
      },
    }),
    { name: 'cg_auth', partialize: (s) => ({ user: s.user, isAuthenticated: s.isAuthenticated }) }
  )
)
