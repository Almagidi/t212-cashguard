/**
 * Playwright global setup — verifies both API and web servers are reachable
 * before any test runs, giving a single clear error instead of 16+ cryptic
 * "stuck on login page" failures.
 */
import { chromium, type FullConfig } from '@playwright/test'

const API_URL = process.env.NEXT_PUBLIC_API_URL?.replace(/\/$/, '') ?? 'http://127.0.0.1:8000'
const WEB_URL = process.env.BASE_URL?.replace(/\/$/, '') ?? 'http://localhost:3000'
const selectedMarketDataProvider = process.env.MARKET_DATA_PROVIDER ?? 'mock'
process.env.MARKET_DATA_PROVIDER = selectedMarketDataProvider

type DependencyHealth = {
  database?: string
  redis?: string
  market_data?: string
}

export function assertE2EDependenciesReady(health: DependencyHealth): void {
  const required: Array<keyof DependencyHealth> = ['database', 'redis', 'market_data']
  const expected: DependencyHealth = { database: 'ok', redis: 'ok', market_data: 'mock' }
  const failures = required.filter((name) => health[name] !== expected[name])

  if (failures.length > 0) {
    const details = failures.map((name) => `${name}=${health[name] ?? 'missing'}`).join(', ')
    throw new Error(`E2E dependency readiness failed: ${details}`)
  }
}

async function probe(url: string, label: string, retries = 3): Promise<void> {
  for (let i = 0; i < retries; i++) {
    try {
      const res = await fetch(url, { signal: AbortSignal.timeout(4_000) })
      if (res.ok) return
    } catch {
      if (i < retries - 1) await new Promise(r => setTimeout(r, 1_000))
    }
  }
  throw new Error(
    `\n\n❌  ${label} not reachable at ${url}\n` +
    `   Make sure it is running before executing the E2E suite.\n` +
    `   API:  cd apps/api && uvicorn app.main:app --port 8000\n` +
    `   Web:  cd apps/web && npm run dev\n`
  )
}

export default async function globalSetup(_config: FullConfig) {
  await probe(`${API_URL}/v1/health/ready`, 'API server')
  const deps = await fetch(`${API_URL}/v1/health/deps`, { signal: AbortSignal.timeout(4_000) })
  if (!deps.ok) {
    throw new Error(`API dependency readiness returned HTTP ${deps.status}`)
  }
  const body = await deps.json() as DependencyHealth
  assertE2EDependenciesReady(body)
  console.log(`Selected market data provider for E2E: ${body.market_data}`)
  await probe(`${WEB_URL}/auth/login`, 'Web server')
  console.log(`\n✅  Both servers reachable — starting E2E suite\n`)
}
