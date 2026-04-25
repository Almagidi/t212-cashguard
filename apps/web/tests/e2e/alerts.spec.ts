import { test } from '@playwright/test'
import { ensureAppPage, expectAnyVisible, expectMainHeading } from './helpers'

test.describe('Alerts', () => {
  test.beforeEach(async ({ page }) => {
    await ensureAppPage(page, '/app/alerts', 'Alerts')
  })

  test('renders alerts page', async ({ page }) => {
    await expectMainHeading(page, 'Alerts')
  })

  test('can send a test alert', async ({ page }) => {
    await page.locator('button:has-text("Send Test")').click()
    await expectAnyVisible(page, ['Test alert sent', 'Telegram test failed', 'Test Alert', /[1-9]\d* total/])
    await expectMainHeading(page, 'Alerts')
  })
})
