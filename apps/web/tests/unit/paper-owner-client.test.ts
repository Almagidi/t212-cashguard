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

test('an owner change inside Axios cannot dispatch a saved intent under a replacement token', async () => {
  api.setToken(crypto.randomUUID())
  useAuthStore.getState().setUser(user(a))
  const nextToken = crypto.randomUUID()
  const transport = jest.fn(async () => { throw new Error('unexpected dispatch') })
  client.defaults.adapter = transport
  const interceptor = client.interceptors.request.use(config => {
    useAuthStore.getState().setUser(user(b)); api.setToken(nextToken); return config
  })
  try {
    await expect(api.placePaperOrder({ ...input, operation_id: crypto.randomUUID(), expected_owner_id: a })).rejects.toMatchObject({ name: 'OwnerScopeChangedError' })
    expect(transport).not.toHaveBeenCalled()
  } finally { client.interceptors.request.eject(interceptor) }
})

test('receipt lookup refuses an Axios token/account switch without disclosing either owner receipt', async () => {
  api.setToken(crypto.randomUUID())
  useAuthStore.getState().setUser(user(a))
  const transport = jest.fn(async () => { throw new Error('unexpected dispatch') })
  client.defaults.adapter = transport
  const interceptor = client.interceptors.request.use(config => {
    useAuthStore.getState().setUser(user(b)); api.setToken(crypto.randomUUID()); return config
  })
  try {
    await expect(api.getPaperOperation(crypto.randomUUID(), a)).rejects.toMatchObject({ name: 'OwnerScopeChangedError' })
    expect(transport).not.toHaveBeenCalled()
  } finally { client.interceptors.request.eject(interceptor) }
})

 test('normal owner-bound receipt lookup retains its expected-owner header', async () => {
  api.setToken(crypto.randomUUID())
  useAuthStore.getState().setUser(user(a))
  const seen: unknown[] = []
  client.defaults.adapter = async config => {
    seen.push(config.headers.get('X-Paper-Expected-Owner'))
    return { config, status: 404, statusText: 'Not found', headers: {}, data: { detail: 'not found' } }
  }
  await api.getPaperOperation(crypto.randomUUID(), a)
  expect(seen).toEqual([a])
})
