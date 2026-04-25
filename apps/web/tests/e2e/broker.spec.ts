import { test, expect } from '@playwright/test'
import { ensureAppPage } from './helpers'

test.describe('Broker', () => {
  test.beforeEach(async ({ page }) => {
    await ensureAppPage(page, '/app/broker', 'Broker Account')
  })

  test('shows cash-only warning banner', async ({ page }) => {
    await expect(page.locator('text=Cash-Only Mode Enforced')).toBeVisible({ timeout: 10_000 })
  })

  test('shows connect form with environment selector', async ({ page }) => {
    const form = page.locator('form')
    await expect(page.locator('text=Connect Trading 212')).toBeVisible({ timeout: 10_000 })
    await expect(form.locator('label').filter({ hasText: /^Demo$/ })).toBeVisible()
    await expect(form.locator('label').filter({ hasText: 'Live' })).toBeVisible()
  })

  test('live environment shows restricted badge', async ({ page }) => {
    await expect(page.locator('text=Restricted')).toBeVisible({ timeout: 10_000 })
  })

  test('mentions credential encryption', async ({ page }) => {
    await expect(page.getByText('encrypted at rest')).toBeVisible({ timeout: 10_000 })
  })
})
