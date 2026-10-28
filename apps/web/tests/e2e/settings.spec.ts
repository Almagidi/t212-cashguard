import { test, expect } from './test'
import { ensureAppPage, expectTopbarTitle } from './helpers'

test.describe('Settings', () => {
  test.beforeEach(async ({ page }) => {
    await ensureAppPage(page, '/app/settings', 'Settings')
  })

  test('renders settings page', async ({ page }) => {
    await expectTopbarTitle(page, 'Settings')
    await expect(page.getByRole('heading', { name: 'Application' })).toBeVisible({ timeout: 10_000 })
  })

  test('shows cash-only guarantee notice', async ({ page }) => {
    await expect(page.locator('text=Safety Guarantees')).toBeVisible({ timeout: 10_000 })
    await expect(page.locator('text=Cash-only mode is permanently enforced')).toBeVisible()
  })

  test('shows no deposit or bank options', async ({ page }) => {
    // Inspect the loaded controls, not the initial loading screen or the
    // safety notice that explicitly says no funding integration exists.
    await expect(page.getByRole('button', { name: 'Save Settings', exact: true })).toBeVisible()
    await expect(page.getByText('No deposit, withdrawal, or bank integration code exists in this application', { exact: true })).toBeVisible()
    const funding = /deposit|withdraw|bank account|open banking|debit card|top.?up/i
    for (const role of ['button', 'link', 'textbox', 'combobox', 'checkbox', 'radio', 'switch', 'tab', 'menuitem', 'menuitemcheckbox', 'menuitemradio', 'option'] as const) {
      await expect(page.getByRole(role, { name: funding })).toHaveCount(0)
    }
  })

  test('theme toggle works', async ({ page }) => {
    const lightBtn = page.locator('form').getByRole('button', { name: 'Light' })
    await expect(lightBtn).toBeVisible({ timeout: 10_000 })
    await lightBtn.click()
    await page.waitForTimeout(300)
    const saveBtn = page.locator('button:has-text("Save Settings")')
    await expect(saveBtn).toBeEnabled()
  })
})
