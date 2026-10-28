import { afterEach, beforeEach, expect, jest, test } from '@jest/globals'
import { act, renderHook, waitFor } from '@testing-library/react'
import { onlineManager } from '@tanstack/react-query'
import { useOwnerMutation } from '@/hooks/use-owner-mutation'
import { useAuthStore } from '@/stores/auth'
import api from '@/services/api'
import type { User } from '@/types'

jest.mock('next-themes', () => ({ ThemeProvider: ({ children }: { children: React.ReactNode }) => children }))
jest.mock('react-hot-toast', () => ({ __esModule: true, default: { remove: jest.fn() }, Toaster: () => null }))
const { Providers } = require('@/app/providers')
const user = (id: string): User => ({ id, email: 'synthetic@test.invalid', is_active: true, is_admin: true, created_at: '2026-10-28T00:00:00Z' })
const a = user('11111111-1111-4111-8111-111111111111')
const b = user('22222222-2222-4222-8222-222222222222')
const wrapper = ({ children }: { children: React.ReactNode }) => <Providers>{children}</Providers>
beforeEach(() => { act(() => useAuthStore.getState().setUser(a)) })
afterEach(() => { onlineManager.setOnline(true); act(() => useAuthStore.getState().logout()) })

test('a late original-account response cannot produce success, error or settled callbacks in the next session', async () => {
  let resolve!: (value: string) => void
  const request = jest.fn(() => new Promise<string>(done => { resolve = done }))
  const success = jest.fn(), error = jest.fn(), settled = jest.fn(), localSuccess = jest.fn(), localError = jest.fn(), localSettled = jest.fn()
  const { result } = renderHook(() => useOwnerMutation({ mutationFn: request, onSuccess: success, onError: error, onSettled: settled }), { wrapper })
  let outcome!: Promise<unknown>
  act(() => { outcome = result.current.mutateAsync(undefined, { onSuccess: localSuccess, onError: localError, onSettled: localSettled }).catch(value => value) })
  await waitFor(() => expect(request).toHaveBeenCalledTimes(1))
  act(() => useAuthStore.getState().setUser(b))
  await act(async () => { resolve('completed for original account'); await outcome })
  expect(await outcome).toMatchObject({ name: 'OwnerScopeChangedError' })
  for (const callback of [success, error, settled, localSuccess, localError, localSettled]) expect(callback).not.toHaveBeenCalled()
})

test('offline work waiting for preflight cannot dispatch after an account switch and reconnect', async () => {
  onlineManager.setOnline(false)
  let release!: () => void
  const preflight = jest.fn(() => new Promise<void>(done => { release = done }))
  const request = jest.fn(async () => 'must not be sent')
  const { result } = renderHook(() => useOwnerMutation({ mutationFn: request, onMutate: preflight }), { wrapper })
  let outcome!: Promise<unknown>
  act(() => { outcome = result.current.mutateAsync().catch(value => value) })
  await waitFor(() => expect(preflight).toHaveBeenCalledTimes(1))
  act(() => useAuthStore.getState().setUser(b))
  await act(async () => { onlineManager.setOnline(true); release(); await outcome })
  expect(await outcome).toMatchObject({ name: 'OwnerScopeChangedError' })
  expect(request).not.toHaveBeenCalled()
})

test('an old saved callback fails before dispatch, including same-owner token replacement', async () => {
  const request = jest.fn(async () => 'unexpected')
  const { result } = renderHook(() => useOwnerMutation({ mutationFn: request }), { wrapper })
  const oldSubmit = result.current.mutateAsync
  act(() => api.setToken(crypto.randomUUID()))
  await act(async () => { await expect(oldSubmit()).rejects.toMatchObject({ name: 'OwnerScopeChangedError' }) })
  expect(request).not.toHaveBeenCalled()
})

test('an unchanged session retains normal awaited mutation and callback behavior', async () => {
  const success = jest.fn(), settled = jest.fn(), local = jest.fn()
  const { result } = renderHook(() => useOwnerMutation({ mutationFn: async (value: number) => value + 1, onSuccess: success, onSettled: settled }), { wrapper })
  await act(async () => { await expect(result.current.mutateAsync(4, { onSuccess: local })).resolves.toBe(5) })
  expect(success).toHaveBeenCalledTimes(1)
  expect(settled).toHaveBeenCalledTimes(1)
  expect(local).toHaveBeenCalledTimes(1)
})


test('a token replaced in another tab is rejected even before its storage event arrives', async () => {
  api.setToken(crypto.randomUUID())
  act(() => useAuthStore.getState().setUser(a))
  localStorage.setItem('cg_token', crypto.randomUUID())
  const request = jest.fn(async () => 'unexpected')
  const { result } = renderHook(() => useOwnerMutation({ mutationFn: request }), { wrapper })
  await act(async () => { await expect(result.current.mutateAsync()).rejects.toMatchObject({ name: 'OwnerScopeChangedError' }) })
  expect(request).not.toHaveBeenCalled()
})
