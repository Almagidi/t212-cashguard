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
    await expect(page.getByText('Kill Switch', { exact: true }).first()).toBeVisible({ timeout: 10_000 })
    await expect(page.getByText('Disable Auto Trading', { exact: true }).first()).toBeVisible()
    await expect(page.getByText('Cancel All Pending Orders', { exact: true }).first()).toBeVisible()
    await expect(page.getByText('Flatten All Positions', { exact: true }).first()).toBeVisible()
  })

  test('execute button opens confirmation dialog', async ({ page }) => {
    await page.getByTestId('activate-kill-switch-button').click()
    await expect(page.getByRole('heading', { name: '⛔ Activate Kill Switch?' })).toBeVisible({ timeout: 5_000 })
  })

  test('confirmation dialog has cancel button', async ({ page }) => {
    await page.getByTestId('activate-kill-switch-button').click()
    await page.waitForTimeout(500)
    const cancelBtn = page.locator('button:has-text("Cancel")').last()
    await expect(cancelBtn).toBeVisible()
    await cancelBtn.click()
    await page.waitForTimeout(300)
    const dialog = page.locator('[class*="fixed"][class*="inset-0"]')
    await expect(dialog).toHaveCount(0)
  })

  test('confirmation dialog requires explicit confirm click', async ({ page }) => {
    await page.getByTestId('activate-kill-switch-button').click()
    await expect(page.getByTestId('confirm-activate-kill-switch-button')).toBeVisible({ timeout: 5_000 })
  })
})
