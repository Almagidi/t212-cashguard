import '@testing-library/jest-dom'
import { afterEach, beforeEach, describe, expect, it, jest } from '@jest/globals'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryObserver } from '@tanstack/react-query'

let settingsQuery: Record<string, unknown>
let depsQuery: Record<string, unknown>
let queryClient: QueryClient
const saved = { auto_trading_enabled: true, kill_switch_active: true }
const healthy = { database: 'ok', redis: 'ok', market_data: 'mock' }
const observed = (data: unknown, dataUpdatedAt = 1000) => ({
  data, dataUpdatedAt, isError: false, isPending: false, isFetching: false,
  fetchStatus: 'idle', failureCount: 0, failureReason: null,
})
const observeSettings = (data: unknown, at: number) => {
  settingsQuery = observed(data, at)
  queryClient.setQueryData(['settings'], data, { updatedAt: at })
}

jest.mock('@/hooks/use-api', () => ({
  QK: { settings: ['settings'] },
  useSettings: () => settingsQuery,
  useDepsHealth: () => depsQuery,
  useAccount: () => ({ data: undefined }),
}))
jest.mock('@tanstack/react-query', () => ({
  ...jest.requireActual<object>('@tanstack/react-query'),
  useQueryClient: () => queryClient,
}))
jest.mock('@/stores/ui-store', () => ({
  useUIStore: (selector: (state: { sidebarCollapsed: boolean }) => unknown) => selector({ sidebarCollapsed: false }),
}))
const { StatusBar } = require('@/components/layout/status-bar')

