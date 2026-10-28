import { test, expect } from './test'
import { ensureAppPage } from './helpers'
import { selectTheme, textContrast, waitForStablePaint } from './status-contrast'
import { waitForBrowserApiSlot } from './rate-limit-guard'
type Route = import('@playwright/test').Route
type PaperExecutionHistory = import('@/types').PaperExecutionHistory

const syntheticHistory: PaperExecutionHistory = {
  total: 1, limit: 25, items: [{
    id: '11111111-1111-4111-8111-111111111111', order_id: '22222222-2222-4222-8222-222222222222',
    created_at: '2026-10-28T00:00:00Z', updated_at: '2026-10-28T00:00:02Z', ticker: 'PAPERXYZ', side: 'buy',
    quantity: '2.00000000', notional: '51.00000000', venue: 'paper', source: 'synthetic_contrast_fixture',
    strategy: 'read-only-example', status: 'filled', risk_result: 'allowed', fill_price: '25.50000000',
    filled_quantity: '2.00000000', paper_only: true, live_order_sent: false, no_broker_order_sent: true,
    rejection_reason: null, audit_count: 3, latest_audit_at: '2026-10-28T00:00:03Z',
  }],
}

const unavailable = (route: Route) => route.fulfill({ status: route.request().method() === 'OPTIONS' ? 204 : 503, headers: { 'access-control-allow-origin': route.request().headers().origin || '*', 'access-control-allow-credentials': 'true', 'access-control-allow-headers': 'authorization,content-type', 'access-control-allow-methods': 'GET,OPTIONS' }, contentType: 'application/json', body: route.request().method() === 'OPTIONS' ? '' : JSON.stringify({ detail: 'Synthetic unavailable observation' }) })

