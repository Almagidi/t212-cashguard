import { test, expect, type Page } from '@playwright/test'
import { adminEmail, adminPassword, ensureAppPage, ensureLoggedIn, expectTopbarTitle, installApiProxy, installAuthMeStub, installExternalMarketDataGuard } from './helpers'

const API_URL = (process.env.NEXT_PUBLIC_API_URL ?? 'http://127.0.0.1:8000').replace(/\/$/, '')
let cachedAdminToken: string | null = null

async function adminToken(request: import('@playwright/test').APIRequestContext): Promise<string> {
  if (cachedAdminToken) return cachedAdminToken

  const res = await request.post(`${API_URL}/v1/auth/login`, {
    data: { email: adminEmail, password: adminPassword },
  })
  expect(res.ok(), `login failed with status ${res.status()}: ${await res.text()}`).toBe(true)
  const body = await res.json() as { access_token?: string }
  expect(body.access_token).toBeTruthy()
  cachedAdminToken = body.access_token!
  return cachedAdminToken
}


async function resetKillSwitch(page: Page) {
  const token = await adminToken(page.request)
  const res = await page.request.post(`${API_URL}/v1/risk/kill-switch/disable`, {
    headers: { Authorization: `Bearer ${token}` },
  })
  expect(res.ok(), `kill-switch reset failed with status ${res.status()}: ${await res.text()}`).toBe(true)
}
test.describe('Mock/Paper Release Candidate Smoke', () => {
  test.skip(
    (process.env.NEXT_PUBLIC_APP_MODE ?? 'mock') !== 'mock',
    'Mock/paper release journey is mock-mode only.',
  )

  test.afterEach(async ({ page }) => {
    await page.unrouteAll({ behavior: 'ignoreErrors' })
  })

  test('backend is pinned to mock market data for mock/paper smoke', async ({ request }) => {
    const res = await request.get(`${API_URL}/v1/health/deps`)
    expect(res.ok(), `health deps failed with status ${res.status()}: ${await res.text()}`).toBe(true)
    const body = await res.json() as { market_data?: string }
    expect(body.market_data).toBe('mock')
  })

  test('operator page shows safe mock/paper runtime state', async ({ page }) => {
    await installApiProxy(page)
    await ensureAppPage(page, '/app/operator', 'Operator')
    await expectTopbarTitle(page, 'Operator')

    // The release candidate must not show the old Docker build/runtime mismatch warning.
    await expect(
      page.getByText(/Frontend mode is demo but backend mode is mock/i),
    ).toHaveCount(0)

    // Mock/paper safety wording should be visible.
    await expect(page.getByText(/Paper|Mock/i).first()).toBeVisible({
      timeout: 10_000,
    })

    // Stable runtime panels should be visible.
    await expect(page.getByTestId('mock-runtime-status')).toBeVisible()
    await expect(page.getByTestId('operator-execution-boundary')).toBeVisible()
    await expect(page.getByTestId('operator-read-only-badge')).toContainText('Read-only endpoint')
    await expect(page.getByTestId('operator-no-broker-order-badge')).toContainText('No broker order sent')
  })

  test('core mock lab routes render without crashing', async ({ page }) => {
    const externalMarketDataHits: string[] = []
    await installExternalMarketDataGuard(page, externalMarketDataHits)

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

    await ensureLoggedIn(page)
    for (const route of routes) {
      await page.goto(route.path)
      await expectTopbarTitle(page, route.title)
    }

    expect(externalMarketDataHits, `Browser attempted external market-data calls: ${externalMarketDataHits.join(', ')}`).toEqual([])
  })

  test('strategies page no longer uses ambiguous dry-run badge wording', async ({ page }) => {
    await ensureAppPage(page, '/app/strategies', 'Strategies')
    await expectTopbarTitle(page, 'Strategies')

    await expect(page.getByText(/DRY RUN/i)).toHaveCount(0)
  })

  test('emergency page keeps destructive controls behind confirmation', async ({ page }) => {
    await resetKillSwitch(page)
    await ensureAppPage(page, '/app/emergency', 'Emergency Controls')
    await expectTopbarTitle(page, 'Emergency Controls')

    // Verify the emergency action buttons are present
    await expect(page.getByTestId('emergency-action-disable-auto-trading')).toBeVisible()
    await expect(page.getByTestId('emergency-action-cancel-orders')).toBeVisible()
    await expect(page.getByTestId('emergency-action-flatten-positions')).toBeVisible()

    // Click the activate kill switch button to verify confirmation is required
    await page.getByTestId('activate-kill-switch-button').click()

    // Verify confirmation dialog appears
    await expect(page.getByRole('heading', { name: /Activate Kill Switch/i })).toBeVisible({
      timeout: 5_000,
    })
    await expect(page.getByTestId('confirm-activate-kill-switch-button')).toBeVisible()
  })

  test('emergency page supports explicit kill-switch recovery without resuming auto-trading', async ({ page }) => {
    const token = await adminToken(page.request)
    const headers = { Authorization: `Bearer ${token}` }
    const reset = await page.request.post(`${API_URL}/v1/risk/kill-switch/disable`, { headers })
    expect(reset.ok(), `kill-switch reset failed with status ${reset.status()}: ${await reset.text()}`).toBe(true)

    const forbiddenRecoveryCalls: string[] = []
    await installApiProxy(page, {
      shouldBlockRequest: ({ method, pathname }) => method !== 'GET' && pathname.startsWith('/v1/broker/'),
      onBlockedRequest: ({ method, pathname }) => {
        forbiddenRecoveryCalls.push(`${method} ${pathname}`)
      },
    })

    await installAuthMeStub(page)
    await page.goto('/auth/login')
    await page.evaluate((accessToken) => {
      localStorage.setItem('cg_token', accessToken)
    }, token)
    await page.goto('/app/emergency')
    await expectTopbarTitle(page, 'Emergency Controls')

    // Verify kill switch is initially inactive
    await expect(page.getByTestId('kill-switch-status').getByText(/Inactive/i)).toBeVisible({ timeout: 10_000 })
    
    // Activate kill switch
    await page.getByTestId('activate-kill-switch-button').click()
    await expect(page.getByRole('heading', { name: /Activate Kill Switch/i })).toBeVisible()
    await page.getByTestId('confirm-activate-kill-switch-button').click()

    // Verify kill switch is now active
    await expect(page.getByTestId('kill-switch-status').getByText(/ACTIVE/i)).toBeVisible({ timeout: 10_000 })
    
    // Disable kill switch
    await page.getByTestId('disable-kill-switch-button').click()
    await expect(page.getByRole('heading', { name: /Disable Kill Switch/i })).toBeVisible()
    await page.getByTestId('confirm-disable-kill-switch-button').click()

    // Verify kill switch is now inactive
    await expect(page.getByTestId('kill-switch-status').getByText(/Inactive/i)).toBeVisible({ timeout: 10_000 })
    
    // Verify recovery message and auto-trading remains disabled
    await expect(
      page.getByTestId('kill-switch-recovery-message').getByText('Kill switch disabled. Auto-trading remains OFF until manually re-enabled.')
    ).toBeVisible()
    await expect(page.getByTestId('auto-trading-status').getByText(/Disabled/i)).toBeVisible()
    expect(forbiddenRecoveryCalls, `Recovery flow touched broker endpoints: ${forbiddenRecoveryCalls.join(', ')}`).toEqual([])
  })

  test('orders page supports safe paper order and kill-switch blocked demo journey', async ({ page }) => {
    const token = await adminToken(page.request)
    const headers = { Authorization: `Bearer ${token}` }
    await page.request.post(`${API_URL}/v1/risk/kill-switch/disable`, { headers })
    await page.request.patch(`${API_URL}/v1/risk/profile`, {
      headers,
      data: { max_open_positions: 50, max_trades_per_day: 200 },
    })

    const forbiddenPaperFlowCalls: string[] = []
    await installApiProxy(page, {
      shouldBlockRequest: ({ method, pathname }) => (
        method !== 'GET' &&
        (
          pathname === '/v1/orders' ||
          pathname.startsWith('/v1/broker/')
        )
      ),
      onBlockedRequest: ({ method, pathname }) => {
        forbiddenPaperFlowCalls.push(`${method} ${pathname}`)
      },
    })

    await page.goto('/auth/login')
    await page.evaluate((accessToken) => {
      localStorage.setItem('cg_token', accessToken)
    }, token)
    await resetKillSwitch(page)
    await page.goto('/app/orders')
    await expectTopbarTitle(page, 'Orders')

    // Verify paper order panel is visible with safety messages
    await expect(page.getByTestId('paper-order-panel')).toBeVisible()
    await expect(page.getByTestId('broker-execution-status')).toContainText(/disabled/i)
    await expect(page.getByText('Paper / Mock Order').first()).toBeVisible()
    await expect(page.getByText('Mock mode only').first()).toBeVisible()
    await expect(page.getByText('No real broker order will be placed')).toBeVisible()
    await expect(page.getByText('No funds are moved')).toBeVisible()

    // Submit a paper order
    await page.getByLabel('Ticker').fill(`MOCK${Date.now().toString().slice(-5)}`)
    await page.getByLabel('Quantity').fill('1')
    await page.getByTestId('paper-order-submit-button').click()

    // Verify paper order was successful
    await expect(page.getByTestId('paper-order-status-message')).toBeVisible({ timeout: 10_000 })
    await expect(page.getByTestId('paper-order-status-message').getByText(/filled|paper order/i)).toBeVisible()
    await expect(page.getByTestId('paper-order-history')).toBeVisible()
    await expect(page.getByTestId('paper-order-history')).not.toBeEmpty({ timeout: 10_000 })
    await expect(page.getByTestId('paper-order-history').getByText(/No broker order sent/i).first()).toBeVisible({
      timeout: 10_000,
    })

    // Enable kill switch
    await page.getByTestId('enable-kill-switch-button').click()
    await expect(page.getByTestId('paper-kill-switch-active-badge')).toBeVisible({ timeout: 10_000 })

    // Attempt to submit another paper order (should be blocked)
    await page.getByLabel('Ticker').fill(`BLOCK${Date.now().toString().slice(-5)}`)
    await page.getByLabel('Quantity').fill('1')
    await page.getByTestId('paper-order-submit-button').click()

    // Verify paper order was blocked
    await expect(page.getByTestId('paper-order-status-message').getByText(/Paper order blocked by safety controls/i)).toBeVisible({
      timeout: 10_000,
    })
    await expect(page.getByTestId('order-blocked-reason').first()).toContainText(/Kill switch|No broker order/i)
    await expect(page.getByTestId('paper-order-status-message').getByText(/No broker order was sent/i)).toBeVisible()
    expect(forbiddenPaperFlowCalls, `Paper flow touched broker/live order endpoints: ${forbiddenPaperFlowCalls.join(', ')}`).toEqual([])

    await page.request.post(`${API_URL}/v1/risk/kill-switch/disable`, { headers })
  })
})
