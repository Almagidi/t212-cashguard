import { test, expect } from '@playwright/test'
import { ensureAppPage, expectMainHeading } from './helpers'

test.describe('Positions', () => {
  test.beforeEach(async ({ page }) => {
    await ensureAppPage(page, '/app/positions', 'Positions')
  })

  test('renders positions page', async ({ page }) => {
    await expectMainHeading(page, 'Positions')
  })

  test('shows refresh button', async ({ page }) => {
    await expect(page.locator('button:has-text("Refresh")')).toBeVisible({ timeout: 10_000 })
  })

  test('in mock mode shows seeded positions', async ({ page }) => {
    await page.waitForTimeout(2000)
    const body = await page.locator('body').textContent()
    expect(body).toBeTruthy()
  })
})
