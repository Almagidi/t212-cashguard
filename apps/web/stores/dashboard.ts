/**
 * Dashboard widget order store.
 * Persists widget order to localStorage so it survives page reloads.
 */
import { create } from 'zustand'
import { persist } from 'zustand/middleware'

export type WidgetId =
  | 'stats'
  | 'equity'
  | 'positions'
  | 'signals'
  | 'orders'
  | 'performance'

export const DEFAULT_WIDGET_ORDER: WidgetId[] = [
  'stats',
  'equity',
  'positions',
  'signals',
  'orders',
  'performance',
]

export const WIDGET_LABELS: Record<WidgetId, string> = {
  stats:       'Portfolio Stats',
  equity:      'Equity Curve & Regime',
  positions:   'Positions & Signals',
  signals:     'Recent Signals',
  orders:      'Recent Orders',
  performance: 'Performance Summary',
}

interface DashboardStore {
  widgetOrder: WidgetId[]
  setWidgetOrder: (order: WidgetId[]) => void
  resetOrder: () => void
}

export const useDashboardStore = create<DashboardStore>()(
  persist(
    (set) => ({
      widgetOrder: DEFAULT_WIDGET_ORDER,
      setWidgetOrder: (order) => set({ widgetOrder: order }),
      resetOrder: () => set({ widgetOrder: DEFAULT_WIDGET_ORDER }),
    }),
    {
      name: 'cg-dashboard-widgets',
    }
  )
)
