import { test, expect } from '@playwright/test'
import { ensureAppPage, expectMainHeading } from './helpers'

test.describe('Emergency Controls', () => {
  test.beforeEach(async ({ page }) => {
    await ensureAppPage(page, '/app/emergency', 'Emergency Controls')
  })

  test('shows danger warning header', async ({ page }) => {
    await expectMainHeading(page, 'Emergency Controls')
  })

  test('shows all four emergency actions', async ({ page }) => {
    await expect(page.locator('main p').filter({ hasText: /^Kill Switch$/ }).nth(1)).toBeVisible({ timeout: 10_000 })
    await expect(page.locator('text=Disable Auto Trading')).toBeVisible()
    await expect(page.locator('text=Cancel All Pending Orders')).toBeVisible()
    await expect(page.locator('text=Flatten All Positions')).toBeVisible()
  })

  test('execute button opens confirmation dialog', async ({ page }) => {
    const executeButtons = page.locator('button:has-text("Execute")')
    await executeButtons.first().click()
    await expect(page.getByRole('heading', { name: '⛔ Activate Kill Switch?' })).toBeVisible({ timeout: 5_000 })
  })

  test('confirmation dialog has cancel button', async ({ page }) => {
    const executeButtons = page.locator('button:has-text("Execute")')
    await executeButtons.first().click()
    await page.waitForTimeout(500)
    const cancelBtn = page.locator('button:has-text("Cancel")').last()
    await expect(cancelBtn).toBeVisible()
    await cancelBtn.click()
    await page.waitForTimeout(300)
    const dialog = page.locator('[class*="fixed"][class*="inset-0"]')
    await expect(dialog).toHaveCount(0)
  })

  test('confirmation dialog requires explicit confirm click', async ({ page }) => {
    const executeButtons = page.locator('button:has-text("Execute")')
    await executeButtons.first().click()
    await expect(page.locator('button:has-text("Activate Kill Switch")')).toBeVisible({ timeout: 5_000 })
  })
})
