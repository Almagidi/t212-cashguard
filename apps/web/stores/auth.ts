import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import type { User } from '@/types'
import { scrubPaperIntentPayloads } from '@/lib/paper-operation-intent'

interface AuthState {
  user: User | null
  isAuthenticated: boolean
  setUser: (user: User | null) => void
  logout: () => void
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set) => ({
      user: null,
      isAuthenticated: false,
      setUser: (user) => set({ user, isAuthenticated: !!user }),
      logout: () => {
        if (typeof window !== 'undefined') {
          try { scrubPaperIntentPayloads(sessionStorage) } catch { /* storage can be unavailable */ }
        }
        set({ user: null, isAuthenticated: false })
        if (typeof window !== 'undefined') localStorage.removeItem('cg_token')
      },
    }),
    { name: 'cg_auth', partialize: (s) => ({ user: s.user, isAuthenticated: s.isAuthenticated }) }
  )
)
