/**
 * Static safety audit — no unauthorized order-placement call sites in UI source.
 *
 * The API client tier (services/api.ts) pre-dates this guard and legitimately
 * defines placeOrder() for the backend's live order endpoint. Its dormant
 * react-query wrapper (usePlaceOrder in hooks/use-api.ts) was removed on
 * 2026-07-12. Until live trading is explicitly approved, no UI code (pages,
 * components, stores, lib, hooks) may mention the live placement method,
 * reintroduce a wrapper for it, or reach the placement endpoint directly.
 * The only permitted UI order mutation is the paper-only flow via
 * placePaperOrder() -> POST /orders/paper.
 */
import { describe, expect, test } from '@jest/globals'
import fs from 'node:fs'
import path from 'node:path'

const WEB_ROOT = path.resolve(__dirname, '..', '..')
const UI_SOURCE_DIRS = ['app', 'components', 'hooks', 'stores', 'lib']

// The API client (services/api.ts) is the only file allowed to mention the
// live order-placement method. It lives outside UI_SOURCE_DIRS, so every
// scanned file — including hooks/use-api.ts — must stay clean.
function collectSourceFiles(dir: string): string[] {
  if (!fs.existsSync(dir)) return []
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const fullPath = path.join(dir, entry.name)
    if (entry.isDirectory()) return collectSourceFiles(fullPath)
    return /\.(ts|tsx)$/.test(entry.name) && !/\.(test|spec)\.(ts|tsx)$/.test(entry.name)
      ? [fullPath]
      : []
  })
}

const uiFiles = UI_SOURCE_DIRS.flatMap((dir) => collectSourceFiles(path.join(WEB_ROOT, dir)))

function offendersMatching(pattern: RegExp): string[] {
  return uiFiles.filter((file) => pattern.test(fs.readFileSync(file, 'utf8')))
}

describe('no order-placement call sites in UI source', () => {
  test('scans a meaningful set of UI source files', () => {
    expect(uiFiles.length).toBeGreaterThan(20)
  })

  test('the live order-placement client method has no UI call sites', () => {
    // Matches api.placeOrder / placeOrder( — placePaperOrder is a distinct
    // identifier and does not match.
    const offenders = offendersMatching(/\bplaceOrder\b/)
    expect(offenders).toEqual([])
  })

  test('the removed usePlaceOrder hook is not reintroduced in UI source', () => {
    const offenders = offendersMatching(/\busePlaceOrder\b/)
    expect(offenders).toEqual([])
  })

  test('no UI source addresses the order endpoints directly', () => {
    // All backend access must go through services/api.ts. An endpoint string
    // like '/orders' or '/v1/orders' appearing in UI source means someone
    // wired a direct fetch/axios call around the client. Route hrefs
    // ('/app/orders') and query keys ('orders') do not match this pattern.
    const offenders = offendersMatching(/['"`]\/(?:v1\/)?orders(?:\/|['"`])/)
    expect(offenders).toEqual([])
  })

  test('the paper order client method targets only the paper endpoint', () => {
    const apiSource = fs.readFileSync(path.join(WEB_ROOT, 'services', 'api.ts'), 'utf8')
    const paperMethod = apiSource.match(/async placePaperOrder[\s\S]{0,300}?\n  \}/)?.[0]
    expect(paperMethod).toBeDefined()
    expect(paperMethod).toContain('"/orders/paper"')
    expect(paperMethod).not.toMatch(/["'`]\/orders["'`]/)
  })

  test('the paper order UI flow submits with paper_only=true', () => {
    const ordersPage = fs.readFileSync(
      path.join(WEB_ROOT, 'app', 'app', 'orders', 'page.tsx'),
      'utf8',
    )
    expect(ordersPage).toContain('paper_only: true')
    expect(ordersPage).toContain('usePlacePaperOrder')
  })
})
