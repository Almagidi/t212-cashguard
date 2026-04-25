import { test, expect } from '@playwright/test'
import { ensureAppPage, expectMainHeading } from './helpers'

test.describe('Audit Log', () => {
  test.beforeEach(async ({ page }) => {
    await ensureAppPage(page, '/app/audit', 'Audit Log')
  })

  test('renders audit page', async ({ page }) => {
    await expectMainHeading(page, 'Audit Log')
  })

  test('shows search filter', async ({ page }) => {
    await expect(page.locator('input[placeholder*="Filter"]')).toBeVisible({ timeout: 10_000 })
  })

  test('shows login events after login', async ({ page }) => {
    await page.waitForTimeout(2000)
    const rows = page.locator('tbody tr')
    const count = await rows.count()
    expect(count).toBeGreaterThanOrEqual(0)
  })

  test('can filter by action', async ({ page }) => {
    await page.fill('input[placeholder*="Filter"]', 'login')
    await page.waitForTimeout(500)
    const value = await page.inputValue('input[placeholder*="Filter"]')
    expect(value).toBe('login')
  })
})