for (const theme of ['light', 'dark'] as const) {
  test(`operator and emergency status text contrast ${theme}`, async ({ page }, testInfo) => {
    test.setTimeout(180_000)
    await page.setViewportSize({ width: 1440, height: 1000 })
    await page.route('**/v1/orders/paper?*', route => route.request().method() !== 'GET' ? route.fallback() : route.fulfill({ status: 200, headers: { 'access-control-allow-origin': route.request().headers().origin || '*', 'access-control-allow-credentials': 'true' }, contentType: 'application/json', body: JSON.stringify(syntheticHistory) }))
    // Presentation-only fixture: never arm/recover real or fixture backend state.
    await page.route('**/v1/settings', async route => {
      if (route.request().method() !== 'GET') return route.fallback()
      await waitForBrowserApiSlot()
      const response = await route.fetch()
      expect(response.ok(), `Settings fixture HTTP ${response.status()}`).toBe(true)
      await route.fulfill({ response, json: { ...await response.json(), kill_switch_active: true, auto_trading_enabled: false } })
    })
    const results: unknown[] = []
    const check = async (name: string, required: string[]) => {
      await selectTheme(page, theme)
      const rows = await textContrast(page, 'main, [data-testid="runtime-status-bar"]')
      for (const text of required) expect(rows.map(row => row.text), `${name} must actually measure ${text}`).toContain(text)
      const active = rows.filter(row => !row.disabled)
      results.push({ name, rows })
      expect.soft(active.filter(row => row.unsupported.length), `${name}: required visible text cannot silently skip composition`).toEqual([])
      expect.soft(active.filter(row => row.ratio < row.threshold), `${name}: normal text requires4.5:1, large text3:1`).toEqual([])
      await page.screenshot({ path: testInfo.outputPath(`${name}-${theme}.png`), fullPage: true, animations: 'disabled' })
    }
    const observation = page.waitForResponse(response => new URL(response.url()).pathname === '/v1/operator/status' && response.status() === 200)
    await ensureAppPage(page, '/app/operator', 'Operator')
    const payload = await (await observation).json()
    const observedReasons = [
      { code: 'synthetic_venue_blocked', severity: 'blocked', message: 'A venue kill switch is active. Trading is blocked until it is cleared.' },
      { code: 'synthetic_global_blocked', severity: 'blocked', message: 'The global kill switch is active. All trading is blocked until it is cleared from Emergency Controls.' },
      { code: 'synthetic_worker_degraded', severity: 'degraded', message: 'Background worker health is not confirmed healthy (stale, missing, or unknown).' },
    ]
    await page.route('**/v1/operator/status', route => route.fulfill({ status: 200, headers: { 'access-control-allow-origin': route.request().headers().origin || '*', 'access-control-allow-credentials': 'true' }, contentType: 'application/json', body: JSON.stringify({ ...payload, overall_status: 'blocked', why_blocked: observedReasons }) }))
    await page.reload()
    await expect(page.getByTestId('why-blocked-reasons')).toBeVisible({ timeout: 30_000 })
    await expect(page.getByTestId('broker-runtime-status')).toBeVisible({ timeout: 30_000 })
    await check('blocked', ['Why blocked / degraded', 'Read-only broker visibility. This panel does not connect, disconnect, trade, or place orders.'])
    const audit = page.locator('summary').filter({ hasText: 'Audit review' }).first()
    await expect(audit).toBeVisible({ timeout: 30_000 }); await audit.focus(); await page.keyboard.press('Enter')
    await expect(audit.locator('..')).toHaveAttribute('open', '')
    await check('paper-audit-keyboard-open', ['Audit review', 'Risk:', 'allowed', 'Audit events:', '3', 'Live order sent:', 'false'])
    await page.setViewportSize({ width: 390, height: 844 }); await check('blocked-mobile', ['Why blocked / degraded'])
    await page.setViewportSize({ width: 1440, height: 1000 })
    await page.route('**/v1/operator/status', route => route.fulfill({ status: 200, headers: { 'access-control-allow-origin': route.request().headers().origin || '*', 'access-control-allow-credentials': 'true' }, contentType: 'application/json', body: JSON.stringify({ ...payload, overall_status: 'degraded', why_blocked: [{ code: 'synthetic_degraded', severity: 'degraded', message: 'Synthetic degraded status requires review.' }] }) }))
    await page.reload(); await expect(page.getByText('Synthetic degraded status requires review.', { exact: true })).toBeVisible({ timeout: 30_000 })
    await check('degraded', ['Synthetic degraded status requires review.'])
    await page.setViewportSize({ width: 390, height: 844 }); await check('degraded-mobile', ['Synthetic degraded status requires review.'])
    await page.setViewportSize({ width: 1440, height: 1000 })
    await page.unroute('**/v1/operator/status')
    for (const url of ['**/v1/broker/trading212/status', '**/v1/broker/trading212/reconciliation/status', '**/v1/broker/trading212/reconciliation/scheduler/status', '**/v1/account/cash-guard', '**/v1/orders/paper?*']) await page.route(url, unavailable)
    await page.reload()
    for (const text of ['Cash snapshot unavailable', 'Paper execution history unavailable', 'Reconciliation status unavailable.', 'Broker status is unavailable. Treat broker connectivity as unknown.']) await expect(page.getByText(text, { exact: true })).toBeVisible({ timeout: 30_000 })
    await check('dependent-errors', ['Cash snapshot unavailable', 'Paper execution history unavailable', 'Reconciliation status unavailable.', 'Broker status is unavailable. Treat broker connectivity as unknown.'])
    await page.setViewportSize({ width: 390, height: 844 }); await check('dependent-errors-mobile', ['Cash snapshot unavailable', 'Paper execution history unavailable', 'Reconciliation status unavailable.', 'Broker status is unavailable. Treat broker connectivity as unknown.'])
    await page.setViewportSize({ width: 1440, height: 1000 })
    await page.route('**/v1/operator/status', unavailable); await page.reload()
    await expect(page.getByText('Operator status unavailable', { exact: true })).toBeVisible({ timeout: 30_000 })
    await check('operator-unavailable', ['Operator status unavailable'])
    await page.setViewportSize({ width: 390, height: 844 }); await check('operator-unavailable-mobile', ['Operator status unavailable'])
    await page.unroute('**/v1/operator/status')
    await page.setViewportSize({ width: 1440, height: 1000 }); await page.goto('/app/emergency')
    await expect(page.getByTestId('disable-kill-switch-button')).toBeEnabled({ timeout: 30_000 })
    await check('emergency-blocked', ['Immediate and irreversible', 'Disable Kill Switch', 'Operator recovery sequence'])
    const recovery = page.getByTestId('disable-kill-switch-button')
    await recovery.focus(); await page.keyboard.press('Tab'); await page.keyboard.press('Shift+Tab')
    await expect(recovery).toBeFocused()
    expect(await recovery.evaluate(element => element.matches(':focus-visible'))).toBe(true)
    // Measure rendered focus styling after the finite CSS transition, not its target variables mid-transition.
    await waitForStablePaint(page)
    const focus = await recovery.evaluate(element => {
      const style = getComputedStyle(element), canvas = document.createElement('canvas'), ctx = canvas.getContext('2d')!
      const rgb = (color: string) => {
        const span = document.createElement('span'); span.style.color = color; element.append(span)
        const resolved = getComputedStyle(span).color; span.remove()
        ctx.clearRect(0, 0, 1, 1); ctx.fillStyle = resolved; ctx.fillRect(0, 0, 1, 1)
        return [...ctx.getImageData(0, 0, 1, 1).data]
      }
      const luminance = (rgb: number[]) => rgb.slice(0, 3).map(n => n / 255).map(n => n <= .04045 ? n / 12.92 : ((n + .055) / 1.055) ** 2.4).reduce((s, n, i) => s + n * [.2126, .7152, .0722][i], 0)
      const ring = rgb(style.getPropertyValue('--tw-ring-color')), adjacent = rgb(style.getPropertyValue('--tw-ring-offset-color'))
      const a = luminance(ring), b = luminance(adjacent)
      const shadowColors = (style.boxShadow.match(/(?:rgba?|lab|oklab|color)\([^)]*\)/g) || []).map(rgb)
      return { boxShadow: style.boxShadow, ring, adjacent, actualOpaqueRing: shadowColors.some(color => color[3] === 255 && ring.slice(0, 3).every((n, i) => Math.abs(n - color[i]) <= 1)), ratio: (Math.max(a, b) + .05) / (Math.min(a, b) + .05) }
    })
    expect(focus.boxShadow).not.toBe('none')
    expect(focus.actualOpaqueRing).toBe(true)
    expect(focus.boxShadow).toContain('0px 0px 0px 4px')
    expect(focus.ring[3]).toBe(255); expect(focus.adjacent[3]).toBe(255)
    expect(focus.ratio).toBeGreaterThanOrEqual(3)
    results.push({ name: 'recovery-focus-indicator', ...focus })
    await check('emergency-keyboard-focus', ['Disable Kill Switch'])
    await recovery.hover(); await check('emergency-hover', ['Disable Kill Switch'])
    await page.setViewportSize({ width: 390, height: 844 }); await check('emergency-mobile', ['Operator recovery sequence'])
    await page.route('**/v1/settings', unavailable); await page.reload()
    await expect(page.getByTestId('kill-switch-status')).toContainText('Unknown', { timeout: 30_000 })
    await expect(page.getByTestId('emergency-action-flatten-positions')).toBeDisabled()
    expect(await page.getByTestId('emergency-action-flatten-positions').evaluate(element => { (element as HTMLButtonElement).focus(); return document.activeElement === element })).toBe(false)
    await expect(page.getByTestId('activate-kill-switch-button')).toBeEnabled()
    await check('emergency-unknown-mobile', ['Unknown', 'Operator recovery sequence'])
    await page.setViewportSize({ width: 1440, height: 1000 }); await check('emergency-unknown', ['Unknown', 'Operator recovery sequence'])
    await testInfo.attach(`status-contrast-${theme}`, { body: JSON.stringify(results), contentType: 'application/json' })
  })
}
