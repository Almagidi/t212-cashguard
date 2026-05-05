/**
 * Playwright global setup for the T-OPS-009 real-backend integration suite.
 *
 * Probes both the FastAPI server (port 8001) and the Next.js dev server
 * (port 3001) before any test runs.  Unlike the mock-mode global-setup,
 * this always checks the real API — there is no E2E_MOCK_API bypass.
 */
import { type FullConfig } from '@playwright/test'

const API_URL = (process.env.NEXT_PUBLIC_API_URL ?? 'http://127.0.0.1:8001').replace(/\/$/, '')
const WEB_URL = (process.env.BASE_URL ?? 'http://localhost:3001').replace(/\/$/, '')

async function probe(url: string, label: string, retries = 12, delayMs = 2_500): Promise<void> {
  for (let i = 0; i < retries; i++) {
    try {
      const res = await fetch(url, { signal: AbortSignal.timeout(5_000) })
      if (res.ok || res.status < 500) return
    } catch {
      if (i < retries - 1) await new Promise(r => setTimeout(r, delayMs))
    }
  }
  throw new Error(
    `\n\n❌  ${label} not reachable at ${url}\n` +
    `   Make sure the integration servers are running:\n` +
    `   API:  cd apps/api && uvicorn app.main:app --port 8001\n` +
    `   Web:  cd apps/web && NEXT_PUBLIC_API_URL=http://127.0.0.1:8001 npx next dev -p 3001\n`,
  )
}

export default async function globalSetup(_config: FullConfig) {
  await probe(`${API_URL}/v1/health/live`, 'Integration API server')
  await probe(`${WEB_URL}/auth/login`, 'Integration web server')
  console.log(`\n✅  Integration servers reachable — starting T-OPS-009 E2E suite\n`)
}
