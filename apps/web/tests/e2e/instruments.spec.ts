import { test, expect } from '@playwright/test'
import { ensureAppPage, expectMainHeading } from './helpers'

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
    const syncButton = page.locator('button:has-text("Sync from Broker")')

    await expect(syncButton).toBeVisible({ timeout: 10_000 })
    await syncButton.click()

    await expectMainHeading(page, 'Instruments')
    await expect(syncButton).toBeVisible({ timeout: 10_000 })
  })
})
