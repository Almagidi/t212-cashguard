/** @jest-environment-options {"url":"http://localhost:3000/app/operator"} */
import '@testing-library/jest-dom'
import { render, screen } from '@testing-library/react'
import { describe, expect, it, jest } from '@jest/globals'

jest.mock('@tanstack/react-query', () => ({
  useQuery: ({ queryKey }: { queryKey: string[] }) => ({
    data: queryKey[1] === 'health' ? { mode: 'demo' } : queryKey[1] === 'startup' ? { checks: [] } : { present: true },
    isPending: false, isError: false, fetchStatus: 'idle', dataUpdatedAt: 1000,
  }),
}))
jest.mock('@/services/api', () => ({
  __esModule: true, API_URL: 'http://localhost:8000', default: {
    isAuthenticated: () => true, getBackendHealth() {}, getMe() {}, getOperatorStatus() {},
    getStartupHealth() {}, getDcaStatus() {}, getDcaActivity() {}, getDcaConfigs() {},
  },
}))
const { RuntimeDiagnostics } = require('@/components/operator/runtime-diagnostics')

describe('supported diagnostic startup guidance', () => {
  it('directs a normal demo runtime to PostgreSQL instructions rather than an incompatible SQLite shortcut', () => {
    const previous = process.env.NEXT_PUBLIC_APP_MODE
    try {
      process.env.NEXT_PUBLIC_APP_MODE = 'demo'
      render(<RuntimeDiagnostics />)
      expect(screen.getByText(/docs\/local-install\.md/)).toBeVisible()
      expect(screen.queryByText(/make operator-manual/)).not.toBeInTheDocument()
    } finally {
      if (previous === undefined) delete process.env.NEXT_PUBLIC_APP_MODE
      else process.env.NEXT_PUBLIC_APP_MODE = previous
    }
  })
})
