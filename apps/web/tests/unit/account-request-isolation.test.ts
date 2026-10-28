import { afterEach, expect, jest, test } from '@jest/globals'
import type { AxiosInstance } from 'axios'
import api from '@/services/api'
import { useAuthStore } from '@/stores/auth'
import type { User } from '@/types'

const user = (id: string): User => ({ id, email: 'synthetic@test.invalid', is_active: true, is_admin: true, created_at: '2026-10-28T00:00:00Z' })
const a = user('11111111-1111-4111-8111-111111111111')
const client = (api as unknown as { client: AxiosInstance }).client
const originalAdapter = client.defaults.adapter
const originalTransform = client.defaults.transformRequest
afterEach(() => { client.defaults.adapter = originalAdapter; client.defaults.transformRequest = originalTransform; useAuthStore.getState().logout() })

test('a session switch in request transformation is refused at the actual adapter boundary', async () => {
  api.setToken(crypto.randomUUID())
  useAuthStore.getState().setUser(a)
  const transport = jest.fn(async () => { throw new Error('unexpected transport') })
  client.defaults.adapter = transport
  client.defaults.transformRequest = [(data) => { api.setToken(crypto.randomUUID()); return data }]
  await expect(api.getBrokerStatus()).rejects.toMatchObject({ name: 'OwnerScopeChangedError' })
  expect(transport).not.toHaveBeenCalled()
})

test('a verified unchanged session uses its captured token at the adapter boundary', async () => {
  const token = crypto.randomUUID()
  api.setToken(token)
  useAuthStore.getState().setUser(a)
  let observed: unknown
  client.defaults.adapter = async config => {
    observed = config.headers.get('Authorization')
    return { config, status: 200, statusText: 'OK', headers: {}, data: null }
  }
  await expect(api.getBrokerStatus()).resolves.toBeNull()
  expect(observed).toBe(`Bearer ${token}`)
})

test('explicit same-owner login starts a new session even when the server repeats the identical JWT', async () => {
  const token = crypto.randomUUID()
  api.setToken(token)
  useAuthStore.getState().setUser(a)
  const revision = useAuthStore.getState().sessionRevision
  client.defaults.adapter = async config => ({ config, status: 200, statusText: 'OK', headers: {}, data: { access_token: token, token_type: 'bearer' } })
  await api.login({ email: 'synthetic@test.invalid', password: crypto.randomUUID() })
  expect(useAuthStore.getState().sessionRevision).toBe(revision + 1)
  expect(useAuthStore.getState().verifiedToken).toBeNull()
})

test('shared token replacement cannot dispatch an old-owner read before the storage event', async () => {
  api.setToken(crypto.randomUUID())
  useAuthStore.getState().setUser(a)
  localStorage.setItem('cg_token', crypto.randomUUID())
  const transport = jest.fn(async () => { throw new Error('unexpected transport') })
  client.defaults.adapter = transport
  await expect(api.getBrokerStatus()).rejects.toMatchObject({ name: 'OwnerScopeChangedError' })
  expect(transport).not.toHaveBeenCalled()
})


test('an already-sent original-account response is never exposed as a new-session observation', async () => {
  api.setToken(crypto.randomUUID())
  useAuthStore.getState().setUser(a)
  let release!: () => void
  let sent = false
  client.defaults.adapter = config => new Promise(resolve => {
    sent = true
    release = () => resolve({ config, status: 200, statusText: 'OK', headers: {}, data: { account: 'original owner' } })
  })
  const response = api.getBrokerStatus().catch(error => error)
  while (!sent) await Promise.resolve()
  api.setToken(crypto.randomUUID())
  useAuthStore.getState().setUser(user('22222222-2222-4222-8222-222222222222'))
  release()
  expect(await response).toMatchObject({ name: 'OwnerScopeChangedError' })
})
