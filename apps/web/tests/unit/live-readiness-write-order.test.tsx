import { afterEach, beforeEach, expect, jest, test } from '@jest/globals'
import { act, renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useAuthStore } from '@/stores/auth'
import type { LiveReadinessStatus } from '@/types'

jest.mock('@/services/api', () => ({ __esModule: true, default: { getLiveReadiness: jest.fn(), updateLiveReadiness: jest.fn() }, getDcaActivity: jest.fn() }))
jest.mock('react-hot-toast', () => ({ __esModule: true, default: { success: jest.fn(), error: jest.fn() } }))
const api = require('@/services/api').default
const { useLiveReadiness, useUpdateLiveReadiness } = require('@/hooks/use-api') as typeof import('@/hooks/use-api')
const ready: LiveReadinessStatus = { mode: 'live', live_execution_enabled: true, live_trading_unlocked: true, eligible_for_unlock: true, ready_for_live: true, blockers: [], checks: [] }
const blocked = { ...ready, ready_for_live: false, eligible_for_unlock: false, blockers: ['Review changed'] }
function deferred<T>() { let resolve!: (value: T) => void; return { promise: new Promise<T>(r => { resolve = r }), resolve } }

beforeEach(() => {
  useAuthStore.getState().setUser({ id: '11111111-1111-4111-8111-111111111111', email: 'synthetic@example.invalid', is_active: true, is_admin: true, created_at: '2026-10-28T00:00:00Z' })
})
afterEach(() => { useAuthStore.getState().logout(); jest.clearAllMocks() })
test('write response and reads started during a write cannot revive older readiness', async () => {
  const write = deferred<LiveReadinessStatus>(), oldRead = deferred<LiveReadinessStatus>(), finalRead = deferred<LiveReadinessStatus>()
  api.getLiveReadiness.mockResolvedValueOnce(ready).mockImplementationOnce(() => oldRead.promise).mockImplementationOnce(() => finalRead.promise)
  api.updateLiveReadiness.mockImplementation(() => write.promise)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: { children: React.ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>
  const { result, unmount } = renderHook(() => ({ query: useLiveReadiness(), mutation: useUpdateLiveReadiness() }), { wrapper })
  await waitFor(() => expect(result.current.query.data).toEqual(ready))
  act(() => result.current.mutation.mutate({ action: 'record_demo_validation' }))
  await waitFor(() => expect(api.updateLiveReadiness).toHaveBeenCalledTimes(1))
  act(() => { void result.current.query.refetch() })
  await waitFor(() => expect(api.getLiveReadiness).toHaveBeenCalledTimes(2))
  await act(async () => { write.resolve(ready); await Promise.resolve() })
  await waitFor(() => expect(api.getLiveReadiness).toHaveBeenCalledTimes(3))
  expect(result.current.mutation.isPending).toBe(true)
  await act(async () => { finalRead.resolve(blocked); await Promise.resolve() })
  await waitFor(() => expect(result.current.mutation.isPending).toBe(false))
  expect(result.current.query.data).toEqual(blocked)
  await act(async () => { oldRead.resolve(ready); await Promise.resolve() })
  expect(result.current.query.data).toEqual(blocked)
  unmount(); client.clear()
})

test('failed post-write observation never acquires authority from the successful mutation payload', async () => {
  api.getLiveReadiness.mockResolvedValueOnce(ready).mockRejectedValue(new Error('Synthetic offline observation'))
  api.updateLiveReadiness.mockResolvedValue(ready)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: { children: React.ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>
  const { result, unmount } = renderHook(() => ({ query: useLiveReadiness(), mutation: useUpdateLiveReadiness() }), { wrapper })
  await waitFor(() => expect(result.current.query.data).toEqual(ready))
  act(() => result.current.mutation.mutate({ action: 'unlock_live' }))
  await waitFor(() => expect(result.current.query.isError).toBe(true))
  await waitFor(() => expect(result.current.mutation.isPending).toBe(false))
  expect(result.current.query.isInvalidated).toBe(true)
  expect(result.current.query.data).toEqual(ready) // retained for diagnosis, never current authority
  unmount(); client.clear()
})

test('explicit invalidation is observable even when refetch has not started', async () => {
  api.getLiveReadiness.mockResolvedValue(ready)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  const wrapper = ({ children }: { children: React.ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>
  const { result, unmount } = renderHook(useLiveReadiness, { wrapper })
  await waitFor(() => expect(result.current.data).toEqual(ready))
  expect(result.current.isInvalidated).toBe(false)
  await act(async () => { await client.invalidateQueries({ queryKey: ['settings', 'live-readiness'], refetchType: 'none' }) })
  await waitFor(() => expect(result.current.isInvalidated).toBe(true))
  expect(result.current.isFetching).toBe(false)
  unmount(); client.clear()
})
