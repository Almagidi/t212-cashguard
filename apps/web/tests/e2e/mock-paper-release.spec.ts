import { test, expect } from '@playwright/test'
import { adminEmail, adminPassword, ensureAppPage, expectTopbarTitle, installAuthMeStub } from './helpers'

const API_URL = (process.env.NEXT_PUBLIC_API_URL ?? 'http://127.0.0.1:8000').replace(/\/$/, '')

async function adminToken(request: import('@playwright/test').APIRequestContext): Promise<string> {
  const res = await request.post(`${API_URL}/v1/auth/login`, {
    data: { email: adminEmail, password: adminPassword },
  })
  expect(res.ok(), `login failed with status ${res.status()}: ${await res.text()}`).toBe(true)
  const body = await res.json() as { access_token?: string }
  expect(body.access_token).toBeTruthy()
  return body.access_token!
}

test.describe('Mock/Paper Release Candidate Smoke', () => {
  test.skip(
    (process.env.NEXT_PUBLIC_APP_MODE ?? 'mock') !== 'mock',
    'Mock/paper release journey is mock-mode only.',
  )

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

    await page.getByRole('button', { name: 'Activate Kill Switch' }).click()

    await expect(page.getByRole('heading', { name: /Activate Kill Switch/i })).toBeVisible({
      timeout: 5_000,
    })
  })

  test('emergency page supports explicit kill-switch recovery without resuming auto-trading', async ({ page, request }) => {
    const token = await adminToken(request)
    const headers = { Authorization: `Bearer ${token}` }
    const reset = await request.post(`${API_URL}/v1/risk/kill-switch/disable`, { headers })
    expect(reset.ok(), `kill-switch reset failed with status ${reset.status()}: ${await reset.text()}`).toBe(true)

    const forbiddenRecoveryCalls: string[] = []
    await page.route('**/*', async (route) => {
      const req = route.request()
      const url = req.url()
      const method = req.method()
      const pathname = new URL(url).pathname.replace(/^\/api/, '')

      if (method !== 'GET' && pathname.startsWith('/v1/broker/')) {
        forbiddenRecoveryCalls.push(`${method} ${pathname}`)
        await route.abort('failed')
        return
      }

      await route.continue()
    })

    await installAuthMeStub(page)
    await page.goto('/auth/login')
    await page.evaluate((accessToken) => {
      localStorage.setItem('cg_token', accessToken)
    }, token)
    await page.goto('/app/emergency')
    await expectTopbarTitle(page, 'Emergency Controls')

    await expect(page.getByText('Kill Switch').first()).toBeVisible()
    await expect(page.getByText(/^Inactive$/).first()).toBeVisible()
    await page.getByRole('button', { name: 'Activate Kill Switch' }).click()
    await expect(page.getByRole('heading', { name: /Activate Kill Switch/i })).toBeVisible()
    await page.getByRole('button', { name: 'Activate Kill Switch' }).last().click()

    await expect(page.getByText(/^ACTIVE$/).first()).toBeVisible({ timeout: 10_000 })
    await expect(page.getByText(/Disable the kill switch separately from auto-trading/i)).toBeVisible()
    await page.getByRole('button', { name: 'Disable Kill Switch' }).click()
    await expect(page.getByRole('heading', { name: /Disable Kill Switch/i })).toBeVisible()
    await page.getByRole('button', { name: 'Disable Kill Switch' }).last().click()

    await expect(page.getByText(/^Inactive$/).first()).toBeVisible({ timeout: 10_000 })
    await expect(page.getByText('Kill switch disabled. Auto-trading remains OFF until manually re-enabled.')).toBeVisible()
    await expect(page.getByText(/Auto Trading/i).locator('..').getByText(/Disabled/i)).toBeVisible()
    expect(forbiddenRecoveryCalls, `Recovery flow touched broker endpoints: ${forbiddenRecoveryCalls.join(', ')}`).toEqual([])
  })

  test('orders page supports safe paper order and kill-switch blocked demo journey', async ({ page, request }) => {
    const token = await adminToken(request)
    const headers = { Authorization: `Bearer ${token}` }
    await request.post(`${API_URL}/v1/risk/kill-switch/disable`, { headers })
    await request.patch(`${API_URL}/v1/risk/profile`, {
      headers,
      data: { max_open_positions: 50, max_trades_per_day: 200 },
    })

    const forbiddenPaperFlowCalls: string[] = []
    await page.route('**/*', async (route) => {
      const req = route.request()
      const url = req.url()
      const method = req.method()
      const pathname = new URL(url).pathname.replace(/^\/api/, '')

      if (
        method !== 'GET' &&
        (
          pathname === '/v1/orders' ||
          pathname.startsWith('/v1/broker/')
        )
      ) {
        forbiddenPaperFlowCalls.push(`${method} ${pathname}`)
        await route.abort('failed')
        return
      }

      await route.continue()
    })

    await page.goto('/auth/login')
    await page.evaluate((accessToken) => {
      localStorage.setItem('cg_token', accessToken)
    }, token)
    await page.goto('/app/orders')
    await expectTopbarTitle(page, 'Orders')
    await expect(page.getByText('Mock API')).toBeVisible()
    await expect(page.getByText('Connected')).toHaveCount(0)

    await expect(page.getByRole('heading', { name: 'Paper / Mock Order' })).toBeVisible()
    await expect(page.getByText('Mock mode only').first()).toBeVisible()
    await expect(page.getByText('No real broker order will be placed')).toBeVisible()
    await expect(page.getByText('No funds are moved')).toBeVisible()

    await page.getByLabel('Ticker').fill(`MOCK${Date.now().toString().slice(-5)}`)
    await page.getByLabel('Quantity').fill('1')
    await page.getByRole('button', { name: 'Submit Paper Order' }).click()

    await expect(page.getByText(/Paper order filled/i)).toBeVisible({ timeout: 10_000 })
    await expect(page.getByText(/No broker order sent/i).first()).toBeVisible()
    await expect(page.getByRole('heading', { name: 'Paper Order History' })).toBeVisible()
    await expect(page.getByText(/filled/i).first()).toBeVisible()

    await page.getByRole('button', { name: 'Enable Kill Switch' }).click()
    await expect(page.getByText(/Kill switch is active/i).first()).toBeVisible({ timeout: 10_000 })

    await page.getByLabel('Ticker').fill(`BLOCK${Date.now().toString().slice(-5)}`)
    await page.getByLabel('Quantity').fill('1')
    await page.getByRole('button', { name: 'Submit Paper Order' }).click()

    await expect(page.getByText(/Paper order blocked by safety controls/i)).toBeVisible({
      timeout: 10_000,
    })
    await expect(page.getByText(/No broker order was sent/i).first()).toBeVisible()
    expect(forbiddenPaperFlowCalls, `Paper flow touched broker/live order endpoints: ${forbiddenPaperFlowCalls.join(', ')}`).toEqual([])

    await request.post(`${API_URL}/v1/risk/kill-switch/disable`, { headers })
  })
})
