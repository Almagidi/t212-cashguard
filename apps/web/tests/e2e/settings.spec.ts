import { test, expect } from '@playwright/test'
import { ensureAppPage, expectTopbarTitle } from './helpers'

test.describe('Settings', () => {
  test.beforeEach(async ({ page }) => {
    await ensureAppPage(page, '/app/settings', 'Settings')
  })

  test('renders settings page', async ({ page }) => {
    await expectTopbarTitle(page, 'Settings')
    await expect(page.getByRole('heading', { name: 'Application' })).toBeVisible({ timeout: 10_000 })
  })

  test('shows cash-only guarantee notice', async ({ page }) => {
    await expect(page.locator('text=Safety Guarantees')).toBeVisible({ timeout: 10_000 })
    await expect(page.locator('text=Cash-only mode is permanently enforced')).toBeVisible()
  })

  test('shows no deposit or bank options', async ({ page }) => {
    const pageText = await page.locator('body').textContent()
    expect(pageText?.toLowerCase()).not.toContain('deposit')
    expect(pageText?.toLowerCase()).not.toContain('bank account')
    expect(pageText?.toLowerCase()).not.toContain('open banking')
    expect(pageText?.toLowerCase()).not.toContain('debit card')
  })

  test('theme toggle works', async ({ page }) => {
    const lightBtn = page.locator('form').getByRole('button', { name: 'Light' })
    await expect(lightBtn).toBeVisible({ timeout: 10_000 })
    await lightBtn.click()
    await page.waitForTimeout(300)
    const saveBtn = page.locator('button:has-text("Save Settings")')
    await expect(saveBtn).toBeEnabled()
  })
})
