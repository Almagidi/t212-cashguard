import { test, expect } from '@playwright/test'
import { ensureAppPage, expectTopbarTitle } from './helpers'

test.describe('Dashboard', () => {
  test.beforeEach(async ({ page }) => {
    await ensureAppPage(page, '/app/dashboard', 'Dashboard')
  })

  test('renders overview section', async ({ page }) => {
    await expectTopbarTitle(page, 'Dashboard')
    await expect(page.getByRole('heading', { name: 'Portfolio Overview' })).toBeVisible({ timeout: 10_000 })
  })

  test('shows account stats cards', async ({ page }) => {
    await expect(page.locator('text=Total Value')).toBeVisible({ timeout: 10_000 })
    await expect(page.locator('text=Available to Trade')).toBeVisible()
  })

  test('shows mode badge in topbar', async ({ page }) => {
    const badge = page.locator('.badge-mock, .badge-demo, .badge-live')
    await expect(badge.first()).toBeVisible({ timeout: 10_000 })
  })

  test('shows auto trading status', async ({ page }) => {
    await expect(page.locator('text=Auto Trading')).toBeVisible({ timeout: 10_000 })
  })

  test('shows kill switch status', async ({ page }) => {
    await expect(page.locator('text=Kill Switch')).toBeVisible({ timeout: 10_000 })
  })

  test('sidebar navigation is present', async ({ page }) => {
    const sidebar = page.locator('aside').first()
    await expect(sidebar.getByRole('link', { name: 'Dashboard', exact: true })).toBeVisible()
    await expect(sidebar.getByRole('link', { name: 'Strategies', exact: true })).toBeVisible()
    await expect(sidebar.getByRole('link', { name: 'Orders', exact: true })).toBeVisible()
    await expect(sidebar.getByRole('link', { name: 'Risk Controls', exact: true })).toBeVisible()
    await expect(sidebar.getByRole('link', { name: 'Emergency', exact: true })).toBeVisible()
  })
})