describe('footer authoritative control observations', () => {
  beforeEach(() => {
    queryClient = new QueryClient({ defaultOptions: { queries: { gcTime: Infinity } } })
    observeSettings(saved, 1000)
    depsQuery = observed(healthy)
  })
  afterEach(() => { queryClient.clear() })

  it('reports controls from successful current REST observations', () => {
    render(<StatusBar />)
    expect(screen.getByText('Automation enabled')).toBeVisible()
    expect(screen.getByText('KILL SWITCH ACTIVE')).toBeVisible()
    expect(screen.getByText('Connected')).toBeVisible()
  })

  it.each([true, false])('qualifies cached automation=%s while connectivity fails and keeps the halt warning', enabled => {
    settingsQuery = observed({ ...saved, auto_trading_enabled: enabled })
    depsQuery = { ...depsQuery, isError: true, failureCount: 1, failureReason: new Error('offline') }
    render(<StatusBar />)
    expect(screen.getByText('Automation unknown')).toBeVisible()
    expect(screen.queryByText(enabled ? 'Automation enabled' : 'Automation off')).not.toBeInTheDocument()
    expect(screen.getByText('Last reported: kill switch active')).toBeVisible()
    expect(screen.getByText('Connectivity unknown')).toBeVisible()
  })

  it.each([
    { isError: true, failureCount: 1 },
    { isFetching: true, failureCount: 1, fetchStatus: 'fetching' },
    { fetchStatus: 'paused' },
  ])('preserves the last halt warning while authoritative settings are unavailable %#', patch => {
    settingsQuery = { ...settingsQuery, ...patch }
    render(<StatusBar />)
    expect(screen.getByText('Automation unknown')).toBeVisible()
    expect(screen.getByText('Last reported: kill switch active')).toBeVisible()
    expect(screen.queryByText('Automation enabled')).not.toBeInTheDocument()
  })

  it('requires a new settings observation after connectivity recovers', () => {
    depsQuery = { ...depsQuery, isError: true, failureCount: 1 }
    const { rerender } = render(<StatusBar />)
    expect(screen.getByText('Automation unknown')).toBeVisible()
    depsQuery = observed(healthy, 2000)
    rerender(<StatusBar />)
    expect(screen.getByText('Automation unknown')).toBeVisible()
    expect(screen.getByText('Last reported: kill switch active')).toBeVisible()
    observeSettings({ ...saved, auto_trading_enabled: false }, 3000)
    rerender(<StatusBar />)
    expect(screen.getByText('Automation off')).toBeVisible()
    expect(screen.getByText('KILL SWITCH ACTIVE')).toBeVisible()
  })

  it('does not treat an ordinary dependency refetch as a failed connection', () => {
    depsQuery = { ...depsQuery, isFetching: true, fetchStatus: 'fetching' }
    const { rerender } = render(<StatusBar />)
    expect(screen.getByText('Automation unknown')).toBeVisible()
    depsQuery = observed(healthy, 2000)
    rerender(<StatusBar />)
    expect(screen.getByText('Automation enabled')).toBeVisible()
  })

  it('keeps cached controls qualified after remount using the retained failure timestamp', () => {
    depsQuery = { ...observed(healthy, 3000), errorUpdatedAt: 2000 }
    const { rerender } = render(<StatusBar />)
    expect(screen.getByText('Automation unknown')).toBeVisible()
    expect(screen.getByText('Last reported: kill switch active')).toBeVisible()
    observeSettings(saved, 4000)
    rerender(<StatusBar />)
    expect(screen.getByText('Automation enabled')).toBeVisible()
  })

  it.each([
    { isFetching: true, failureCount: 1, failureReason: new Error('retryable disconnect') },
    { data: { ...healthy, database: 'error' }, dataUpdatedAt: 2000 },
  ])('retains failed authority across a recovered transient/health-error remount %#', failure => {
    const view = render(<StatusBar />)
    depsQuery = { ...depsQuery, ...failure }
    view.rerender(<StatusBar />)
    expect(screen.getByText('Automation unknown')).toBeVisible()
    view.unmount()
    depsQuery = { ...observed(healthy, 3000), errorUpdatedAt: 0 }
    const recovered = render(<StatusBar />)
    expect(screen.getByText('Automation unknown')).toBeVisible()
    expect(screen.getByText('Last reported: kill switch active')).toBeVisible()
    observeSettings({ ...saved, auto_trading_enabled: false }, 4000)
    recovered.rerender(<StatusBar />)
    expect(screen.getByText('Automation off')).toBeVisible()
  })

  it('requires a settings read after a later retry failure, even if an earlier read succeeded', () => {
    const view = render(<StatusBar />)
    depsQuery = { ...depsQuery, isFetching: true, failureCount: 1 }
    view.rerender(<StatusBar />)
    observeSettings(saved, 2000)
    view.rerender(<StatusBar />)
    depsQuery = { ...depsQuery, failureCount: 2 }
    view.rerender(<StatusBar />)
    depsQuery = observed(healthy, 3000)
    view.rerender(<StatusBar />)
    expect(screen.getByText('Automation unknown')).toBeVisible()
    observeSettings({ ...saved, auto_trading_enabled: false }, 4000)
    view.rerender(<StatusBar />)
    expect(screen.getByText('Automation off')).toBeVisible()
  })

  it('does not start an active settings request or invalidate unrelated readiness on a connection failure', () => {
    const request = jest.fn(async () => saved)
    queryClient.setQueryData(['settings', 'live-readiness'], { ready: false })
    const observer = new QueryObserver(queryClient, { queryKey: ['settings'], queryFn: request, staleTime: Infinity })
    const unsubscribe = observer.subscribe(() => {})
    try {
      depsQuery = { ...depsQuery, failureCount: 1, isFetching: true }
      render(<StatusBar />)
      expect(request).not.toHaveBeenCalled()
      expect(queryClient.getQueryState(['settings', 'live-readiness'])?.isInvalidated).toBe(false)
    } finally { unsubscribe() }
  })

  it('keeps a later paused retry qualified after an intermediate settings success', () => {
    const view = render(<StatusBar />)
    depsQuery = { ...depsQuery, failureCount: 1, isFetching: true, fetchStatus: 'fetching' }
    view.rerender(<StatusBar />)
    observeSettings(saved, 2000)
    view.rerender(<StatusBar />)
    depsQuery = { ...depsQuery, fetchStatus: 'paused' }
    view.rerender(<StatusBar />)
    depsQuery = observed(healthy, 3000)
    view.rerender(<StatusBar />)
    expect(screen.getByText('Automation unknown')).toBeVisible()
    observeSettings(saved, 4000)
    view.rerender(<StatusBar />)
    expect(screen.getByText('Automation enabled')).toBeVisible()
  })

  it('does not invent an inactive halt from missing control data', () => {
    settingsQuery = { ...settingsQuery, data: undefined, isPending: true }
    render(<StatusBar />)
    expect(screen.getByText('Automation unknown')).toBeVisible()
    expect(screen.queryByText('KILL SWITCH ACTIVE')).not.toBeInTheDocument()
    expect(screen.queryByText('Last reported: kill switch active')).not.toBeInTheDocument()
  })
})
