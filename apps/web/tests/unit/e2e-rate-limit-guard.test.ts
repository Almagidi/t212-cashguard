import { describe, expect, it } from '@jest/globals'
import { readdirSync, readFileSync } from 'node:fs'
import path from 'node:path'
import { assertNoUnexpectedRateLimit, guardedApiFetch } from '../e2e/rate-limit-guard'

describe('browser acceptance rate-limit guard', () => {
  it('fails closed when ordinary browser traffic receives HTTP 429', () => {
    expect(() => assertNoUnexpectedRateLimit(429, 'GET', '/v1/orders')).toThrow(
      'Unexpected HTTP 429 for GET /v1/orders',
    )
  })

  it('allows non-rate-limit responses through unchanged', () => {
    expect(() => assertNoUnexpectedRateLimit(200, 'GET', '/v1/orders')).not.toThrow()
    expect(() => assertNoUnexpectedRateLimit(401, 'POST', '/v1/orders')).not.toThrow()
  })

  it('fails closed when a direct API request receives HTTP 429', async () => {
    const request = {
      fetch: async () => ({ status: () => 429 }),
    }

    await expect(
      guardedApiFetch(request, 'http://127.0.0.1:8000/v1/settings', { method: 'GET' }),
    ).rejects.toThrow('Unexpected HTTP 429 for GET /v1/settings')
  })

  it('routes every default-suite spec through the guarded fixture', () => {
    const e2eDirectory = path.resolve(process.cwd(), 'tests/e2e')
    const defaultSpecs = readdirSync(e2eDirectory)
      .filter((name) => name.endsWith('.spec.ts') && name !== 'operator-integration.spec.ts')

    expect(defaultSpecs.length).toBeGreaterThan(0)
    for (const name of defaultSpecs) {
      const source = readFileSync(path.join(e2eDirectory, name), 'utf8')
      expect(source).toMatch(/from ['"]\.\/test['"]/)
      expect(source).not.toMatch(/from ['"]@playwright\/test['"]/)
    }
  })

  it('keeps the dedicated operator integration spec out of the default suite', () => {
    const defaultConfig = readFileSync(path.resolve(process.cwd(), 'playwright.config.ts'), 'utf8')
    const integrationConfig = readFileSync(
      path.resolve(process.cwd(), 'playwright.integration.config.ts'),
      'utf8',
    )

    expect(defaultConfig).toMatch(/testIgnore:\s*\['\*\*\/operator-integration\.spec\.ts'\]/)
    expect(integrationConfig).toMatch(/testMatch:\s*\['\*\*\/operator-integration\.spec\.ts'\]/)
  })

  it('restores any temporary risk-profile expansion from a snapshot in finally', () => {
    const releaseSpec = readFileSync(
      path.resolve(process.cwd(), 'tests/e2e/mock-paper-release.spec.ts'),
      'utf8',
    )

    expect(releaseSpec).toContain('const originalRiskProfileResponse = await guardedApiFetch')
    expect(releaseSpec).toContain('const originalRiskProfile = await originalRiskProfileResponse.json()')
    expect(releaseSpec).toMatch(/finally\s*\{/)
    expect(releaseSpec).toContain('max_open_positions: originalRiskProfile.max_open_positions')
    expect(releaseSpec).toContain('max_trades_per_day: originalRiskProfile.max_trades_per_day')
    expect(releaseSpec).toMatch(/verifiedRiskProfileResponse\s*=\s*await guardedApiFetch/)
    expect(releaseSpec).toContain('expect(verifiedRiskProfile.max_open_positions).toBe(')
    expect(releaseSpec).toContain('expect(verifiedRiskProfile.max_trades_per_day).toBe(')
    expect(releaseSpec).toContain('let riskProfileRestoreError: unknown')
    expect(releaseSpec).toContain('let killSwitchResetError: unknown')
  })

  it('waits for responsive layout settlement and rejects horizontal overflow before screenshots', () => {
    const offlineSpec = readFileSync(
      path.resolve(process.cwd(), 'tests/e2e/offline-rendering.spec.ts'),
      'utf8',
    )

    expect(offlineSpec).toContain('async function expectStableContainedLayout')
    expect(offlineSpec).toContain('main.getBoundingClientRect().left')
    expect(offlineSpec).toContain('aside.getBoundingClientRect().right')
    expect(offlineSpec).toContain('document.documentElement.scrollWidth')
    expect(offlineSpec).toContain('window.innerWidth')
    expect(offlineSpec).toMatch(/await expectStableContainedLayout\(page\)/g)
  })
})
