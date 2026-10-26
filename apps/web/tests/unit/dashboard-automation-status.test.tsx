import { act, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, jest } from '@jest/globals'
import { AutomationStatus } from '@/components/dashboard/automation-status'

const NOW = new Date('2026-10-26T12:00:00.000Z')
const frame = (enabled: boolean, ts = NOW.toISOString()) => ({
  ts,
  system: { auto_trading_enabled: enabled, kill_switch_active: false, max_daily_loss_pct: 3 },
})

describe('dashboard automation status', () => {
  beforeEach(() => {
    jest.useFakeTimers()
    jest.setSystemTime(NOW)
  })
  afterEach(() => {
    jest.useRealTimers()
  })

  it.each([false, true])('reports the current saved %s state from a fresh frame', enabled => {
    render(<AutomationStatus snapshot={frame(enabled)} status="connected" />)
    expect(screen.getByText(enabled ? 'Enabled' : 'Disabled')).toBeVisible()
  })

  it.each(['connecting', 'reconnecting', 'disconnected'] as const)(
    'reports unknown while %s even if the last frame said enabled', status => {
      render(<AutomationStatus snapshot={frame(true)} status={status} />)
      expect(screen.getByText('Unknown')).toBeVisible()
      expect(screen.queryByText('Enabled')).not.toBeInTheDocument()
    },
  )

  it.each(['not-a-time', new Date(NOW.getTime() - 10_001).toISOString(), new Date(NOW.getTime() + 60_000).toISOString()])(
    'rejects an invalid, stale or future timestamp: %s', ts => {
      render(<AutomationStatus snapshot={frame(true, ts)} status="connected" />)
      expect(screen.getByText('Unknown')).toBeVisible()
    },
  )

  it('expires the displayed state even if the connection keeps receiving pings', () => {
    render(<AutomationStatus snapshot={frame(true)} status="connected" />)
    expect(screen.getByText('Enabled')).toBeVisible()
    act(() => jest.advanceTimersByTime(11_000))
    expect(screen.getByText('Unknown')).toBeVisible()
  })

  it('does not reuse a frame from a failed connection before a new snapshot arrives', () => {
    const previous = frame(true)
    const view = render(<AutomationStatus snapshot={previous} status="connected" />)
    view.rerender(<AutomationStatus snapshot={previous} status="disconnected" />)
    expect(screen.getByText('Unknown')).toBeVisible()
    view.rerender(<AutomationStatus snapshot={previous} status="connected" />)
    expect(screen.getByText('Unknown')).toBeVisible()
    view.rerender(<AutomationStatus snapshot={frame(false)} status="connected" />)
    expect(screen.getByText('Disabled')).toBeVisible()
  })

  it('reports unknown when no snapshot has arrived', () => {
    render(<AutomationStatus snapshot={null} status="connected" />)
    expect(screen.getByText('Unknown')).toBeVisible()
  })
})
