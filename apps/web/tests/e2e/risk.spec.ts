import { test, expect } from '@playwright/test'
import { ensureAppPage } from './helpers'

test.describe('Risk Controls', () => {
  test.beforeEach(async ({ page }) => {
    await ensureAppPage(page, '/app/risk', 'Risk Controls')
  })

  test('renders risk profile', async ({ page }) => {
    await expect(page.locator('text=Risk Profile')).toBeVisible({ timeout: 10_000 })
  })

  test('shows kill switch control', async ({ page }) => {
    await expect(page.locator('main p').filter({ hasText: /^Kill Switch$/ })).toBeVisible({ timeout: 10_000 })
  })

  test('kill switch button exists', async ({ page }) => {
    const btn = page.getByRole('button', { name: /Activate Kill Switch|Deactivate/ })
    await expect(btn).toBeVisible({ timeout: 10_000 })
  })

  test('shows risk form fields', async ({ page }) => {
    await expect(page.locator('text=Max Risk Per Trade')).toBeVisible({ timeout: 10_000 })
    await expect(page.locator('text=Max Daily Loss')).toBeVisible()
    await expect(page.locator('text=Max Open Positions')).toBeVisible()
  })
})
