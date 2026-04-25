import { test, expect } from '@playwright/test'
import { ensureAppPage, ensureLoggedIn, expectMainHeading, installAuthMeStub } from './helpers'

test.describe('Reports', () => {
  test.beforeEach(async ({ page }) => {
    await ensureAppPage(page, '/app/reports', 'Reports')
  })

  test('renders reports page', async ({ page }) => {
    await expectMainHeading(page, 'Reports')
  })

  test('shows performance stats', async ({ page }) => {
    await expect(page.locator('text=Total Trades')).toBeVisible({ timeout: 10_000 })
    await expect(page.locator('text=Win Rate')).toBeVisible()
  })

  test('shows degraded API errors with retry affordance', async ({ page }) => {
    await installAuthMeStub(page)
    await page.route('**/v1/reports/performance**', async (route) => {
      await route.fulfill({
        status: 503,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'Performance API degraded' }),
      })
    })

    await ensureLoggedIn(page)
    await page.goto('/app/reports')
    await expect(page.getByText('Failed to load performance report')).toBeVisible({ timeout: 15_000 })
    await expect(page.getByText('Performance API degraded')).toBeVisible()
    await expect(page.getByRole('button', { name: 'Try again' })).toBeVisible()
  })
})
