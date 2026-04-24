import '@testing-library/jest-dom'

import { beforeEach, describe, expect, it, jest } from '@jest/globals'
import { render, screen, waitFor } from '@testing-library/react'

const replace = jest.fn()
const getToken = jest.fn<() => string | null>()
const getMe = jest.fn<() => Promise<any>>()
const logoutApi = jest.fn<() => Promise<void>>()
const setUser = jest.fn<(user: any) => void>()
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
  useAuthStore: (selector: (state: { setUser: typeof setUser; logout: typeof logoutStore }) => unknown) =>
    selector({ setUser, logout: logoutStore }),
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

  it('logs out and redirects when session verification fails', async () => {
    getToken.mockReturnValue('bad-token')
    getMe.mockRejectedValue(new Error('Unauthorized'))
    logoutApi.mockResolvedValue(undefined)

    render(<AuthGate><div>secret page</div></AuthGate>)

    await waitFor(() => {
      expect(logoutApi).toHaveBeenCalledTimes(1)
      expect(logoutStore).toHaveBeenCalledTimes(1)
      expect(replace).toHaveBeenCalledWith('/auth/login?next=%2Fapp%2Forders')
    })
  })
})
