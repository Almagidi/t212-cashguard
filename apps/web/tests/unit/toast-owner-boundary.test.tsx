import { StrictMode, useLayoutEffect } from 'react'
import { act, render, screen } from '@testing-library/react'
import toast from 'react-hot-toast'
import { Providers } from '@/app/providers'
import { useAuthStore } from '@/stores/auth'
import type { User } from '@/types'
import { afterEach, expect, jest, test } from '@jest/globals'
Object.defineProperty(window, 'matchMedia', { writable: true, value: () => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }) })
const user = (id: string): User => ({ id, email: 'synthetic@example.invalid', is_active: true, is_admin: true, created_at: '2026-10-28T00:00:00Z' })
afterEach(() => { act(() => { toast.remove(); useAuthStore.getState().logout() }); jest.restoreAllMocks() })
test('an already-emitted owner A connection notice must not be displayed as context in owner B', () => {
  // jsdom has no layout; provide a nonzero toast measurement, as a browser does.
  jest.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({ x: 0, y: 0, width: 300, height: 50, top: 0, right: 300, bottom: 50, left: 0, toJSON: () => ({}) })
  act(() => useAuthStore.getState().setUser(user('11111111-1111-4111-8111-111111111111')))
  render(<Providers><p>Private content</p></Providers>)
  act(() => { toast.success('Synthetic A broker connected') })
  expect(screen.getByText('Synthetic A broker connected')).toBeVisible()
  act(() => useAuthStore.getState().setUser(user('22222222-2222-4222-8222-222222222222')))
  expect(screen.queryByText('Synthetic A broker connected')).not.toBeInTheDocument()
})


test('same-owner session renewal clears its prior notices while ordinary user refresh preserves them', () => {
  jest.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({ x: 0, y: 0, width: 300, height: 50, top: 0, right: 300, bottom: 50, left: 0, toJSON: () => ({}) })
  const a = user('11111111-1111-4111-8111-111111111111')
  act(() => useAuthStore.getState().setUser(a))
  render(<Providers><p>Private content</p></Providers>)
  act(() => { toast.success('Synthetic current-session notice') })
  expect(screen.getByText('Synthetic current-session notice')).toBeVisible()
  act(() => useAuthStore.getState().setUser({ ...a }))
  expect(screen.getByText('Synthetic current-session notice')).toBeVisible()
  act(() => useAuthStore.getState().advanceSession())
  expect(screen.queryByText('Synthetic current-session notice')).not.toBeInTheDocument()
})

test('StrictMode cleanup clears prior-session notices before new-session layout notices are emitted', () => {
  jest.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({ x: 0, y: 0, width: 300, height: 50, top: 0, right: 300, bottom: 50, left: 0, toJSON: () => ({}) })
  const a = user('11111111-1111-4111-8111-111111111111')
  const b = user('22222222-2222-4222-8222-222222222222')
  function CurrentNotice() {
    const owner = useAuthStore(state => state.user?.id)
    useLayoutEffect(() => { toast.success(owner === b.id ? 'Synthetic B current notice' : 'Synthetic A current notice', { id: `synthetic-session-${owner}` }) }, [owner])
    return <p>Private content</p>
  }
  act(() => useAuthStore.getState().setUser(a))
  render(<StrictMode><Providers><CurrentNotice /></Providers></StrictMode>)
  expect(screen.getByText('Synthetic A current notice')).toBeVisible()
  act(() => useAuthStore.getState().setUser(b))
  expect(screen.queryByText('Synthetic A current notice')).not.toBeInTheDocument()
  expect(screen.getByText('Synthetic B current notice')).toBeVisible()
})
