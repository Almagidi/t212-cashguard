import { test, expect } from '@playwright/test'
import { ensureAppPage, expectTopbarTitle } from './helpers'

test.describe('Mock/Paper Release Candidate Smoke', () => {
  test('operator page shows safe mock/paper runtime state', async ({ page }) => {
    await ensureAppPage(page, '/app/operator', 'Operator')
    await expectTopbarTitle(page, 'Operator')

    // The release candidate must not show the old Docker build/runtime mismatch warning.
    await expect(
      page.getByText(/Frontend mode is demo but backend mode is mock/i),
    ).toHaveCount(0)

    // Mock/paper safety wording should be visible.
    await expect(page.getByRole('heading', { name: 'Paper Execution', exact: true })).toBeVisible({
      timeout: 10_000,
    })
    await expect(page.getByText(/Paper only/i).first()).toBeVisible()
    await expect(page.getByText(/Mock execution/i).first()).toBeVisible()
    await expect(page.getByText(/No broker order sent/i).first()).toBeVisible()

    // Broker status wording should not imply a real broker is connected in mock mode.
    await expect(page.getByText(/Real broker configured/i)).toBeVisible()
    await expect(page.getByText(/Mock broker active/i)).toBeVisible()

    // Live execution must remain visibly gated.
    await expect(page.getByText(/Live trading possible/i)).toBeVisible()
    await expect(page.getByText(/Live enabled anywhere/i)).toBeVisible()
  })

  test('core mock lab routes render without crashing', async ({ page }) => {
    const routes = [
      { path: '/app/dashboard', title: 'Dashboard' },
      { path: '/app/orders', title: 'Orders' },
      { path: '/app/positions', title: 'Positions' },
      { path: '/app/broker', title: 'Broker' },
      { path: '/app/risk', title: 'Risk Controls' },
      { path: '/app/emergency', title: 'Emergency Controls' },
      { path: '/app/reports', title: 'Reports' },
      { path: '/app/audit', title: 'Audit' },
    ]

    for (const route of routes) {
      await ensureAppPage(page, route.path, route.title)
      await expectTopbarTitle(page, route.title)
    }
  })

  test('strategies page no longer uses ambiguous dry-run badge wording', async ({ page }) => {
    await ensureAppPage(page, '/app/strategies', 'Strategies')
    await expectTopbarTitle(page, 'Strategies')

    await expect(page.getByText(/DRY RUN/i)).toHaveCount(0)
  })

  test('emergency page keeps destructive controls behind confirmation', async ({ page }) => {
    await ensureAppPage(page, '/app/emergency', 'Emergency Controls')
    await expectTopbarTitle(page, 'Emergency Controls')

    await expect(page.locator('text=Disable Auto Trading')).toBeVisible()
    await expect(page.locator('text=Cancel All Pending Orders')).toBeVisible()
    await expect(page.locator('text=Flatten All Positions')).toBeVisible()

    const executeButtons = page.locator('button:has-text("Execute")')
    await executeButtons.first().click()

    await expect(page.getByRole('heading', { name: /Activate Kill Switch/i })).toBeVisible({
      timeout: 5_000,
    })
  })
})
