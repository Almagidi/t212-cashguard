import { describe, expect, test } from '@jest/globals'
import fs from 'node:fs'
import path from 'node:path'

const WEB_ROOT = path.resolve(__dirname, '..', '..')
const GLOBALS_PATH = path.join(WEB_ROOT, 'styles', 'globals.css')
const TAILWIND_CONFIG_PATH = path.join(WEB_ROOT, 'tailwind.config.js')

describe('offline font configuration', () => {
  test('does not load fonts from an external origin', () => {
    const globals = fs.readFileSync(GLOBALS_PATH, 'utf8')

    expect(globals).not.toMatch(/(?:@import|url\()[^;\n]*(?:https?:)?\/\//i)
    expect(globals).not.toMatch(/fonts\.(?:googleapis|gstatic)\.com/i)
  })

  test('uses explicit local system font stacks', () => {
    const config = fs.readFileSync(TAILWIND_CONFIG_PATH, 'utf8')

    expect(config).not.toMatch(/\b(?:Inter|JetBrains Mono)\b/)
    expect(config).toContain("'system-ui'")
    expect(config).toContain("'ui-monospace'")
  })
})
