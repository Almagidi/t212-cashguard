import { test, expect } from '@playwright/test'
import { ensureLoggedIn } from './helpers'

test.describe('Safety Invariants', () => {
  const pages = [
    '/app/dashboard', '/app/broker', '/app/settings',
    '/app/emergency', '/app/risk',
  ]

  for (const pagePath of pages) {
    test(`no deposit/bank UI on ${pagePath}`, async ({ page }) => {
      await ensureLoggedIn(page)
      await page.goto(pagePath)
      await page.waitForTimeout(1500)
      const text = (await page.locator('body').textContent())?.toLowerCase() ?? ''
      const forbidden = ['open banking', 'bank account number', 'sort code', 'add funds', 'top up', 'debit card']
      for (const term of forbidden) {
        expect(text).not.toContain(term)
      }
    })
  }
})
