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

async function probe(url: string, label: string, retries = 3): Promise<void> {
  for (let i = 0; i < retries; i++) {
    try {
      const res = await fetch(url, { signal: AbortSignal.timeout(4_000) })
      if (res.ok || res.status < 500) return
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
  if (process.env.E2E_MOCK_API !== '1') {
    await probe(`${API_URL}/v1/health/ready`, 'API server')
    const deps = await fetch(`${API_URL}/v1/health/deps`, { signal: AbortSignal.timeout(4_000) })
    if (deps.ok) {
      const body = await deps.json() as { market_data?: string }
      console.log(`Selected market data provider for E2E: ${body.market_data ?? 'unknown'}`)

      const isMockPaperRun = (process.env.NEXT_PUBLIC_APP_MODE ?? 'mock') === 'mock'
      const allowExternalMarketData = process.env.E2E_ALLOW_EXTERNAL_MARKET_DATA === '1'
      if (isMockPaperRun && !allowExternalMarketData && body.market_data !== 'mock') {
        throw new Error(
          `Mock/paper E2E requires MARKET_DATA_PROVIDER=mock; backend reported ${body.market_data ?? 'unknown'}.`
        )
      }
    } else {
      console.log(`Selected market data provider for E2E: ${selectedMarketDataProvider} (health/deps unavailable)`)
    }
  }
  await probe(`${WEB_URL}/auth/login`, 'Web server')
  console.log(`\n✅  Both servers reachable — starting E2E suite\n`)
}
