import { test, expect, type Page } from './test'
import { ensureLoggedIn } from './helpers'

function snapshot(enabled: boolean, ts = new Date().toISOString()) {
  return {
    type: 'snapshot', ts,
    account: { total_value: 10000, free_cash: 10000, invested: 0, currency: 'GBP', unrealized_pnl: 0 },
    positions: [], signals: [], orders: [],
    system: { auto_trading_enabled: enabled, kill_switch_active: false, max_daily_loss_pct: 3 },
    regime: {
      regime: 'ranging', label: 'Synthetic range', color: 'gray', adx: 10,
      vol_percentile: 50, confidence: 0.5, active_strategies: [], suppressed_strategies: [],
    },
  }
}

const automationRow = (page: Page) =>
  page.getByText('Auto Trading', { exact: true }).locator('..')

test('mock dashboard automation matches the real backend settings and WebSocket', async ({ page }) => {
  let streamed: { auto_trading_enabled: boolean } | undefined
  page.on('websocket', socket => {
    if (new URL(socket.url()).pathname !== '/v1/ws/live') return
    socket.on('framereceived', ({ payload }) => {
      const frame = JSON.parse(String(payload))
      if (frame.type === 'snapshot') streamed = frame.system
    })
  })
  await ensureLoggedIn(page)
  const saved = await page.evaluate(async apiUrl => {
    const response = await fetch(`${apiUrl}/v1/settings`, {
      headers: { Authorization: `Bearer ${localStorage.getItem('cg_token')}` },
    })
    if (!response.ok) throw new Error(`Settings returned ${response.status}`)
    return response.json() as Promise<{ auto_trading_enabled: boolean }>
  }, process.env.NEXT_PUBLIC_API_URL ?? 'http://127.0.0.1:8000')
  await expect.poll(() => streamed?.auto_trading_enabled).toBe(saved.auto_trading_enabled)
  await expect(automationRow(page)).toContainText(saved.auto_trading_enabled ? 'Enabled' : 'Disabled')
})

for (const enabled of [false, true]) {
  test(`automation reports a fresh ${enabled ? 'enabled' : 'disabled'} snapshot`, async ({ page }) => {
    await page.routeWebSocket('**/v1/ws/live', socket => {
      socket.onMessage(() => socket.send(JSON.stringify(snapshot(enabled))))
    })
    await ensureLoggedIn(page)
    await expect(automationRow(page)).toContainText(enabled ? 'Enabled' : 'Disabled')
  })
}

test('an expired enabled snapshot is shown as unknown', async ({ page }) => {
  await page.routeWebSocket('**/v1/ws/live', socket => {
    socket.onMessage(() => socket.send(JSON.stringify(snapshot(true, new Date(Date.now() - 60_000).toISOString()))))
  })
  await ensureLoggedIn(page)
  await expect(automationRow(page)).toContainText('Unknown')
})

test('a socket error cannot keep claiming enabled, including during reconnect', async ({ page }) => {
  let closeCurrentSocket: (() => void) | undefined
  let connections = 0
  let supplySnapshots = true
  await page.routeWebSocket('**/v1/ws/live', socket => {
    connections += 1
    socket.onMessage(() => {
      if (!supplySnapshots) return
      closeCurrentSocket = () => socket.close({ code: 1011, reason: 'synthetic stream read error' })
      socket.send(JSON.stringify(snapshot(true)))
    })
  })
  await ensureLoggedIn(page)
  await expect(automationRow(page)).toContainText('Enabled')
  expect(closeCurrentSocket).toBeDefined()
  const connectionsBeforeError = connections
  supplySnapshots = false
  closeCurrentSocket!()
  await expect(automationRow(page)).toContainText('Unknown')
  await expect.poll(() => connections).toBeGreaterThan(connectionsBeforeError)
  await expect(automationRow(page)).toContainText('Unknown')
})
