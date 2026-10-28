import '@testing-library/jest-dom'

import { beforeEach, describe, expect, it, jest, test } from '@jest/globals'
import { act, render, screen, waitFor } from '@testing-library/react'

const replace = jest.fn()
const getToken = jest.fn<() => string | null>()
const getMe = jest.fn<() => Promise<any>>()
const logoutApi = jest.fn<() => Promise<void>>()
const setUser = jest.fn<(user: any) => void>()
let mockRevision = 0
const logoutStore = jest.fn<() => void>()

jest.mock('next/navigation', () => ({
  useRouter: () => ({ replace }),
  usePathname: () => '/app/orders',
}))

jest.mock('@/services/api', () => ({
  __esModule: true,
  default: {
    getToken,
    getMe,
    logout: logoutApi,
  },
}))

jest.mock('@/stores/auth', () => ({
  useAuthStore: Object.assign((selector: (state: { setUser: typeof setUser; logout: typeof logoutStore }) => unknown) =>
    selector({ setUser, logout: logoutStore }), { getState: () => ({ user: null, sessionRevision: mockRevision, verifiedToken: null }) }),
}))

const { AuthGate } = require('@/components/layout/auth-gate')

describe('AuthGate', () => {
  beforeEach(() => {
    jest.clearAllMocks()
  })

  it('redirects to login when no token is present', async () => {
    getToken.mockReturnValue(null)

    render(<AuthGate><div>secret page</div></AuthGate>)

    await waitFor(() => {
      expect(logoutStore).toHaveBeenCalledTimes(1)
      expect(replace).toHaveBeenCalledWith('/auth/login?next=%2Fapp%2Forders')
    })
  })

  it('renders children after a valid session check', async () => {
    getToken.mockReturnValue('token-123')
    getMe.mockResolvedValue({
      id: 'user-1',
      email: 'admin@localhost',
      is_active: true,
      is_admin: true,
      created_at: '2026-04-23T10:00:00Z',
    })

    render(<AuthGate><div>secret page</div></AuthGate>)

    expect(screen.getByText('Verifying session...')).toBeTruthy()

    await waitFor(() => {
      expect(setUser).toHaveBeenCalledWith(expect.objectContaining({ email: 'admin@localhost' }))
      expect(screen.getByText('secret page')).toBeTruthy()
    })
  })

  it('logs out and redirects when session verification fails with 401', async () => {
    getToken.mockReturnValue('bad-token')
    const err = Object.assign(new Error('Unauthorized'), { response: { status: 401 } })
    getMe.mockRejectedValue(err)
    logoutApi.mockResolvedValue(undefined)

    render(<AuthGate><div>secret page</div></AuthGate>)

    await waitFor(() => {
      expect(logoutApi).toHaveBeenCalledTimes(1)
      expect(logoutStore).toHaveBeenCalledTimes(1)
      expect(replace).toHaveBeenCalledWith('/auth/login?next=%2Fapp%2Forders')
    })
  })
})


test('a failed session check cannot render persisted private controls', async () => {
  jest.clearAllMocks()
  getToken.mockReturnValue('synthetic-unverified-session')
  getMe.mockRejectedValue(Object.assign(new Error('offline'), { response: { status: 503 } }))
  render(<AuthGate><div>private live controls</div></AuthGate>)
  expect(await screen.findByRole('alert')).toHaveTextContent('Session verification unavailable')
  expect(screen.queryByText('private live controls')).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Retry session verification' })).toBeEnabled()
})

test('a delayed different-token verification cannot rebind the current session', async () => {
  jest.clearAllMocks()
  let resolve!: (value: unknown) => void
  getToken.mockReturnValue('synthetic-original-session')
  getMe.mockImplementation(() => new Promise(done => { resolve = done }))
  render(<AuthGate><div>private live controls</div></AuthGate>)
  getToken.mockReturnValue('synthetic-next-session')
  await act(async () => { resolve({ id: 'original-owner' }); await Promise.resolve() })
  expect(setUser).not.toHaveBeenCalled()
  expect(screen.queryByText('private live controls')).not.toBeInTheDocument()
})


test('same-token session renewal cannot adopt a delayed original-session identity', async () => {
  jest.clearAllMocks()
  let resolve!: (value: unknown) => void
  getToken.mockReturnValue('synthetic-same-token')
  getMe.mockImplementation(() => new Promise(done => { resolve = done }))
  render(<AuthGate><div>private live controls</div></AuthGate>)
  mockRevision += 1
  await act(async () => { resolve({ id: 'original-owner' }); await Promise.resolve() })
  expect(setUser).not.toHaveBeenCalled()
  expect(screen.queryByText('private live controls')).not.toBeInTheDocument()
})


test('same-token renewal during a failed-session logout cannot clear the new session', async () => {
  jest.clearAllMocks()
  getToken.mockReturnValue('synthetic-same-token')
  getMe.mockRejectedValue(Object.assign(new Error('Unauthorized'), { response: { status: 401 } }))
  let release!: () => void
  logoutApi.mockImplementation(() => new Promise(done => { release = done }))
  render(<AuthGate><div>private live controls</div></AuthGate>)
  await waitFor(() => expect(logoutApi).toHaveBeenCalledTimes(1))
  mockRevision += 1
  await act(async () => { release(); await Promise.resolve() })
  expect(logoutStore).not.toHaveBeenCalled()
  expect(replace).not.toHaveBeenCalled()
})
