import { expect, test as base } from '@playwright/test'
import { waitForBrowserApiSlot } from './rate-limit-guard'

const apiUrl = (process.env.NEXT_PUBLIC_API_URL ?? 'http://127.0.0.1:8000').replace(/\/$/, '')

export const test = base.extend({
  page: async ({ page }, runTest) => {
    const unexpectedRateLimits: string[] = []

    await page.route(`${apiUrl}/**`, async (route) => {
      if (route.request().method() !== 'OPTIONS') {
        await waitForBrowserApiSlot()
      }
      await route.fallback()
    })

    page.on('response', (response) => {
      if (response.url().startsWith(`${apiUrl}/`) && response.status() === 429) {
        unexpectedRateLimits.push(`${response.request().method()} ${new URL(response.url()).pathname}`)
      }
    })

    await runTest(page)
    await page.unrouteAll({ behavior: 'wait' })
    expect(unexpectedRateLimits, 'ordinary browser traffic received HTTP 429').toEqual([])
  },
})

export { expect }
export type { Page } from '@playwright/test'
