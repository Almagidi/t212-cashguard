import '@testing-library/jest-dom'
import { beforeEach, expect, jest, test } from '@jest/globals'
import { render, screen } from '@testing-library/react'

let settings: Record<string, unknown>
let intent: Record<string, unknown> | null
const mutation = { mutateAsync: jest.fn(), isPending: false }
jest.mock('@tanstack/react-query', () => ({ useQueryClient: () => ({ invalidateQueries: jest.fn(), getQueryCache: () => ({ subscribe: () => () => {} }), getQueryState: () => ({ isInvalidated: false }) }) }))
jest.mock('@/hooks/use-api', () => ({
  QK: { settings: ['settings'] },
  useOrders: () => ({ data: [], isLoading: false }), useOrder: () => ({ data: null }),
  useSettings: () => settings, usePaperExecutionHistory: () => ({ data: undefined }),
  useDepsHealth: () => ({ data: { database: 'ok' }, dataUpdatedAt: 3000, errorUpdatedAt: 0, isPending: false, isError: false }),
  usePlacePaperOrder: () => mutation, useKillSwitch: () => mutation,
  useCancelOrder: () => mutation, useCancelAllPending: () => mutation,
}))
jest.mock('@/hooks/use-paper-intent', () => ({ usePaperIntent: () => ({
  ready: true, busy: false, intent, result: null, submit: jest.fn(), check: jest.fn(), startNew: jest.fn(),
}) }))
const { default: OrdersPage } = require('@/app/app/orders/page')

beforeEach(() => {
  settings = { data: { kill_switch_active: false }, dataUpdatedAt: 1000, isPending: false, isError: false, isFetching: false }
  intent = null
})

test.each(['recorded', 'pending'])('opaque %s operation never displays unrelated default financial inputs', state => {
  intent = { state, operationId: '00000000-0000-4000-8000-000000000001' }
  render(<OrdersPage />)
  expect(screen.queryByLabelText('Ticker')).not.toBeInTheDocument()
  expect(screen.getByText(/inputs are no longer stored/i)).toBeVisible()
  expect(screen.getByRole('button', { name: 'Check Result' })).toBeEnabled()
})

test('pending operation displays only its persisted input while retaining its identity', () => {
  intent = { state: 'pending', operationId: '00000000-0000-4000-8000-000000000001', payload: { ticker: 'SAVED', side: 'buy', quantity: '2', venue: 'paper' } }
  render(<OrdersPage />)
  expect(screen.getByLabelText('Ticker')).toHaveValue('SAVED')
  expect(screen.getByLabelText('Ticker')).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Retry Same Operation' })).toBeEnabled()
})

test.each([
  { data: undefined, isPending: true },
  { data: undefined, isError: true },
  { data: { kill_switch_active: false }, isFetching: true },
  { data: { kill_switch_active: false }, isError: true },
  { data: { kill_switch_active: false }, fetchStatus: 'paused' },
])('unavailable or stale settings never assert an inactive kill switch: %j', query => {
  settings = query
  render(<OrdersPage />)
  expect(screen.getByText(/kill switch status unknown/i)).toBeVisible()
  expect(screen.queryByText('Activate it to demonstrate a blocked paper order.')).not.toBeInTheDocument()
})

test('retained active kill warning remains protective after a failed refresh', () => {
  settings = { data: { kill_switch_active: true }, isError: true }
  render(<OrdersPage />)
  expect(screen.getByText(/kill switch status unknown/i)).toBeVisible()
  expect(screen.getByTestId('paper-kill-switch-active-badge')).toHaveTextContent('Kill switch last reported active')
})

test('a current disarmed observation alone permits the normal inactive instruction', () => {
  render(<OrdersPage />)
  expect(screen.getByText('Activate it to demonstrate a blocked paper order.')).toBeVisible()
})
