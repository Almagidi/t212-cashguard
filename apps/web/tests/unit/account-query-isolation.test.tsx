import { afterEach, expect, jest, test } from '@jest/globals'
import { StrictMode } from 'react'
import api from '@/services/api'
import { act, render, screen, waitFor } from '@testing-library/react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useAuthStore } from '@/stores/auth'
import type { User } from '@/types'

jest.mock('next-themes', () => ({ ThemeProvider: ({ children }: { children: React.ReactNode }) => children }))
jest.mock('react-hot-toast', () => ({ __esModule: true, default: { remove: jest.fn() }, Toaster: () => null }))
const { Providers } = require('@/app/providers')
const user = (id: string): User => ({ id, email: `${id}@example.invalid`, is_active: true, is_admin: true, created_at: '2026-10-28T00:00:00Z' })
const a = user('11111111-1111-4111-8111-111111111111')
const b = user('22222222-2222-4222-8222-222222222222')
afterEach(() => { act(() => useAuthStore.getState().logout()) })

test('principal transitions never render prior-owner cached readiness, broker, operator or user records', async () => {
  const observed: { owner: string; values: (string | undefined)[] }[] = []
  const keys = [['settings', 'live-readiness'], ['broker', 'status'], ['operator', 'status'], ['me']]
  const request = jest.fn(async () => useAuthStore.getState().user?.id ?? 'anonymous')
  function Probe() {
    const owner = useAuthStore(state => state.user?.id ?? 'anonymous')
    const readiness = useQuery({ queryKey: keys[0], queryFn: request, staleTime: Infinity })
    const broker = useQuery({ queryKey: keys[1], queryFn: request, staleTime: Infinity })
    const operator = useQuery({ queryKey: keys[2], queryFn: request, staleTime: Infinity })
    const me = useQuery({ queryKey: keys[3], queryFn: request, staleTime: Infinity })
    const values = [readiness.data, broker.data, operator.data, me.data]
    observed.push({ owner, values })
    return <p data-testid="private">{owner}:{values.join(',')}</p>
  }
  act(() => useAuthStore.getState().setUser(a))
  render(<Providers><Probe /></Providers>)
  await waitFor(() => expect(screen.getByTestId('private')).toHaveTextContent(`${a.id}:${[a.id, a.id, a.id, a.id].join(',')}`))
  act(() => useAuthStore.getState().setUser(b))
  await waitFor(() => expect(screen.getByTestId('private')).toHaveTextContent(`${b.id}:${[b.id, b.id, b.id, b.id].join(',')}`))
  expect(observed.filter(row => row.owner === b.id).some(row => row.values.includes(a.id))).toBe(false)
})

test('late old-owner query cannot populate the new cache and consumed signals abort', async () => {
  const clients: ReturnType<typeof useQueryClient>[] = []
  let resolveA!: (value: string) => void
  let signalA: AbortSignal | undefined
  function Probe() {
    const owner = useAuthStore(state => state.user?.id ?? 'anonymous')
    const client = useQueryClient()
    if (!clients.includes(client)) clients.push(client)
    const query = useQuery({ queryKey: ['broker', 'status'], queryFn: ({ signal }) => {
      if (owner === a.id) { signalA = signal; return new Promise<string>(resolve => { resolveA = resolve }) }
      return Promise.resolve(owner)
    }, staleTime: Infinity })
    return <p data-testid="private">{owner}:{query.data ?? 'unknown'}</p>
  }
  act(() => useAuthStore.getState().setUser(a))
  render(<Providers><Probe /></Providers>)
  await waitFor(() => expect(signalA).toBeDefined())
  act(() => useAuthStore.getState().setUser(b))
  await waitFor(() => expect(screen.getByTestId('private')).toHaveTextContent(`${b.id}:${b.id}`))
  await act(async () => { resolveA(a.id); await Promise.resolve() })
  expect(screen.getByTestId('private')).not.toHaveTextContent(`${b.id}:${a.id}`)
  expect(signalA?.aborted).toBe(true)
  expect(clients).toHaveLength(2)
  expect(clients[0].getQueryData(['broker', 'status'])).toBeUndefined()
})

test('logout then same-owner login creates a fresh cache', () => {
  const clients: ReturnType<typeof useQueryClient>[] = []
  function Probe() {
    const owner = useAuthStore(state => state.user?.id ?? 'anonymous')
    const client = useQueryClient()
    if (!clients.includes(client)) clients.push(client)
    return <p data-testid="private">{owner}</p>
  }
  act(() => useAuthStore.getState().setUser(a))
  render(<Providers><Probe /></Providers>)
  clients[0].setQueryData(['settings', 'live-readiness'], 'old-private-ready')
  act(() => useAuthStore.getState().logout())
  act(() => useAuthStore.getState().setUser(a))
  expect(clients).toHaveLength(3)
  expect(clients[2].getQueryData(['settings', 'live-readiness'])).toBeUndefined()
})


