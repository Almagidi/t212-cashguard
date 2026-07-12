import { test, expect, type Page } from '@playwright/test'
import { ensureLoggedIn } from './helpers'

/**
 * Safety invariants — regression guard for the demo/paper-mode phase.
 *
 * Until the project is explicitly approved for live-money trading, the
 * frontend must not expose interactive controls that could place, submit,
 * execute, or confirm broker orders. Read-only reporting (order history,
 * positions, execution quality, reconciliation) is allowed, as are the
 * pre-existing defensive controls (kill switch, emergency cancel/flatten)
 * and the explicitly paper-gated order form on /app/orders, which only
 * ever targets the paper endpoint with paper_only=true.
 */

const APP_PAGES = [
  '/app/dashboard',
  '/app/broker',
  '/app/orders',
  '/app/positions',
  '/app/instruments',
  '/app/strategies',
  '/app/backtest',
  '/app/reports',
  '/app/risk',
  '/app/alerts',
  '/app/audit',
  '/app/journal',
  '/app/settings',
  '/app/emergency',
  '/app/operator',
]

/**
 * Labels that indicate an order-placement or live-trading control.
 * Deliberately scoped to placement/enablement phrasing so that read-only
 * order history ("order", "execution quality"), the "Trade Journal" nav
 * link, defensive controls ("Cancel All Pending", "Flatten All Positions"
 * on /app/emergency), the paper-only form ("Submit Paper Order"), and the
 * server-gated readiness controls ("Unlock/Relock Live Trading") do not
 * false-positive. New controls matching any of these phrases fail the sweep.
 */
const DANGEROUS_CONTROL_LABEL = new RegExp(
  [
    '\\bbuy\\b',
    '\\bsell\\b',
    'trade now',
    'start trading',
    'place trade',
    'place (?:an? )?(?:live |real |broker |market |limit )?order',
    'submit (?:live|real|broker) order',
    'execute (?:order|trade)',
    'confirm order',
    'send order',
    'amend order',
    'retry order',
    'open position',
    'close position',
    'liquidate',
    'go live',
    'enable live',
  ].join('|'),
  'i',
)

async function settleAppPage(page: Page, pagePath: string) {
  await page.goto(pagePath)
  await expect(page.locator('header h1')).toBeVisible({ timeout: 10_000 })
  // Allow async cards/tables to finish their first render pass.
  await page.waitForTimeout(1500)
}

/** Visible, enabled interactive controls with their accessible text. */
async function collectEnabledControlLabels(page: Page): Promise<string[]> {
  return page.$$eval(
    'button, a[href], [role="button"], [role="link"], input[type="submit"], input[type="button"]',
    (elements) =>
      elements
        .filter((el) => {
          const isVisible =
            typeof (el as HTMLElement & { checkVisibility?: () => boolean }).checkVisibility === 'function'
              ? (el as HTMLElement & { checkVisibility: () => boolean }).checkVisibility()
              : true
          const isDisabled =
            (el as HTMLButtonElement).disabled === true || el.getAttribute('aria-disabled') === 'true'
          return isVisible && !isDisabled
        })
        .map((el) => {
          const value = el instanceof HTMLInputElement ? el.value : ''
          return [el.textContent ?? '', el.getAttribute('aria-label') ?? '', el.getAttribute('title') ?? '', value]
            .join(' ')
            .replace(/\s+/g, ' ')
            .trim()
        })
        .filter((label) => label.length > 0),
  )
}

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

