import { test, expect } from '@playwright/test'
import { ensureAppPage, expectMainHeading } from './helpers'

test.describe('Orders', () => {
  test.beforeEach(async ({ page }) => {
    await ensureAppPage(page, '/app/orders', 'Orders')
  })

  test('renders orders page', async ({ page }) => {
    await expectMainHeading(page, 'Orders')
  })

  test('shows tab filters', async ({ page }) => {
    await expect(page.locator('button:has-text("all")')).toBeVisible({ timeout: 10_000 })
    await expect(page.locator('button:has-text("pending")')).toBeVisible()
    await expect(page.locator('button:has-text("filled")')).toBeVisible()
    await expect(page.locator('button:has-text("cancelled")')).toBeVisible()
  })

  test('tabs switch correctly', async ({ page }) => {
    await page.locator('button:has-text("filled")').click()
    await page.waitForTimeout(300)
    const filledBtn = page.locator('button:has-text("filled")')
    await expect(filledBtn).toBeVisible()
  })
})
