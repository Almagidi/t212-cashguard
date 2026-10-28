import { test, expect } from './test'
import { ensureAppPage } from './helpers'
type WebSocketRoute = import('@playwright/test').WebSocketRoute

test('mock account examples stay distinct through delayed, unidentified and reconnected stream frames', async ({ page }) => {
  test.setTimeout(60_000)
  let socket: WebSocketRoute | undefined
  await page.routeWebSocket('**/v1/ws/live', route => { socket = route; route.onMessage(() => {}) })
  await ensureAppPage(page, '/app/dashboard', 'Dashboard')
  const notice = page.getByTestId('account-source-notice')
  await expect(notice).toContainText('Mock adapter example')
  await expect(notice).toContainText('not a paper ledger balance')
  await expect(page.getByTestId('adapter-cash-example')).toContainText('USD 10,000.00')
  const frame = (identified: boolean) => ({ type: 'snapshot', ts: new Date().toISOString(), ...(identified ? { data_source: 'mock_market_animation' } : {}), account: { total_value: 10989.8, free_cash: 8234.8, invested: 2755, currency: 'USD', unrealized_pnl: 142.5 }, positions: [], signals: [], orders: [], system: { auto_trading_enabled: false, kill_switch_active: true, max_daily_loss_pct: 2 }, regime: { regime: 'ranging', label: 'Synthetic', color: 'gray', adx: 0, vol_percentile: 0, confidence: 0, active_strategies: [], suppressed_strategies: [] } })
  await expect.poll(() => !!socket).toBe(true)
  socket!.send(JSON.stringify(frame(false)))
  await expect(notice).toContainText('Stream source unverified')
  await expect(notice).not.toContainText('Mock animation example')
  socket!.send(JSON.stringify(frame(true)))
  await expect(notice).toContainText('Mock animation example')
  await expect(notice).toContainText('not reconciled')
  await expect(page.getByTestId('account-stats')).toContainText('USD 10,989.80')
  await expect(page.getByTestId('account-stats')).toContainText('USD 8,234.80')
  await expect(page.getByTestId('adapter-cash-example')).toContainText('USD 10,000.00')
  socket!.close({ code: 1001, reason: 'synthetic interruption' }); socket = undefined
  await expect(notice).toContainText('Mock animation example')
  await expect(notice).toContainText('not reconciled')
  await expect.poll(() => !!socket).toBe(true)
  socket!.send(JSON.stringify(frame(true)))
  await expect(notice).toContainText('Mock animation example')
  await page.waitForTimeout(11_000)
  await expect(notice).toContainText('not reconciled')
  await expect(page.getByTestId('account-stats')).not.toContainText('GBP')
})
