/** @jest-environment-options {"url":"http://localhost:3002/app/operator"} */
import '@testing-library/jest-dom'
import { render, screen } from '@testing-library/react'
import { expect, jest, test } from '@jest/globals'
jest.mock('@tanstack/react-query', () => ({ useQuery: ({ queryKey }: { queryKey: string[] }) => ({ data: queryKey[1] === 'health' ? { mode: 'mock' } : queryKey[1] === 'startup' ? { checks: [] } : { present: true }, isPending: false, isError: false, fetchStatus: 'idle', dataUpdatedAt: 1000 }) }))
jest.mock('@/services/api', () => ({ __esModule: true, API_URL: 'http://localhost:8002', default: { isAuthenticated: () => true, getBackendHealth() {}, getMe() {}, getOperatorStatus() {}, getStartupHealth() {}, getDcaStatus() {}, getDcaActivity() {}, getDcaConfigs() {} } }))
const { RuntimeDiagnostics } = require('@/components/operator/runtime-diagnostics')
test('ports never attest database type or an incompatible launcher', () => {
  render(<RuntimeDiagnostics />)
  expect(screen.queryByText(/incompatible with current startup admission/i)).not.toBeInTheDocument()
  expect(screen.getByText(/database type.*not verified/i)).toBeVisible()
  expect(screen.getAllByText('HTTP 200')).toHaveLength(6)
})
