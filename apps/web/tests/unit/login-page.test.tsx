import '@testing-library/jest-dom'

import { beforeEach, describe, expect, it, jest } from '@jest/globals'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

const push = jest.fn()
const mockSearchParams = new URLSearchParams()
const login = jest.fn<(payload: any) => Promise<any>>()
const getMe = jest.fn<() => Promise<any>>()
let mockSessionRevision = 0
const setUser = jest.fn<(user: any) => void>()
const toastError = jest.fn<(message: string) => void>()

jest.mock('next/navigation', () => ({
  useRouter: () => ({ push }),
  useSearchParams: () => mockSearchParams,
}))

jest.mock('@/services/api', () => ({
  __esModule: true,
  default: {
    login: async (payload: unknown) => {
      const response = await login(payload)
      localStorage.setItem('cg_token', response.access_token)
      mockSessionRevision += 1
      return response
    },
    getMe,
    getToken: () => localStorage.getItem('cg_token'),
  },
}))

jest.mock('@/stores/auth', () => ({
  useAuthStore: Object.assign(() => ({ setUser }), { getState: () => ({ user: null, sessionRevision: mockSessionRevision, verifiedToken: null }) }),
}))

jest.mock('react-hot-toast', () => ({
  __esModule: true,
  default: {
    error: toastError,
  },
}))

const LoginPage = require('@/app/auth/login/page').default

describe('LoginPage', () => {
  beforeEach(() => {
    jest.clearAllMocks()
    mockSessionRevision = 0
    localStorage.removeItem('cg_token')
    mockSearchParams.delete('next')
  })

  it('renders the login form with the default admin email', () => {
    render(<LoginPage />)

    expect(screen.getByRole('heading', { name: 'CashGuard Trader' })).toBeTruthy()
    expect((screen.getByLabelText('Email') as HTMLInputElement).value).toBe('admin@localhost')
    expect(screen.getByRole('button', { name: 'Sign in' })).toBeTruthy()
  })

  it('logs in successfully and redirects to the safe next path', async () => {
    const user = userEvent.setup()
    mockSearchParams.set('next', '/app/orders')
    login.mockResolvedValue({ access_token: 'token' })
    getMe.mockResolvedValue({
      id: 'user-1',
      email: 'admin@localhost',
      is_active: true,
      is_admin: true,
      created_at: '2026-04-23T10:00:00Z',
    })

    render(<LoginPage />)

    await user.type(screen.getByLabelText('Password'), 'change-me')
    await user.click(screen.getByRole('button', { name: 'Sign in' }))

    await waitFor(() => {
      expect(login).toHaveBeenCalledWith({ email: 'admin@localhost', password: 'change-me' })
      expect(setUser).toHaveBeenCalledWith(expect.objectContaining({ email: 'admin@localhost' }))
      expect(push).toHaveBeenCalledWith('/app/orders')
    })
  })

  it('falls back to dashboard when next is unsafe', async () => {
    const user = userEvent.setup()
    mockSearchParams.set('next', 'https://evil.example/steal')
    login.mockResolvedValue({ access_token: 'token' })
    getMe.mockResolvedValue({
      id: 'user-1',
      email: 'admin@localhost',
      is_active: true,
      is_admin: true,
      created_at: '2026-04-23T10:00:00Z',
    })

    render(<LoginPage />)

    await user.type(screen.getByLabelText('Password'), 'change-me')
    await user.click(screen.getByRole('button', { name: 'Sign in' }))

    await waitFor(() => {
      expect(push).toHaveBeenCalledWith('/app/dashboard')
    })
  })

  it('shows validation errors when required fields are empty', async () => {
    const user = userEvent.setup()

    render(<LoginPage />)

    await user.clear(screen.getByLabelText('Email'))
    await user.click(screen.getByRole('button', { name: 'Sign in' }))

    await waitFor(() => {
      expect(screen.getByText('Email required')).toBeTruthy()
      expect(screen.getByText('Password required')).toBeTruthy()
    })

    expect(login).not.toHaveBeenCalled()
  })

  it('shows the API error message on login failure', async () => {
    const user = userEvent.setup()
    login.mockRejectedValue({
      response: {
        data: {
          detail: 'Invalid credentials',
        },
      },
    })

    render(<LoginPage />)

    await user.type(screen.getByLabelText('Password'), 'wrong-password')
    await user.click(screen.getByRole('button', { name: 'Sign in' }))

    await waitFor(() => {
      expect(toastError).toHaveBeenCalledWith('Invalid credentials')
      expect(push).not.toHaveBeenCalled()
    })
  })
})
