import { test, expect } from './test'
import { ensureAppPage } from './helpers'
import { selectTheme, textContrast } from './status-contrast'
type Route = import('@playwright/test').Route

async function unavailableRead(route: Route) {
  const headers = { 'access-control-allow-origin': route.request().headers().origin || '*', 'access-control-allow-credentials': 'true', 'access-control-allow-headers': 'authorization,content-type', 'access-control-allow-methods': 'GET,OPTIONS' }
  await route.fulfill({ status: route.request().method() === 'OPTIONS' ? 204 : 503, headers, contentType: 'application/json', body: route.request().method() === 'OPTIONS' ? '' : JSON.stringify({ detail: 'Synthetic unavailable observation' }) })
}

test.describe('operator observation and keyboard safety', () => {
  test('unknown settings preserve protective actions and block enabling or flattening', async ({ page }, testInfo) => {
    await page.route('**/v1/settings', unavailableRead)
    await ensureAppPage(page, '/app/emergency', 'Emergency Controls')
    await expect(page.getByTestId('kill-switch-status')).toContainText('Unknown')
    await expect(page.getByTestId('auto-trading-status')).toContainText('Unknown')
    await expect(page.getByTestId('auto-trading-enable-button')).toHaveCount(0)
    await expect(page.getByTestId('emergency-action-flatten-positions')).toBeDisabled()
    await expect(page.getByTestId('emergency-action-cancel-orders')).toBeEnabled()
    await expect(page.getByTestId('activate-kill-switch-button')).toBeEnabled()
    await page.screenshot({ path: testInfo.outputPath('unknown-controls.png'), fullPage: true })
  })

  test('confirmation traps focus, supports Escape and returns to its opener', async ({ page }, testInfo) => {
    await ensureAppPage(page, '/app/emergency', 'Emergency Controls')
    const opener = page.getByTestId('emergency-action-flatten-positions')
    await expect(opener).toBeEnabled(); await opener.click()
    const dialog = page.getByRole('dialog', { name: /Flatten all positions\?/i })
    await expect(dialog).toBeVisible()
    await expect(dialog.getByRole('button', { name: 'Cancel', exact: true })).toBeFocused()
    await page.screenshot({ path: testInfo.outputPath('confirmation.png') })
    for (const key of ['Shift+Tab', 'Tab', 'Tab', 'Tab']) {
      await page.keyboard.press(key)
      expect(await dialog.evaluate(element => element.contains(document.activeElement))).toBe(true)
    }
    await page.keyboard.press('Escape')
    await expect(dialog).toHaveCount(0); await expect(opener).toBeFocused()
  })

  test('mobile navigation contains focus, closes on Escape and releases at desktop width', async ({ page }, testInfo) => {
    await page.setViewportSize({ width: 390, height: 844 })
    await ensureAppPage(page, '/app/operator', 'Operator')
    const trigger = page.getByRole('button', { name: 'Toggle menu', exact: true, includeHidden: true })
    await trigger.click()
    const drawer = page.getByRole('dialog', { name: 'Navigation menu' })
    await expect(drawer).toBeVisible(); await expect(trigger).toHaveAttribute('aria-expanded', 'true')
    await page.screenshot({ path: testInfo.outputPath('mobile-navigation.png') })
    for (let i = 0; i < 20; i++) {
      await page.keyboard.press('Tab')
      expect(await drawer.evaluate(element => element.contains(document.activeElement))).toBe(true)
    }
    await page.keyboard.press('Escape'); await expect(trigger).toBeFocused()
    await trigger.click(); await drawer.getByRole('link', { name: 'Dashboard', exact: true }).click()
    await expect(drawer).toHaveCount(0)
    await expect(page.locator('header h1')).toHaveText('Dashboard')
    await page.goBack(); await expect(page.locator('header h1')).toHaveText('Operator')
    await trigger.click(); await page.setViewportSize({ width: 1280, height: 900 })
    await expect(drawer).toHaveCount(0)
    await expect(page.locator('main')).toBeFocused()
    expect(await page.evaluate(() => document.body.scrollWidth <= innerWidth)).toBe(true)
  })

  test('cash amounts use the selected account currency without a dollar default', async ({ page }, testInfo) => {
    await ensureAppPage(page, '/app/dashboard', 'Dashboard')
    const stats = page.getByTestId('account-stats')
    await expect(stats).toContainText(/(?:GBP|USD|EUR)\s*[\d,]+\.\d{2}/)
    await expect(stats).not.toContainText(/\$[\d,]+/)
    await page.screenshot({ path: testInfo.outputPath('dashboard.png'), fullPage: true })
  })

  test('a failed refetch cannot preserve a current HTTP200 diagnosis', async ({ page }) => {
    test.setTimeout(60_000)
    await ensureAppPage(page, '/app/operator', 'Operator')
    await expect(page.getByText('HTTP 200', { exact: true }).first()).toBeVisible()
    await page.route('**/v1/health/live', unavailableRead)
    // Focus/reconnect refetches are public query behavior; no private client access.
    await page.context().setOffline(true)
    await page.context().setOffline(false)
    await expect(page.getByTestId('api-connection-status')).not.toContainText(/Mock API|Connected/, { timeout: 35_000 })
    await expect(page.getByText('HTTP 503', { exact: true }).first()).toBeVisible({ timeout: 35_000 })
    await expect(page.getByText('Backend mock', { exact: true })).toHaveCount(0)
  })

  test('critical navigation and operator status labels meet text contrast in both themes', async ({ page }, testInfo) => {
    await ensureAppPage(page, '/app/operator', 'Operator')
    await expect(page.getByText('Backend computed gate state', { exact: true })).toBeVisible()
    for (const theme of ['light', 'dark'] as const) {
      await selectTheme(page, theme)
      await expect(page.getByTestId('live-trading-disabled-badge')).toBeVisible()
      const backdrop = await page.locator('header').evaluate(element => {
        const style = getComputedStyle(element), ctx = document.createElement('canvas').getContext('2d')!
        ctx.fillStyle = style.backgroundColor; ctx.fillRect(0, 0, 1, 1)
        return { alpha: ctx.getImageData(0, 0, 1, 1).data[3], filter: style.filter, backdropFilter: style.backdropFilter, opacity: style.opacity }
      })
      expect(backdrop, 'Critical top-bar status has an opaque, unfiltered backdrop in either theme').toEqual({ alpha: 255, filter: 'none', backdropFilter: 'none', opacity: '1' })
      const result = await textContrast(page, '.nav-section-header, a[href="/app/emergency"], [data-testid="live-trading-disabled-badge"], main .stat-sub')
      expect(result.filter(row => row.unsupported.length), 'Critical labels must have supported paint composition').toEqual([])
      expect(result.map(row => row.text)).toEqual(expect.arrayContaining(['Operations', 'Monitoring', 'System', 'Emergency', 'Live disabled', 'Backend computed gate state']))
      expect(result.filter(row => row.ratio < 4.5)).toEqual([])
      await testInfo.attach(`critical-contrast-${theme}`, { body: JSON.stringify(result), contentType: 'application/json' })
      await page.screenshot({ path: testInfo.outputPath(`operator-${theme}.png`), fullPage: true })
    }
  })
})
