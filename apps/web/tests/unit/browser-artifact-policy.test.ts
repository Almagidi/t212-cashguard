import { describe, expect, it } from '@jest/globals'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { runInNewContext } from 'node:vm'
import { transformSync } from 'next/dist/build/swc'

describe('supported browser artifact confidentiality', () => {
  it.each(['playwright.config.ts', 'playwright.integration.config.ts'])('%s does not retain authentication-bearing protocol traces', name => {
    const code = transformSync(readFileSync(resolve(__dirname, '../../', name), 'utf8'), { jsc: { parser: { syntax: 'typescript' } }, module: { type: 'commonjs' } }).code
    type CaptureOptions = { trace?: string; recordHar?: unknown; contextOptions?: { recordHar?: unknown } }
    const compiledModule = { exports: {} as { default: { use: CaptureOptions; retries: number; projects: Array<{ use?: CaptureOptions }> } } }
    runInNewContext(code, { module: compiledModule, exports: compiledModule.exports, process: { env: {} }, require: () => ({ defineConfig: (value: unknown) => value, devices: { 'Desktop Chrome': {} } }) })
    expect(compiledModule.exports.default.use.trace).toBe('off')
    expect(compiledModule.exports.default.use.recordHar).toBeUndefined()
    expect(compiledModule.exports.default.use.contextOptions?.recordHar).toBeUndefined()
    expect(compiledModule.exports.default.retries).toBe(0)
    for (const project of compiledModule.exports.default.projects) {
      expect(project.use?.trace ?? compiledModule.exports.default.use.trace).toBe('off')
      expect(project.use?.recordHar).toBeUndefined()
      expect(project.use?.contextOptions?.recordHar).toBeUndefined()
    }
  })
})