test('batched logout/relogin under the same owner never reuses prior cached authority', () => {
  const clients: ReturnType<typeof useQueryClient>[] = []
  function Probe() {
    const client = useQueryClient()
    if (!clients.includes(client)) clients.push(client)
    return null
  }
  act(() => useAuthStore.getState().setUser(a))
  render(<Providers><Probe /></Providers>)
  clients[0].setQueryData(['settings', 'live-readiness'], 'old-ready')
  act(() => { useAuthStore.getState().logout(); useAuthStore.getState().setUser(a) })
  expect(clients).toHaveLength(2)
  expect(clients[1].getQueryData(['settings', 'live-readiness'])).toBeUndefined()
})

test('same-owner token replacement resets the cache while a repeated verified user read does not', () => {
  const clients: ReturnType<typeof useQueryClient>[] = []
  function Probe() {
    const client = useQueryClient()
    if (!clients.includes(client)) clients.push(client)
    return null
  }
  act(() => useAuthStore.getState().setUser(a))
  render(<Providers><Probe /></Providers>)
  act(() => useAuthStore.getState().setUser({ ...a }))
  expect(clients).toHaveLength(1)
  act(() => api.setToken(crypto.randomUUID()))
  expect(clients).toHaveLength(2)
  const persisted = JSON.parse(localStorage.getItem('cg_auth')!).state
  expect(persisted).not.toHaveProperty('sessionRevision')
})

test('StrictMode replay does not clear the remounted active query client', async () => {
  const clients: ReturnType<typeof useQueryClient>[] = []
  function Probe() {
    const client = useQueryClient()
    if (!clients.includes(client)) clients.push(client)
    const query = useQuery({ queryKey: ['settings', 'live-readiness'], queryFn: async () => 'current-ready', staleTime: Infinity })
    return <p>{query.data ?? 'unknown'}</p>
  }
  act(() => useAuthStore.getState().setUser(a))
  render(<StrictMode><Providers><Probe /></Providers></StrictMode>)
  await screen.findByText('current-ready')
  await act(async () => { await Promise.resolve(); await Promise.resolve() })
  expect(clients.at(-1)?.getQueryData(['settings', 'live-readiness'])).toBe('current-ready')
  expect(screen.getByText('current-ready')).toBeVisible()
})


test('another tab token replacement invalidates the private boundary without sharing cached authority', () => {
  api.setToken(crypto.randomUUID())
  act(() => useAuthStore.getState().setUser(a))
  const clients: ReturnType<typeof useQueryClient>[] = []
  function Probe() {
    const client = useQueryClient()
    if (!clients.includes(client)) clients.push(client)
    return null
  }
  render(<Providers><Probe /></Providers>)
  clients[0].setQueryData(['settings', 'live-readiness'], 'old-ready')
  const previous = localStorage.getItem('cg_token')
  const next = crypto.randomUUID()
  act(() => {
    localStorage.setItem('cg_token', next)
    window.dispatchEvent(new StorageEvent('storage', { key: 'cg_token', oldValue: previous, newValue: next, storageArea: localStorage }))
  })
  expect(clients).toHaveLength(2)
  expect(clients[1].getQueryData(['settings', 'live-readiness'])).toBeUndefined()
  expect(useAuthStore.getState().verifiedToken).toBeNull()
})


test('shared localStorage clear invalidates cached authority even when storage event values are null', () => {
  api.setToken(crypto.randomUUID())
  act(() => useAuthStore.getState().setUser(a))
  const clients: ReturnType<typeof useQueryClient>[] = []
  function Probe() {
    const client = useQueryClient()
    if (!clients.includes(client)) clients.push(client)
    return null
  }
  render(<Providers><Probe /></Providers>)
  clients[0].setQueryData(['settings', 'live-readiness'], 'old-ready')
  act(() => {
    localStorage.clear()
    window.dispatchEvent(new StorageEvent('storage', { key: null, oldValue: null, newValue: null, storageArea: localStorage }))
  })
  expect(clients).toHaveLength(2)
  expect(clients[1].getQueryData(['settings', 'live-readiness'])).toBeUndefined()
})


test('same-owner permission changes clear cached readiness and infinite-stale user records', () => {
  act(() => useAuthStore.getState().setUser(a))
  const clients: ReturnType<typeof useQueryClient>[] = []
  function Probe() {
    const client = useQueryClient()
    if (!clients.includes(client)) clients.push(client)
    return null
  }
  render(<Providers><Probe /></Providers>)
  clients[0].setQueryData(['me'], a)
  clients[0].setQueryData(['settings', 'live-readiness'], 'old-ready')
  act(() => useAuthStore.getState().setUser({ ...a, is_admin: false }))
  expect(clients).toHaveLength(2)
  expect(clients[1].getQueryData(['me'])).toBeUndefined()
  expect(clients[1].getQueryData(['settings', 'live-readiness'])).toBeUndefined()
})
