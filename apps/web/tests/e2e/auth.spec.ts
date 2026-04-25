import { test, expect } from '@playwright/test'
import { adminEmail, clearClientAuth, expectTopbarTitle, loginThroughUi } from './helpers'

test.describe('Authentication', () => {
  test('login page renders correctly', async ({ page }) => {
    await page.goto('/auth/login')
    await expect(page.locator('text=CashGuard Trader')).toBeVisible()
    await expect(page.locator('input[name="email"]')).toBeVisible()
    await expect(page.locator('input[type="password"]')).toBeVisible()
    await expect(page.locator('button[type="submit"]')).toBeVisible()
  })

  test('shows error on wrong credentials', async ({ page }) => {
    await page.goto('/auth/login')
    await page.fill('input[name="email"]', adminEmail)
    await page.fill('input[type="password"]', 'wrongpassword')
    await page.click('button[type="submit"]')
    await expect(page).toHaveURL(/login/, { timeout: 5_000 })
  })

  test('logs in with valid credentials', async ({ page }) => {
    await loginThroughUi(page)
    await expectTopbarTitle(page, 'Dashboard')
  })

  test('redirects unauthenticated users', async ({ page }) => {
    await clearClientAuth(page)
    await page.goto('/app/dashboard')
    await expect(page).toHaveURL(/\/auth\/login/, { timeout: 10_000 })
    await expect(page.locator('input[name="email"]')).toBeVisible()
  })
})
