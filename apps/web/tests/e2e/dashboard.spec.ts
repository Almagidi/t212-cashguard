import { test, expect } from './test'
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

  // The values are single-line and truncate with an ellipsis when they do not
  // fit, so on a phone a too-narrow card hides part of the amount.
  for (const width of [320, 375]) {
    test(`account stat values are shown in full at ${width}px`, async ({ page }) => {
      await page.setViewportSize({ width, height: 700 })
      const values = page.getByTestId('account-stats').locator('.mono-value')
      await expect(values).toHaveCount(4, { timeout: 10_000 })

      // Shrinking the viewport animates the content margin for 200ms, so wait
      // for the settled layout. A value that really does not fit stays cut.
      await expect
        .poll(
          () =>
            values.evaluateAll((elements) =>
              elements
                .filter((element) => element.clientWidth === 0 || element.scrollWidth > element.clientWidth)
                .map((element) => element.textContent),
            ),
          { timeout: 5_000 },
        )
        .toEqual([])
    })
  }

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
