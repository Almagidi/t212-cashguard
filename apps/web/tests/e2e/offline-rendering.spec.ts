import { mkdirSync } from 'node:fs'
import path from 'node:path'
import { expect, test, type Page } from './test'
import { ensureAppPage } from './helpers'

const screenshotDirectory = process.env.E2E_SCREENSHOT_DIR

async function expectStableContainedLayout(page: Page) {
  await expect.poll(async () => page.evaluate(() => {
    const main = document.querySelector('main')
    const aside = document.querySelector('aside')
    const sidebarAligned = (
      window.innerWidth < 768 ||
      !main ||
      !aside ||
      Math.abs(main.getBoundingClientRect().left - aside.getBoundingClientRect().right) <= 1
    )
    const contained = document.documentElement.scrollWidth <= window.innerWidth
    const overflowing = contained
      ? []
      : Array.from(document.querySelectorAll<HTMLElement>('body *'))
          .map((element) => ({ element, bounds: element.getBoundingClientRect() }))
          .filter(({ bounds }) => bounds.right > window.innerWidth + 1 || bounds.left < -1)
          .slice(0, 10)
          .map(({ element, bounds }) => ({
            tag: element.tagName,
            className: element.className,
            left: bounds.left,
            right: bounds.right,
            scrollWidth: element.scrollWidth,
          }))
    const result = {
      sidebarAligned,
      contained,
      documentScrollWidth: document.documentElement.scrollWidth,
      windowInnerWidth: window.innerWidth,
      overflowing,
    }
    return sidebarAligned && contained ? 'ok' : JSON.stringify(result)
  }), { timeout: 5_000 }).toBe('ok')
}

async function capture(page: Page, name: string) {
  await expectStableContainedLayout(page)
  if (!screenshotDirectory) return
  mkdirSync(screenshotDirectory, { recursive: true })
  await page.screenshot({ path: path.join(screenshotDirectory, name), fullPage: true })
}

async function expectDesktopPaperFormContained(page: Page) {
  await expect.poll(async () => page.evaluate(() => {
    const panel = document.querySelector('[data-testid="paper-order-panel"]')
    const form = panel?.querySelector('form')
    const left = form?.parentElement
    const right = left?.nextElementSibling
    const ticker = form?.querySelector('#paper-ticker')
    const submit = form?.querySelector('[data-testid="paper-order-submit-button"]')
    if (!left || !right || !ticker || !submit) return false
    const tickerBounds = ticker.getBoundingClientRect()
    const submitBounds = submit.getBoundingClientRect()
    return tickerBounds.width >= 100 &&
      submitBounds.right <= left.getBoundingClientRect().right + 1 &&
      submitBounds.right <= right.getBoundingClientRect().left - 1
  }), { timeout: 5_000 }).toBe(true)
}

test('production operator and orders routes render offline at desktop and narrow widths', async ({ page }) => {
  const unexpectedRequests: string[] = []
  page.on('request', (request) => {
    const url = new URL(request.url())
    if (!['127.0.0.1', 'localhost'].includes(url.hostname)) {
      unexpectedRequests.push(request.url())
    }
  })

  await ensureAppPage(page, '/app/operator', 'Operator')
  await expect(page.getByRole('heading', { name: 'Read-only Operator Dashboard' })).toBeVisible()
  await expect(page.getByTestId('operator-execution-boundary')).toBeVisible({ timeout: 15_000 })
  await capture(page, 'operator-desktop.png')

  await page.setViewportSize({ width: 390, height: 844 })
  await expect(page.getByTestId('operator-execution-boundary')).toBeVisible()
  await capture(page, 'operator-narrow.png')

  await page.goto('/app/orders')
  await expect(page.getByRole('main').getByRole('heading', { name: 'Orders', exact: true })).toBeVisible()
  await expect(page.getByTestId('paper-order-panel')).toBeVisible({ timeout: 15_000 })
  await expect(page.getByRole('button', { name: /cancelled/i })).toBeVisible()
  await expect(page.getByText('Loading paper history...')).toBeHidden()
  await expect(page.getByText('Loading orders…')).toBeHidden()
  await expect(page.getByTestId('paper-order-history')).toContainText(/Paper submissions and safety blocks|No broker order sent/)
  await capture(page, 'orders-narrow.png')

  await page.setViewportSize({ width: 1280, height: 720 })
  await expect(page.getByRole('button', { name: /cancelled/i })).toBeVisible()
  await expectDesktopPaperFormContained(page)
  await capture(page, 'orders-desktop.png')

  await page.evaluate(() => document.fonts.ready)
  expect(unexpectedRequests).toEqual([])
})
