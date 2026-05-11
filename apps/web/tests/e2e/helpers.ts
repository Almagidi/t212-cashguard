import fs from 'node:fs'
import path from 'node:path'
import { expect, type Page } from '@playwright/test'

function readEnvValue(name: string): string | undefined {
  const envPath = path.resolve(process.cwd(), '..', '..', '.env')

  if (!fs.existsSync(envPath)) return undefined

  const content = fs.readFileSync(envPath, 'utf8')
  const match = content.match(new RegExp(`^${name}=(.*)$`, 'm'))
  if (!match) return undefined

  return match[1]?.trim().replace(/^['"]|['"]$/g, '')
}

export const adminEmail = process.env.E2E_ADMIN_EMAIL ?? readEnvValue('ADMIN_EMAIL') ?? 'admin@localhost'
export const adminPassword = process.env.E2E_ADMIN_PASSWORD ?? readEnvValue('ADMIN_PASSWORD') ?? 'change-me'

const apiUrl = (process.env.NEXT_PUBLIC_API_URL ?? 'http://127.0.0.1:8000').replace(/\/$/, '')
const testUser = {
  id: '00000000-0000-0000-0000-000000000001',
  email: adminEmail,
  is_active: true,
  is_admin: true,
  created_at: '2026-01-01T00:00:00Z',
}
let cachedToken: string | undefined

export async function loginThroughUi(page: Page, email = adminEmail, password = adminPassword) {
  await page.goto('/auth/login')
  await page.waitForSelector('input[name="email"]')
  await page.fill('input[name="email"]', email)
  await page.fill('input[type="password"]', password)
  await page.click('button[type="submit"]')
  await page.waitForURL('**/app/**', { timeout: 10_000 })
  await expect(page.locator('header h1')).toBeVisible({ timeout: 10_000 })
}

export async function clearClientAuth(page: Page) {
  await page.goto('/auth/login')
  await page.context().clearCookies()
  await page.evaluate(() => {
    window.localStorage.clear()
    window.sessionStorage.clear()
  })
}

export async function expectTopbarTitle(page: Page, title: string) {
  await expect(page.locator('header h1').filter({ hasText: title })).toBeVisible({ timeout: 10_000 })
}

export async function expectMainHeading(page: Page, title: string) {
  await expect(page.locator('main h2').filter({ hasText: title })).toBeVisible({ timeout: 10_000 })
}

export async function installAuthMeStub(page: Page) {
  await page.route('**/v1/auth/me', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(testUser),
    })
  })
}

export async function installApiProxy(
  page: Page,
  options: {
    onBlockedRequest?: (request: { method: string; pathname: string }) => void
    shouldBlockRequest?: (request: { method: string; pathname: string }) => boolean
  } = {},
) {
  await page.route(`${apiUrl}/**`, async (route) => {
    const request = route.request()
    const method = request.method()
    const url = request.url()
    const pathname = new URL(url).pathname.replace(/^\/api/, '')
    const origin = request.headers().origin ?? page.url().match(/^https?:\/\/[^/]+/)?.[0] ?? '*'

    if (method === 'OPTIONS') {
      await route.fulfill({
        status: 204,
        headers: {
          'access-control-allow-origin': origin,
          'access-control-allow-credentials': 'true',
          'access-control-allow-methods': 'GET, POST, PUT, PATCH, DELETE, OPTIONS',
          'access-control-allow-headers': 'authorization, content-type',
        },
        body: '',
      })
      return
    }

    if (options.shouldBlockRequest?.({ method, pathname })) {
      options.onBlockedRequest?.({ method, pathname })
      await route.abort('failed')
      return
    }

    let response
    try {
      response = await page.request.fetch(request)
    } catch (error) {
      await page.waitForTimeout(250)
      response = await page.request.fetch(request).catch((retryError) => {
        throw new Error(
          `API proxy failed for ${method} ${pathname}: ${String(retryError)}; first error: ${String(error)}`,
        )
      })
    }
    const headers = response.headers()
    await route.fulfill({
      response,
      headers: {
        ...headers,
        'access-control-allow-origin': origin,
        'access-control-allow-credentials': 'true',
      },
    })
  })
}

export async function installExternalMarketDataGuard(page: Page, hits: string[]) {
  const blockedHosts = [
    'api.polygon.io',
    'data.alpaca.markets',
    'api.alpaca.markets',
    'paper-api.alpaca.markets',
  ]

  await page.route('**/*', async (route) => {
    const url = new URL(route.request().url())
    if (blockedHosts.some((host) => url.hostname === host || url.hostname.endsWith(`.${host}`))) {
      hits.push(route.request().url())
      await route.abort('blockedbyclient')
      return
    }

    await route.fallback()
  })
}

export async function ensureLoggedIn(page: Page) {
  await installAuthMeStub(page)
  await page.goto('/auth/login')

  if (!cachedToken) {
    const response = await page.request.post(`${apiUrl}/v1/auth/login`, {
      data: { email: adminEmail, password: adminPassword },
    })
    expect(response.ok(), `login bootstrap failed with status ${response.status()}: ${await response.text()}`).toBe(true)
    const data = await response.json() as { access_token?: string }
    cachedToken = data.access_token
    expect(cachedToken).toBeTruthy()
  }

  await page.evaluate((accessToken) => {
    localStorage.setItem('cg_token', accessToken)
  }, cachedToken!)

  await page.goto('/app/dashboard')
  await expectTopbarTitle(page, 'Dashboard')
}

export async function ensureAppPage(page: Page, path: string, title: string) {
  await ensureLoggedIn(page)
  await page.goto(path)
  await expectTopbarTitle(page, title)
}

export async function expectToast(page: Page, text: string | RegExp) {
  await expect(page.locator('[role="status"]').filter({ hasText: text }).first()).toBeVisible({ timeout: 10_000 })
}

export async function expectAnyVisible(page: Page, labels: Array<string | RegExp>) {
  const timeoutAt = Date.now() + 10_000

  while (Date.now() < timeoutAt) {
    for (const label of labels) {
      const locator = page.getByText(label)
      if (await locator.first().isVisible().catch(() => false)) {
        return
      }
    }

    await page.waitForTimeout(250)
  }

  throw new Error(`None of the expected labels were visible: ${labels.map(String).join(', ')}`)
}
