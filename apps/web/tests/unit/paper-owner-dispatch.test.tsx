import { afterEach, expect, jest, test } from '@jest/globals'
import { QueryClient, QueryClientProvider, onlineManager } from '@tanstack/react-query'
import { act, renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'

const placePaperOrder = jest.fn<(value: unknown) => Promise<unknown>>()
jest.mock('@/services/api', () => ({ __esModule: true, default: { placePaperOrder } }))
jest.mock('react-hot-toast', () => ({ __esModule: true, default: { success: jest.fn(), error: jest.fn() } }))
const { usePlacePaperOrder } = require('@/hooks/use-api')

afterEach(() => { onlineManager.setOnline(true); jest.clearAllMocks() })

test('actual TanStack offline mutation fails now instead of queuing an authenticated command for reconnect', async () => {
  const client = new QueryClient()
  let currentOwner = 'owner-A'
  const dispatchOwners: string[] = []
  placePaperOrder.mockImplementation(async () => { dispatchOwners.push(currentOwner); throw new Error('synthetic offline') })
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>
  onlineManager.setOnline(false)
  const hook = renderHook(() => usePlacePaperOrder(), { wrapper })
  let pending: Promise<unknown> | undefined
  try {
    act(() => { pending = hook.result.current.mutateAsync({ operation_id: crypto.randomUUID(), expected_owner_id: 'owner-A' }).catch(() => undefined) })
    await waitFor(() => expect(placePaperOrder).toHaveBeenCalledTimes(1), { timeout: 1000 })
    await act(async () => { await pending })
    expect(hook.result.current.isPaused).toBe(false)
    currentOwner = 'owner-B'
    await act(async () => { onlineManager.setOnline(true); await Promise.resolve() })
    expect(dispatchOwners).toEqual(['owner-A'])
    expect(placePaperOrder).toHaveBeenCalledTimes(1)
  } finally {
    currentOwner = 'owner-B'
    onlineManager.setOnline(true)
    await pending
    hook.unmount()
    client.clear()
  }
})
