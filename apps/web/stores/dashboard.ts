/**
 * Dashboard widget order store.
 * Persists widget order to localStorage so it survives page reloads.
 */
import { create } from 'zustand'
import { persist } from 'zustand/middleware'

export type WidgetId =
  | 'stats'
  | 'presets'
  | 'equity'
  | 'positions'
  | 'signals'
  | 'orders'
  | 'portfolio'
  | 'performance'

export const DEFAULT_WIDGET_ORDER: WidgetId[] = [
  'stats',
  'presets',
  'equity',
  'positions',
  'signals',
  'orders',
  'portfolio',
  'performance',
]

export const WIDGET_LABELS: Record<WidgetId, string> = {
  stats:       'Portfolio Stats',
  presets:     'Demo Strategy Presets',
  equity:      'Equity Curve & Regime',
  positions:   'Open Positions',
  signals:     'Recent Signals',
  orders:      'Recent Orders',
  portfolio:   'Portfolio Rebalance Monitor',
  performance: 'Performance Summary',
}

export function normalizeWidgetOrder(order: WidgetId[]): WidgetId[] {
  const validIds = new Set(DEFAULT_WIDGET_ORDER)
  const seen = new Set<WidgetId>()
  const savedOrder = order.filter((id): id is WidgetId => {
    if (!validIds.has(id) || seen.has(id)) return false
    seen.add(id)
    return true
  })
  return [
    ...savedOrder,
    ...DEFAULT_WIDGET_ORDER.filter((id) => !seen.has(id)),
  ]
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
      setWidgetOrder: (order) => set({ widgetOrder: normalizeWidgetOrder(order) }),
      resetOrder: () => set({ widgetOrder: DEFAULT_WIDGET_ORDER }),
    }),
    {
      name: 'cg-dashboard-widgets',
    }
  )
)
