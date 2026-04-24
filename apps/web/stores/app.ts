import { create } from 'zustand'
import type { AppSettings } from '@/types'

interface AppState {
  settings: AppSettings | null
  setSettings: (s: AppSettings) => void
  updateSettings: (partial: Partial<AppSettings>) => void
}

export const useAppStore = create<AppState>((set) => ({
  settings: null,
  setSettings: (settings) => set({ settings }),
  updateSettings: (partial) =>
    set((state) => ({
      settings: state.settings ? { ...state.settings, ...partial } : null,
    })),
}))