test.describe('No trading controls (pre-live regression guard)', () => {
  for (const pagePath of APP_PAGES) {
    test(`no order-placement controls or forms on ${pagePath}`, async ({ page }) => {
      await ensureLoggedIn(page)
      await settleAppPage(page, pagePath)

      // 1. No visible, enabled control carries an order-placement label.
      const labels = await collectEnabledControlLabels(page)
      const offenders = labels.filter((label) => DANGEROUS_CONTROL_LABEL.test(label))
      expect(
        offenders,
        `Dangerous trading control labels found on ${pagePath}: ${offenders.join(' | ')}`,
      ).toEqual([])

      // 2. Forms with a buy/sell side selector exist only as the paper form
      //    on /app/orders — nowhere else.
      const sideForms = page
        .locator('form')
        .filter({ has: page.locator('option[value="buy"], option[value="sell"]') })
      if (pagePath === '/app/orders') {
        await expect(sideForms).toHaveCount(1)
      } else {
        await expect(sideForms).toHaveCount(0)
      }
    })
  }

  test('the only order-entry form is the explicitly paper-gated one', async ({ page }) => {
    await ensureLoggedIn(page)
    await settleAppPage(page, '/app/orders')

    const paperPanel = page.locator('[data-testid="paper-order-panel"]')
    await expect(paperPanel).toBeVisible()
    await expect(paperPanel).toContainText('No real broker order will be placed')
    await expect(paperPanel.locator('[data-testid="broker-execution-status"]')).toHaveText(
      'Broker execution disabled',
    )

    // The side-selector form lives inside the paper panel and submits via
    // the paper submit button only.
    const sideForm = page
      .locator('form')
      .filter({ has: page.locator('option[value="buy"]') })
    await expect(sideForm).toHaveCount(1)
    await expect(
      paperPanel.locator('form').filter({ has: page.locator('option[value="buy"]') }),
    ).toHaveCount(1)
    await expect(sideForm.locator('button[type="submit"]')).toHaveAttribute(
      'data-testid',
      'paper-order-submit-button',
    )
  })

  test('demo broker submit control, when rendered, is disabled', async ({ page }) => {
    await ensureLoggedIn(page)
    await settleAppPage(page, '/app/orders')

    const demoSubmit = page.locator('[data-testid="demo-order-submit-button"]')
    if ((await demoSubmit.count()) > 0) {
      // Demo mode renders the boundary panel; the submit must stay disabled.
      await expect(demoSubmit).toBeDisabled()
    } else {
      // Mock mode must not render a demo broker submit control at all.
      await expect(demoSubmit).toHaveCount(0)
    }
  })

  test('submitting the paper form never touches the live order-placement endpoint', async ({ page }) => {
    await ensureLoggedIn(page)

    const liveOrderHits: string[] = []
    const paperOrderHits: string[] = []
    await page.route('**/*', async (route) => {
      const request = route.request()
      const method = request.method()
      if (['POST', 'PUT', 'PATCH', 'DELETE'].includes(method)) {
        const pathname = new URL(request.url()).pathname.replace(/\/$/, '')
        if (/\/v1\/orders$/.test(pathname)) {
          liveOrderHits.push(`${method} ${pathname}`)
          await route.abort('blockedbyclient')
          return
        }
        if (/\/v1\/orders\/paper$/.test(pathname)) {
          paperOrderHits.push(`${method} ${pathname}`)
        }
      }
      await route.fallback()
    })

    await settleAppPage(page, '/app/orders')
    await page.fill('#paper-ticker', 'AAPL')
    await page.fill('#paper-quantity', '1')
    await page.click('[data-testid="paper-order-submit-button"]')
    await expect(page.locator('[data-testid="paper-order-status-message"]')).toBeVisible({
      timeout: 15_000,
    })

    // Whether the paper order fills or is blocked by safety controls, the
    // request must have gone to /orders/paper — never to /orders.
    expect(liveOrderHits, `UI issued live order-placement requests: ${liveOrderHits.join(', ')}`).toEqual([])
    expect(paperOrderHits.length).toBeGreaterThan(0)
  })

  test('operator dashboard remains read-only', async ({ page }) => {
    await ensureLoggedIn(page)
    await settleAppPage(page, '/app/operator')

    // Reconciliation/protective-stop visibility is reporting only: no forms,
    // no text inputs, no dangerous controls.
    await expect(page.locator('main form')).toHaveCount(0)
    await expect(page.locator('main input, main textarea, main select')).toHaveCount(0)
  })
})
