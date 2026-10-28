import { describe, expect, it } from '@jest/globals'
import { randomBytes } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { runInNewContext } from 'node:vm'
import { transformSync } from 'next/dist/build/swc'

type Response = { ok: () => boolean; status: () => number; text: () => Promise<string>; json: () => Promise<{ access_token: string }> }

function actualLogin(messages: string[]) {
  const source = readFileSync(resolve(__dirname, '../e2e/operator-integration.spec.ts'), 'utf8')
  const compiled = transformSync(source, { jsc: { parser: { syntax: 'typescript' }, target: 'es2022' }, module: { type: 'commonjs' } }).code
  const compiledModule = { exports: {} as Record<string, unknown> }
  runInNewContext(`${compiled}\nmodule.exports.realLogin = realLogin`, {
    module: compiledModule, exports: compiledModule.exports, process: { env: {} },
    require: (name: string) => {
      if (name !== '@playwright/test') throw new Error('Unexpected dependency in login probe')
      return {
        test: { describe: () => undefined },
        expect: (value: unknown, message = '') => {
          messages.push(message)
          return {
            toBe: (expected: unknown) => { if (value !== expected) throw new Error('Login assertion refused') },
            toBeTruthy: () => { if (!value) throw new Error('Token missing') },
          }
        },
      }
    },
  }, { timeout: 1000 })
  return compiledModule.exports.realLogin as (request: { post: () => Promise<Response> }) => Promise<string>
}

describe('actual operator login diagnostic privacy', () => {
  it.each([200, 401])('does not copy an HTTP %s authentication body into report step titles', async status => {
    const messages: string[] = []
    const login = actualLogin(messages)
    const token = randomBytes(32).toString('base64url')
    let bodyReads = 0
    const request = { post: async () => ({
      ok: () => status === 200, status: () => status,
      text: async () => { bodyReads++; return JSON.stringify({ access_token: token }) },
      json: async () => ({ access_token: token }),
    }) }
    if (status === 200) {
      const result = await login(request)
      expect(result === token).toBe(true)
    } else {
      await expect(login(request)).rejects.toThrow('Login assertion refused')
    }
    expect(bodyReads).toBe(0)
    const secretInStepTitle = messages.some(message => message.includes(token))
    expect(secretInStepTitle).toBe(false)
    expect(messages.some(message => message.includes(String(status)))).toBe(true)
  })
})
