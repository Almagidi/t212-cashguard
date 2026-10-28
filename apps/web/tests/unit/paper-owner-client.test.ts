import { afterEach, expect, jest, test } from '@jest/globals'
import type { AxiosInstance } from 'axios'
import { api } from '@/services/api'
import { useAuthStore } from '@/stores/auth'
import type { User } from '@/types'
import { createPaperIntent, readPaperIntent } from '@/lib/paper-operation-intent'

const a = '00000000-0000-4000-8000-000000000001'
const b = '00000000-0000-4000-8000-000000000002'
const user = (id: string) => ({ id, email: 'synthetic@test.invalid', is_active: true, is_admin: true, created_at: '2026-10-28T00:00:00Z' }) as User
const input = { ticker: 'EXAMPLE', side: 'buy' as const, quantity: '1', estimated_price: '100', source: 'manual_demo_ui', venue: 'paper' as const, paper_only: true as const, order_type: 'market' as const }
const client = (api as unknown as { client: AxiosInstance }).client
const originalAdapter = client.defaults.adapter
afterEach(() => { client.defaults.adapter = originalAdapter; useAuthStore.getState().logout(); sessionStorage.clear(); jest.restoreAllMocks() })

test('logout/relogin and delayed retry cannot dispatch a saved different-owner intent', async () => {
  useAuthStore.getState().setUser(user(a))
  const saved = createPaperIntent(a, input, sessionStorage)
  useAuthStore.getState().logout()
  useAuthStore.getState().setUser(user(b))
  const transport = jest.fn(async () => { throw new Error('unexpected dispatch') })
  client.defaults.adapter = transport
  await expect(api.placePaperOrder({ ...input, operation_id: saved.operationId, expected_owner_id: a })).rejects.toThrow('owner changed')
  expect(transport).not.toHaveBeenCalled()
  expect(readPaperIntent(a, sessionStorage)?.state).toBe('unresolved')
  expect(readPaperIntent(b, sessionStorage)).toBeNull()
})

test('an owner change inside actual Axios dispatch cannot change the expected-owner precondition', async () => {
  useAuthStore.getState().setUser(user(a))
  api.setToken(crypto.randomUUID())
  const nextToken = crypto.randomUUID()
  let observedExpected: unknown
  let observedAuth: unknown
  let observedBody: unknown
  client.defaults.adapter = async config => {
    observedExpected = config.headers.get('X-Paper-Expected-Owner')
    observedAuth = config.headers.get('Authorization')
    observedBody = JSON.parse(config.data)
    return { config, status: 403, statusText: 'Forbidden', headers: {}, data: { detail: 'expected-owner mismatch' } }
  }
  const interceptor = client.interceptors.request.use(config => {
    useAuthStore.getState().setUser(user(b)); api.setToken(nextToken); return config
  })
  try {
    await api.placePaperOrder({ ...input, operation_id: crypto.randomUUID(), expected_owner_id: a })
    expect(observedExpected).toBe(a)
    expect(observedAuth).toBe(`Bearer ${nextToken}`)
    expect(observedBody).not.toHaveProperty('expected_owner_id')
  } finally { client.interceptors.request.eject(interceptor) }
})

test('receipt lookup preserves the saved expected owner across an Axios token/account switch', async () => {
  useAuthStore.getState().setUser(user(a))
  const nextToken = crypto.randomUUID()
  const seen: unknown[] = []
  client.defaults.adapter = async config => {
    seen.push(config.headers.get('X-Paper-Expected-Owner'), config.headers.get('Authorization'))
    return { config, status: 403, statusText: 'Forbidden', headers: {}, data: { detail: 'expected-owner mismatch' } }
  }
  const interceptor = client.interceptors.request.use(config => {
    useAuthStore.getState().setUser(user(b)); api.setToken(nextToken); return config
  })
  try {
    await api.getPaperOperation(crypto.randomUUID(), a)
    expect(seen).toEqual([a, `Bearer ${nextToken}`])
  } finally { client.interceptors.request.eject(interceptor) }
})
