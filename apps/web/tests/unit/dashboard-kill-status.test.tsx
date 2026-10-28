import '@testing-library/jest-dom'
import { act, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, jest } from '@jest/globals'
import { KillSwitchStatus } from '@/components/dashboard/automation-status'

describe('dashboard kill status shares accepted control snapshot freshness', () => {
  const now = new Date('2026-10-27T12:00:00Z')
  const frame = (active = false) => ({ ts: now.toISOString(), system: { auto_trading_enabled: false, kill_switch_active: active, max_daily_loss_pct: 3 } })
  beforeEach(() => { jest.useFakeTimers(); jest.setSystemTime(now) })
  afterEach(() => { jest.useRealTimers() })
  it.each([false, true])('reports only the current Boolean %s', active => {
    render(<KillSwitchStatus snapshot={frame(active)} status="connected" lastFrameAt={now} />)
    expect(screen.getByText(active ? 'ACTIVE' : 'Inactive')).toBeVisible()
  })
  it('becomes unknown on interruption and waits for a new reconnect snapshot', () => {
    const old = frame(); const { rerender } = render(<KillSwitchStatus snapshot={old} status="connected" lastFrameAt={now} />)
    rerender(<KillSwitchStatus snapshot={old} status="reconnecting" lastFrameAt={now} />)
    expect(screen.getByText('Unknown')).toBeVisible()
    rerender(<KillSwitchStatus snapshot={old} status="connected" lastFrameAt={now} />)
    expect(screen.getByText('Unknown')).toBeVisible()
    rerender(<KillSwitchStatus snapshot={frame()} status="connected" lastFrameAt={now} />)
    expect(screen.getByText('Inactive')).toBeVisible()
    act(() => jest.advanceTimersByTime(11_000))
    expect(screen.getByText('Unknown')).toBeVisible()
  })
})
