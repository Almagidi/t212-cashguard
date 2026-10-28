import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, jest, test } from '@jest/globals'
import type { AxiosInstance } from 'axios'
import api from '@/services/api'
import { useAuthStore } from '@/stores/auth'
import type { User } from '@/types'
let releaseValidation: (() => void) | undefined
const navigate = jest.fn()
jest.doMock('next/navigation', () => ({ useRouter: () => ({ push: navigate }), useSearchParams: () => ({ get: () => null }) }))
jest.doMock('@hookform/resolvers/zod', () => {
  const actual = jest.requireActual<typeof import('@hookform/resolvers/zod')>('@hookform/resolvers/zod')
  return { zodResolver: (...options: Parameters<typeof actual.zodResolver>) => {
    const validate = actual.zodResolver(...options)
    return async (...values: Parameters<typeof validate>) => {
      const result = await validate(...values)
      await new Promise<void>(resolve => { releaseValidation = resolve })
      return result
    }
  } }
})
const LoginPage = require('@/app/auth/login/page').default
const user = (id: string): User => ({ id, email: 'synthetic@example.invalid', is_active: true, is_admin: true, created_at: '2026-10-28T00:00:00Z' })
const a = user('11111111-1111-4111-8111-111111111111')
const b = user('22222222-2222-4222-8222-222222222222')
const client = (api as unknown as { client: AxiosInstance }).client
const originalAdapter = client.defaults.adapter
beforeEach(() => { releaseValidation = undefined; jest.clearAllMocks() })
afterEach(() => { client.defaults.adapter = originalAdapter; act(() => useAuthStore.getState().logout()); jest.restoreAllMocks() })
test('old login validation cannot replace a newer verified session', async () => {
  const dispatched: string[] = []
  client.defaults.adapter = async config => {
    dispatched.push(config.url ?? '')
    return { config, status: 200, statusText: 'OK', headers: {}, data: config.url === '/auth/login' ? { access_token: crypto.randomUUID(), token_type: 'bearer' } : a }
  }
  api.setToken(crypto.randomUUID())
  act(() => useAuthStore.getState().setUser(a))
  const view = render(<LoginPage key="owner-a" />)
  fireEvent.change(screen.getByLabelText('Email'), { target: { value: 'synthetic@example.invalid' } })
  fireEvent.change(screen.getByLabelText('Password'), { target: { value: crypto.randomUUID() } })
  fireEvent.click(screen.getByRole('button', { name: 'Sign in' }))
  await waitFor(() => expect(releaseValidation).toBeDefined())
  expect(dispatched).toEqual([])
  act(() => { api.setToken(crypto.randomUUID()); useAuthStore.getState().setUser(b) })
  view.rerender(<LoginPage key="owner-b" />)
  await act(async () => { releaseValidation!(); await Promise.resolve(); await Promise.resolve() })
  expect(dispatched).toEqual([])
  expect(useAuthStore.getState().user?.id).toBe(b.id)
  expect(navigate).not.toHaveBeenCalled()
})


test('an unchanged login form retains its deliberate new-session transition and authenticated navigation', async () => {
  const dispatched: string[] = []
  const token = crypto.randomUUID()
  client.defaults.adapter = async config => {
    dispatched.push(config.url ?? '')
    return { config, status: 200, statusText: 'OK', headers: {}, data: config.url === '/auth/login' ? { access_token: token, token_type: 'bearer' } : a }
  }
  api.setToken(token)
  act(() => useAuthStore.getState().setUser(a))
  const view = render(<LoginPage />)
  fireEvent.change(screen.getByLabelText('Password'), { target: { value: crypto.randomUUID() } })
  fireEvent.click(screen.getByRole('button', { name: 'Sign in' }))
  await waitFor(() => expect(releaseValidation).toBeDefined())
  await act(async () => { releaseValidation!(); await Promise.resolve(); await Promise.resolve() })
  expect(dispatched).toEqual(['/auth/login', '/auth/me'])
  expect(useAuthStore.getState().user?.id).toBe(a.id)
  expect(navigate).toHaveBeenCalledWith('/app/dashboard')
  view.unmount()
})

test('a completed login cannot adopt another session token before its identity handoff', async () => {
  const dispatched: string[] = []
  const returnedToken = crypto.randomUUID()
  const newerToken = crypto.randomUUID()
  client.defaults.adapter = async config => {
    dispatched.push(config.url ?? '')
    return { config, status: 200, statusText: 'OK', headers: {}, data: config.url === '/auth/login' ? { access_token: returnedToken, token_type: 'bearer' } : b }
  }
  const login = api.login.bind(api)
  jest.spyOn(api, 'login').mockImplementation(async payload => {
    const response = await login(payload)
    api.setToken(newerToken)
    useAuthStore.getState().setUser(b)
    return response
  })
  render(<LoginPage />)
  fireEvent.change(screen.getByLabelText('Password'), { target: { value: crypto.randomUUID() } })
  fireEvent.click(screen.getByRole('button', { name: 'Sign in' }))
  await waitFor(() => expect(releaseValidation).toBeDefined())
  await act(async () => { releaseValidation!(); await Promise.resolve(); await Promise.resolve() })
  expect(dispatched).toEqual(['/auth/login'])
  expect(useAuthStore.getState().user?.id).toBe(b.id)
  expect(api.getToken()).toBe(newerToken)
  expect(navigate).not.toHaveBeenCalled()
})


test('a newer same-token reauthentication cannot be adopted by the original login continuation', async () => {
  const dispatched: string[] = []
  const token = crypto.randomUUID()
  client.defaults.adapter = async config => {
    dispatched.push(config.url ?? '')
    return { config, status: 200, statusText: 'OK', headers: {}, data: config.url === '/auth/login' ? { access_token: token, token_type: 'bearer' } : a }
  }
  const login = api.login.bind(api)
  jest.spyOn(api, 'login').mockImplementation(async payload => {
    const response = await login(payload)
    api.setToken(response.access_token, true)
    useAuthStore.getState().setUser(a)
    return response
  })
  render(<LoginPage />)
  fireEvent.change(screen.getByLabelText('Password'), { target: { value: crypto.randomUUID() } })
  fireEvent.click(screen.getByRole('button', { name: 'Sign in' }))
  await waitFor(() => expect(releaseValidation).toBeDefined())
  await act(async () => { releaseValidation!(); await Promise.resolve(); await Promise.resolve() })
  expect(dispatched).toEqual(['/auth/login'])
  expect(useAuthStore.getState().user?.id).toBe(a.id)
  expect(api.getToken()).toBe(token)
  expect(navigate).not.toHaveBeenCalled()
})
