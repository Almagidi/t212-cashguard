import { describe, expect, it, jest } from '@jest/globals'
import { act, renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

jest.mock('@/services/api', () => ({ __esModule: true, default: {
  getSettings: jest.fn(), emergencyKillSwitch: jest.fn(),
} }))
jest.mock('react-hot-toast', () => ({ __esModule: true, default: { error: jest.fn(), success: jest.fn() } }))
const api = require('@/services/api').default as typeof import('@/services/api').default
const { useEmergencyKillSwitch, useSettings } = require('@/hooks/use-api') as typeof import('@/hooks/use-api')

describe('emergency settings refresh ordering', () => {
  it('discards a pre-mutation pending read and waits for a new authoritative read', async () => {
    let finishOld!: (value: unknown) => void
    const old = new Promise(resolve => { finishOld = resolve })
    const get = jest.mocked(api.getSettings)
    get.mockImplementationOnce(() => old as ReturnType<typeof api.getSettings>)
    get.mockResolvedValue({ kill_switch_active: true, auto_trading_enabled: false } as Awaited<ReturnType<typeof api.getSettings>>)
    jest.mocked(api.emergencyKillSwitch).mockResolvedValue({} as Awaited<ReturnType<typeof api.emergencyKillSwitch>>)
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const invalidated = jest.spyOn(client, 'invalidateQueries')
    const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>
    const { result, unmount } = renderHook(() => ({ settings: useSettings(), kill: useEmergencyKillSwitch() }), { wrapper })
    await waitFor(() => expect(get).toHaveBeenCalledTimes(1))
    let mutation!: Promise<unknown>
    act(() => { mutation = result.current.kill.mutateAsync() })
    await waitFor(() => expect(invalidated).toHaveBeenCalled())
    await act(async () => { finishOld({ kill_switch_active: false, auto_trading_enabled: true }); await mutation })
    await waitFor(() => expect(result.current.settings.data?.kill_switch_active).toBe(true))
    expect(get).toHaveBeenCalledTimes(2)
    unmount(); client.clear()
  })
})
