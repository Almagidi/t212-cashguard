import { describe, expect, test } from '@jest/globals'
import fs from 'node:fs'
import path from 'node:path'

import { assertE2EDependenciesReady } from '../e2e/global-setup'

const WEB_ROOT = path.resolve(__dirname, '..', '..')

describe('E2E dependency readiness', () => {
  test.each([
    [{ database: 'error', redis: 'ok', market_data: 'mock' }, 'database'],
    [{ database: 'ok', redis: 'error', market_data: 'mock' }, 'redis'],
    [{ database: 'ok', redis: 'ok', market_data: 'polygon' }, 'market_data'],
  ])('rejects an unhealthy required dependency: %s', (health, expectedField) => {
    expect(() => assertE2EDependenciesReady(health)).toThrow(expectedField)
  })

  test('accepts task-owned database, Redis, and mock market data readiness', () => {
    expect(() => assertE2EDependenciesReady({
      database: 'ok',
      redis: 'ok',
      market_data: 'mock',
    })).not.toThrow()
  })

  test('keeps acceptance at one worker with no Playwright or transport retries', () => {
    const config = fs.readFileSync(path.join(WEB_ROOT, 'playwright.config.ts'), 'utf8')
    const helpers = fs.readFileSync(path.join(WEB_ROOT, 'tests', 'e2e', 'helpers.ts'), 'utf8')

    expect(config).toMatch(/retries:\s*0/)
    expect(config).toMatch(/workers:\s*1/)
    expect(helpers).not.toMatch(/page\.request\.(?:fetch|get|post|put|patch|delete)\(/)
    expect(helpers).toContain('guardedApiFetch')
  })

  test('has no readiness or production rate-limit bypass in shipped acceptance targets', () => {
    const setup = fs.readFileSync(path.join(WEB_ROOT, 'tests', 'e2e', 'global-setup.ts'), 'utf8')
    const makefile = fs.readFileSync(path.resolve(WEB_ROOT, '..', '..', 'Makefile'), 'utf8')
    const validateE2E = makefile.slice(makefile.indexOf('validate-e2e:'), makefile.indexOf('# ── Trading 212 demo app'))

    expect(setup).not.toContain('E2E_MOCK_API')
    expect(makefile.slice(makefile.indexOf('e2e-operator:'), makefile.indexOf('logs:'))).not.toContain('E2E_MOCK_API')
    expect(validateE2E).not.toContain('DISABLE_RATE_LIMITING')
  })

  test('does not discover E2E credentials from an owner environment file', () => {
    const helpers = fs.readFileSync(path.join(WEB_ROOT, 'tests', 'e2e', 'helpers.ts'), 'utf8')

    expect(helpers).not.toContain("'.env'")
    expect(helpers).not.toContain('readEnvValue')
  })
})
