import '@testing-library/jest-dom'
import { expect, jest, test } from '@jest/globals'
import { act, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query'

let dependencyErrorAt = 0
const mutation = { mutateAsync: jest.fn(), isPending: false }
jest.mock('@/hooks/use-api', () => ({
  QK: { settings: ['settings'] },
  useSettings: () => useQuery({ queryKey: ['settings'], queryFn: async () => ({ kill_switch_active: false }), enabled: false }),
  useDepsHealth: () => ({ data: { database: 'ok', redis: 'ok' }, dataUpdatedAt: 3000, errorUpdatedAt: dependencyErrorAt, isPending: false, isFetching: false, isError: false }),
  useOrders: () => ({ data: [], isLoading: false }), useOrder: () => ({ data: null }),
  usePaperExecutionHistory: () => ({ data: undefined }), usePlacePaperOrder: () => mutation,
  useKillSwitch: () => mutation, useCancelOrder: () => mutation, useCancelAllPending: () => mutation,
}))
jest.mock('@/hooks/use-paper-intent', () => ({ usePaperIntent: () => ({ ready: true, busy: false, intent: null, result: null }) }))
const { default: OrdersPage } = require('@/app/app/orders/page')

test('invalidated retained settings stay unknown after remount and dependency recovery until a newer settings success', async () => {
  dependencyErrorAt = 0
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  client.setQueryData(['settings'], { kill_switch_active: false }, { updatedAt: 1000 })
  const view = () => <QueryClientProvider client={client}><OrdersPage /></QueryClientProvider>
  const first = render(view())
  expect(screen.getByText('Activate it to demonstrate a blocked paper order.')).toBeVisible()
  await act(async () => { await client.invalidateQueries({ queryKey: ['settings'], exact: true, refetchType: 'none' }) })
  await waitFor(() => expect(screen.getByText(/kill switch status unknown/i)).toBeVisible())
  first.unmount()
  dependencyErrorAt = 2000
  render(view())
  expect(screen.getByText(/kill switch status unknown/i)).toBeVisible()
  expect(screen.queryByText('Activate it to demonstrate a blocked paper order.')).not.toBeInTheDocument()
  await act(async () => { client.setQueryData(['settings'], { kill_switch_active: false }, { updatedAt: 4000 }) })
  await waitFor(() => expect(screen.getByText('Activate it to demonstrate a blocked paper order.')).toBeVisible())
  client.clear()
})
