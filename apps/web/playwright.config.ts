import { defineConfig, devices } from '@playwright/test'

const webPort = process.env.E2E_WEB_PORT ?? '3000'
const webUrl = process.env.BASE_URL || `http://localhost:${webPort}`
const webReadyUrl = `${webUrl.replace(/\/$/, '')}/auth/login`
process.env.MARKET_DATA_PROVIDER ??= 'mock'

export default defineConfig({
  testDir: './tests/e2e',
  globalSetup: './tests/e2e/global-setup.ts',
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? undefined : 1,
  reporter: [['html', { open: 'never' }], ['list']],
  use: {
    baseURL: webUrl,
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
    // Give each action/assertion a generous timeout for slower CI runners
    actionTimeout: 15_000,
    navigationTimeout: 20_000,
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
  // Start the dev server automatically only when running locally
  // with no CI flag and no externally provided BASE_URL.
  webServer: process.env.CI || process.env.BASE_URL ? undefined : {
    command: `npx next dev -p ${webPort}`,
    url: webReadyUrl,
    reuseExistingServer: false,
    timeout: 120_000,
  },
})
