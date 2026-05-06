import { defineConfig, devices } from '@playwright/test'

const apiPort = process.env.INTEGRATION_API_PORT ?? '8001'
const webPort = process.env.INTEGRATION_WEB_PORT ?? '3001'
const apiUrl = `http://127.0.0.1:${apiPort}`
const webUrl = `http://localhost:${webPort}`
const webReadyUrl = `${webUrl}/auth/login`

export default defineConfig({
  testDir: './tests/e2e',
  testMatch: ['**/operator-integration.spec.ts'],
  globalSetup: './tests/e2e/integration-global-setup.ts',
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: 1,
  reporter: [['html', { open: 'never' }], ['list']],
  use: {
    baseURL: webUrl,
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    actionTimeout: 20_000,
    navigationTimeout: 30_000,
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
  webServer: {
    command: `npx next dev -p ${webPort}`,
    url: webReadyUrl,
    reuseExistingServer: true,
    timeout: 120_000,
    env: {
      NEXT_PUBLIC_API_URL: apiUrl,
      NEXT_PUBLIC_APP_MODE: 'mock',
    },
  },
})
