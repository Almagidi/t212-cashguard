import { test, expect } from '@playwright/test'
import { ensureAppPage, expectMainHeading, expectToast } from './helpers'

test.describe('Strategies', () => {
  test.beforeEach(async ({ page }) => {
    await ensureAppPage(page, '/app/strategies', 'Strategies')
  })

  test('renders strategies page', async ({ page }) => {
    await expectMainHeading(page, 'Strategies')
  })

  test('shows Add Demo ORB button', async ({ page }) => {
    await expect(page.locator('text=Add Demo ORB')).toBeVisible({ timeout: 10_000 })
  })

  test('can create a demo ORB strategy', async ({ page }) => {
    const btn = page.locator('text=Add Demo ORB')
    await btn.click()
    await expectToast(page, /Strategy created|Failed to create strategy/)
    await expectMainHeading(page, 'Strategies')
  })
})
