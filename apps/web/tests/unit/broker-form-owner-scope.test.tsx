import { afterEach, beforeEach, expect, jest, test } from '@jest/globals'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useAuthStore } from '@/stores/auth'
import type { User } from '@/types'

let mockDeferValidation = false
let mockReleaseValidation: (() => void) | undefined
const mockConnect = jest.fn<(payload: unknown) => Promise<unknown>>()
const mockSuccess = jest.fn()
const mockError = jest.fn()

jest.mock('@hookform/resolvers/zod', () => {
  const actual = jest.requireActual<typeof import('@hookform/resolvers/zod')>('@hookform/resolvers/zod')
  return {
    zodResolver: (...options: Parameters<typeof actual.zodResolver>) => {
      const validate = actual.zodResolver(...options)
      return async (...values: Parameters<typeof validate>) => {
        const result = await validate(...values)
        if (mockDeferValidation) await new Promise<void>(resolve => { mockReleaseValidation = resolve })
        return result
      }
    },
  }
})
jest.mock('@/hooks/use-api', () => ({
  useBrokerStatus: () => ({ data: null, isLoading: false }),
  useAccount: () => ({ data: null }),
  useTestBroker: () => ({ data: undefined, mutate: jest.fn(), isPending: false }),
  useDisconnectBroker: () => ({ mutate: jest.fn(), isPending: false }),
}))
jest.mock('@/services/api', () => ({ __esModule: true, default: { connectBroker: (payload: unknown) => mockConnect(payload) } }))
jest.mock('react-hot-toast', () => ({ __esModule: true, default: { success: mockSuccess, error: mockError } }))
const BrokerPage = require('@/app/app/broker/page').default
const user = (id: string): User => ({ id, email: 'synthetic@test.invalid', is_active: true, is_admin: true, created_at: '2026-10-28T00:00:00Z' })
const a = user('11111111-1111-4111-8111-111111111111')
const b = user('22222222-2222-4222-8222-222222222222')
const client = () => new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
const page = (owner: string, queryClient: QueryClient) => <QueryClientProvider key={owner} client={queryClient}><BrokerPage /></QueryClientProvider>
function fillForm() {
  fireEvent.change(screen.getByLabelText('API Key'), { target: { value: crypto.randomUUID() } })
  fireEvent.change(screen.getByLabelText('API Secret'), { target: { value: crypto.randomUUID() } })
  fireEvent.click(screen.getByRole('button', { name: 'Connect Broker' }))
}
beforeEach(() => {
  jest.clearAllMocks()
  mockDeferValidation = false
  mockReleaseValidation = undefined
  act(() => useAuthStore.getState().setUser(a))
})
afterEach(() => { act(() => useAuthStore.getState().logout()) })

test('the actual broker form cannot submit A credentials as B after asynchronous schema validation', async () => {
  mockDeferValidation = true
  mockConnect.mockResolvedValue(null)
  const view = render(page(a.id, client()))
  fillForm()
  await waitFor(() => expect(mockReleaseValidation).toBeDefined())
  expect(mockConnect).not.toHaveBeenCalled()
  act(() => useAuthStore.getState().setUser(b))
  view.rerender(page(b.id, client()))
  await act(async () => { mockReleaseValidation!(); await Promise.resolve() })
  expect(mockConnect).not.toHaveBeenCalled()
  expect(mockSuccess).not.toHaveBeenCalled()
  expect(mockError).not.toHaveBeenCalled()
})

test.each(['success', 'failure'] as const)('a late original-account broker %s cannot announce a result in B', async outcome => {
  let complete!: () => void
  mockConnect.mockImplementation(() => new Promise((resolve, reject) => {
    complete = () => outcome === 'success' ? resolve(null) : reject(new Error('synthetic original-account failure'))
  }))
  const view = render(page(a.id, client()))
  fillForm()
  await waitFor(() => expect(mockConnect).toHaveBeenCalledTimes(1))
  act(() => useAuthStore.getState().setUser(b))
  view.rerender(page(b.id, client()))
  await act(async () => { complete(); await Promise.resolve() })
  expect(mockSuccess).not.toHaveBeenCalled()
  expect(mockError).not.toHaveBeenCalled()
})

test('an unchanged session still validates, dispatches once and announces its own completed connection', async () => {
  mockConnect.mockResolvedValue(null)
  render(page(a.id, client()))
  fillForm()
  await waitFor(() => expect(mockConnect).toHaveBeenCalledTimes(1))
  await waitFor(() => expect(mockSuccess).toHaveBeenCalledWith('Broker connected'))
  expect(mockError).not.toHaveBeenCalled()
})
