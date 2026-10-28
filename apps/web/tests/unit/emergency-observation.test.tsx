import '@testing-library/jest-dom'
import { beforeEach, describe, expect, it, jest } from '@jest/globals'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

const settings = { auto_trading_enabled: false, kill_switch_active: false, live_trading_unlocked: false }
let query: Record<string, unknown>
const invoke = jest.fn<() => Promise<void>>()
const mutation = () => ({ mutate: invoke, mutateAsync: invoke, isPending: false })
jest.mock('@/hooks/use-api', () => ({
  useSettings: () => query,
  useEmergencyKillSwitch: () => mutation(), useEmergencyDisableKillSwitch: () => mutation(),
  useEmergencyAutoTradingOff: () => mutation(), useEmergencyAutoTradingOn: () => mutation(),
  useEmergencyCancelAll: () => mutation(), useEmergencyFlattenAll: () => mutation(),
}))
const EmergencyPage = require('@/app/app/emergency/page').default

describe('authoritative emergency observation', () => {
  beforeEach(() => { invoke.mockClear(); query = { data: settings, isLoading: false, isError: false, isFetching: false, failureCount: 0, dataUpdatedAt: 1000 } })

  it.each([
    { data: undefined, isError: true },
    { data: undefined, isLoading: true, isPending: true },
    { data: settings, isError: true },
    { data: settings, failureCount: 1, isFetching: true },
    { data: { auto_trading_enabled: false, kill_switch_active: null } },
    { data: { auto_trading_enabled: 'false', kill_switch_active: false } },
  ])('does not invent safe control state for unavailable/malformed data %#', patch => {
    query = { ...query, ...patch }; render(<EmergencyPage />)
    expect(screen.getByTestId('kill-switch-status')).toHaveTextContent('Unknown')
    expect(screen.getByTestId('auto-trading-status')).toHaveTextContent('Unknown')
    expect(screen.queryByTestId('auto-trading-enable-button')).not.toBeInTheDocument()
    expect(screen.getByTestId('emergency-action-flatten-positions')).toBeDisabled()
    expect(screen.getByTestId('emergency-action-cancel-orders')).not.toBeDisabled()
  })

  it('keeps known disabled controls accurate', () => {
    render(<EmergencyPage />)
    expect(screen.getByTestId('kill-switch-status')).toHaveTextContent('Inactive')
    expect(screen.getByTestId('auto-trading-status')).toHaveTextContent('Disabled')
  })

  it('rechecks authority if a destructive confirmation is open when a read fails', async () => {
    const user = userEvent.setup(); const { rerender } = render(<EmergencyPage />)
    await user.click(screen.getByTestId('emergency-action-flatten-positions'))
    query = { ...query, isError: true }; rerender(<EmergencyPage />)
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    const confirm = screen.getByRole('button', { name: 'Flatten All Positions' })
    expect(confirm).toBeDisabled(); await user.click(confirm); expect(invoke).not.toHaveBeenCalled()
  })
})
