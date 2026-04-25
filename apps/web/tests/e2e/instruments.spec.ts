import { test, expect } from '@playwright/test'
import { ensureAppPage, expectAnyVisible, expectMainHeading } from './helpers'

test.describe('Instruments', () => {
  test.beforeEach(async ({ page }) => {
    await ensureAppPage(page, '/app/instruments', 'Instruments')
  })

  test('renders instruments page', async ({ page }) => {
    await expectMainHeading(page, 'Instruments')
  })

  test('shows sync button', async ({ page }) => {
    await expect(page.locator('button:has-text("Sync from Broker")')).toBeVisible({ timeout: 10_000 })
  })

  test('search filter is present', async ({ page }) => {
    await expect(page.locator('input[placeholder*="Search"]')).toBeVisible({ timeout: 10_000 })
  })

  test('can trigger sync', async ({ page }) => {
    await page.locator('button:has-text("Sync from Broker")').click()
    await expectAnyVisible(page, [/Synced \d+ instruments/, 'Sync failed'])
    await expectMainHeading(page, 'Instruments')
  })
})
