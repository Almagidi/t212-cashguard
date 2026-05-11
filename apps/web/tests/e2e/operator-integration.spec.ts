/**
 * T-OPS-009 — Operator dashboard real-backend integration E2E.
 *
 * Connects to a live FastAPI server (port 8001, APP_MODE=mock, SQLite DB)
 * seeded by apps/api/scripts/init_integration_db.py.  No route mocks are
 * used for API calls — all responses come from the real backend.
 *
 * Invariants verified:
 *   1. Real /v1/auth/login succeeds and returns a bearer token.
 *   2. The operator dashboard page loads backend-derived content.
 *   3. No POST / PATCH / PUT / DELETE requests reach operator or DCA paths.
 *   4. All four required read-only endpoints return HTTP 200 with auth.
 */
import { test, expect } from '@playwright/test'

const API_URL = (process.env.NEXT_PUBLIC_API_URL ?? 'http://127.0.0.1:8001').replace(/\/$/, '')
const ADMIN_EMAIL = process.env.E2E_ADMIN_EMAIL ?? process.env.ADMIN_EMAIL ?? 'admin@localhost'
const ADMIN_PASSWORD = process.env.E2E_ADMIN_PASSWORD ?? process.env.ADMIN_PASSWORD ?? 'change-me'

const REQUIRED_ENDPOINTS = [
  '/v1/operator/status',
  '/v1/kraken/dca/status',
  '/v1/kraken/dca/activity',
  '/v1/kraken/dca/configs',
] as const

// ─── helpers ──────────────────────────────────────────────────────────────────

async function realLogin(request: import('@playwright/test').APIRequestContext): Promise<string> {
  const res = await request.post(`${API_URL}/v1/auth/login`, {
    data: { email: ADMIN_EMAIL, password: ADMIN_PASSWORD },
  })
  expect(res.ok(), `Real login failed — status ${res.status()}: ${await res.text()}`).toBe(true)
  const { access_token } = await res.json() as { access_token: string }
  expect(access_token, 'access_token missing from login response').toBeTruthy()
  return access_token
}

// ─── test suite ───────────────────────────────────────────────────────────────

test.describe('Operator dashboard — real-backend integration (T-OPS-009)', () => {

  test('real /v1/auth/login succeeds and issues a bearer token', async ({ request }) => {
    const token = await realLogin(request)
    expect(token.split('.').length).toBe(3) // minimal JWT shape check
  })

  test('four required operator endpoints return 200 with auth', async ({ request }) => {
    const token = await realLogin(request)
    const headers = { Authorization: `Bearer ${token}` }

    for (const endpoint of REQUIRED_ENDPOINTS) {
      const res = await request.get(`${API_URL}${endpoint}`, { headers })
      expect(
        res.status(),
        `${endpoint} returned ${res.status()} — body: ${await res.text()}`,
      ).toBe(200)
    }
  })

  test('loads read-only operator dashboard and triggers no mutations', async ({ page, request }) => {
    const token = await realLogin(request)
    const mutations: string[] = []

    // Intercept browser-originated requests; block any non-GET to API paths.
    await page.route('**/*', async (route) => {
      const req = route.request()
      const url = req.url()
      const method = req.method()

      if (!url.includes('/v1/') && !url.includes('/api/v1/')) {
        await route.continue()
        return
      }

      if (method !== 'GET') {
        mutations.push(`${method} ${url}`)
        await route.abort('failed')
        return
      }

      await route.continue()
    })

    // Set the real token so the SPA treats the session as authenticated.
    await page.goto('/auth/login')
    await page.evaluate((t) => localStorage.setItem('cg_token', t), token)

    await page.goto('/app/operator')

    // The page heading is always rendered regardless of data shape.
    await expect(
      page.getByRole('heading', { name: 'Read-only Operator Dashboard' }),
    ).toBeVisible({ timeout: 20_000 })

    // DCA section — seeded with BTC/USD and ETH/USD configs.
    await expect(page.getByRole('heading', { name: 'DCA Summary' })).toBeVisible()
    await expect(page.getByText('BTC/USD').first()).toBeVisible()

    // Kraken section must be present.
    await expect(page.getByRole('heading', { name: 'Kraken Summary' })).toBeVisible()

    // Execution boundary is rendered from real backend status flags.
    await expect(page.getByTestId('operator-execution-boundary')).toBeVisible()
    await expect(page.getByTestId('operator-read-only-badge')).toContainText('Read-only endpoint')
    await expect(page.getByTestId('operator-no-broker-order-badge')).toContainText('No broker order sent')
    await expect(page.getByTestId('operator-execution-boundary')).toContainText('Creates orders')
    await expect(page.getByTestId('operator-execution-boundary')).toContainText('Calls brokers')
    await expect(page.getByTestId('operator-execution-boundary')).toContainText('Triggers schedulers')
    await expect(page.getByTestId('operator-execution-boundary')).toContainText('Runs strategies')

    // Safety flags section — endpoint_read_only is hardcoded true in the route.
    await expect(page.getByText('Endpoint read-only')).toBeVisible()

    // No mutation buttons should exist on a read-only page.
    await expect(
      page.getByRole('button', { name: /enable|disable|execute|trade|buy|sell/i }),
    ).toHaveCount(0)

    // Critical safety invariant: no mutations were attempted.
    expect(mutations, `Unexpected mutations attempted: ${mutations.join(', ')}`).toEqual([])
  })

})
