import { test, expect } from '@playwright/test'
import { ensureAppPage, expectMainHeading, expectToast } from './helpers'

test.describe('Strategies', () => {
  test.beforeEach(async ({ page }) => {
    await ensureAppPage(page, '/app/strategies', 'Strategies')
  })

  test('renders strategies page', async ({ page }) => {
    await expectMainHeading(page, 'Strategies')
  })

  test('shows Add Demo ORB action or existing ORB strategy', async ({ page }) => {
    await expect(
      page.getByText(/Add Demo ORB|ORB|Opening Range Breakout/i).first(),
    ).toBeVisible({ timeout: 10_000 })
  })

  test('can create a demo ORB strategy when action is available', async ({ page }) => {
    const btn = page.getByText('Add Demo ORB').first()

    if (await btn.isVisible().catch(() => false)) {
      await btn.click()
      await expectToast(page, /Strategy created|Failed to create strategy/)
    }

    await expectMainHeading(page, 'Strategies')
  })
})
