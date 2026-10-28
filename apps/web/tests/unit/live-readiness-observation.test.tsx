import { beforeEach, expect, jest, test } from '@jest/globals'
import { act, fireEvent, render, screen } from '@testing-library/react'
import type { LiveReadinessStatus } from '@/types'

const ready: LiveReadinessStatus = { mode: 'live', live_execution_enabled: true, live_trading_unlocked: true, eligible_for_unlock: true, ready_for_live: true, blockers: [], checks: [] }
let settingsLoading = false
let observation: Record<string, unknown>
const mutate = jest.fn()
const mockUpdateReadiness = jest.fn(() => ({ mutate, isPending: false }))
jest.mock('next-themes', () => ({ useTheme: () => ({ theme: 'dark', setTheme: jest.fn() }) }))
jest.mock('@/hooks/use-api', () => ({
  useSettings: () => ({ data: { theme: 'dark', timezone: 'UTC', daily_stats_reset_time: '00:00' }, isLoading: settingsLoading }),
  useLiveReadiness: () => observation,
  useTelegramStatus: () => ({ data: undefined, isLoading: false }),
  useTelegramTestAlert: () => ({ mutate: jest.fn(), isPending: false }),
  useUpdateSettings: () => ({ mutate: jest.fn(), isPending: false }),
  useUpdateLiveReadiness: () => mockUpdateReadiness(),
}))
const { default: SettingsPage } = require('@/app/app/settings/page')
beforeEach(() => { mutate.mockReset(); mockUpdateReadiness.mockReset(); mockUpdateReadiness.mockImplementation(() => ({ mutate, isPending: false })); settingsLoading = false; observation = { data: ready, isLoading: false, isFetching: false, isError: false } })
test.each([{ isError: true }, { isFetching: true }, { fetchStatus: 'paused' }, { failureCount: 1 }, { isInvalidated: true }, { data: undefined }])('cached readiness is unknown for failed or unsettled observation %j while protective lock remains available', flags => {
  observation = { ...observation, ...flags }
  render(<SettingsPage />)
  expect(screen.queryByText('Ready')).not.toBeInTheDocument()
  expect(screen.getByRole('status')).toHaveTextContent(/unknown|unavailable|checking/i)
  expect(screen.queryByRole('button', { name: /record demo review/i })).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: /lock live trading/i }))
  expect(mutate).toHaveBeenCalledWith({ action: 'lock_live' })
})
test('a current eligible snapshot can be explicitly acknowledged again while the global unlocked flag remains true', () => {
  observation = { ...observation, data: { ...ready, ready_for_live: false } }
  render(<SettingsPage />)
  const acknowledge = screen.getByRole('button', { name: /acknowledge updated live review/i })
  expect(acknowledge).toBeEnabled()
  fireEvent.click(acknowledge)
  expect(mutate).toHaveBeenCalledWith({ action: 'unlock_live' }, expect.anything())
})
test('a mutation makes cached readiness unknown until the authoritative refresh settles', () => {
  const view = render(<SettingsPage />)
  expect(screen.getByText('Ready')).toBeVisible()
  mockUpdateReadiness.mockReturnValueOnce({ mutate, isPending: true })
  act(() => view.rerender(<SettingsPage />))
  expect(screen.queryByText('Ready')).not.toBeInTheDocument()
  expect(screen.getByRole('status')).toHaveTextContent(/unknown|unavailable|checking/i)
  expect(screen.getByRole('button', { name: /lock live trading/i })).toBeEnabled()
  fireEvent.click(screen.getByRole('button', { name: /lock live trading/i }))
  expect(mutate).toHaveBeenCalledWith({ action: 'lock_live' })
})

test('protective lock stays reachable while initial preferences and readiness are missing', () => {
  settingsLoading = true; observation = { data: undefined, isLoading: true }
  render(<SettingsPage />)
  fireEvent.click(screen.getByRole('button', { name: /lock live trading/i }))
  expect(mutate).toHaveBeenCalledWith({ action: 'lock_live' })
})
