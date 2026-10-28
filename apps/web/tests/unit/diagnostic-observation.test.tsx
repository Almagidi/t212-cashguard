import '@testing-library/jest-dom'
import { describe, expect, it, jest } from '@jest/globals'
import { render, screen } from '@testing-library/react'

let failed = true
jest.mock('@tanstack/react-query', () => ({
  useQuery: ({ queryKey }: { queryKey: string[] }) => ({
    data: queryKey[1] === 'health' ? { mode: 'demo', status: 'ok' } : queryKey[1] === 'startup' ? { checks: [{ key: 't212_demo_credentials', status: 'pass' }] } : { present: true },
    isError: failed, isPending: false, fetchStatus: 'idle', failureCount: failed ? 1 : 0,
    dataUpdatedAt: 1000, error: failed ? { response: { status: 503 } } : null,
  }),
}))
jest.mock('@/services/api', () => ({
  __esModule: true, API_URL: 'http://127.0.0.1:8000', default: {
    isAuthenticated: () => true, getBackendHealth() {}, getMe() {}, getOperatorStatus() {},
    getStartupHealth() {}, getDcaStatus() {}, getDcaActivity() {}, getDcaConfigs() {},
  },
}))
const { RuntimeDiagnostics } = require('@/components/operator/runtime-diagnostics')

describe('diagnostic observations', () => {
  it('does not promise a demo-only backend from frontend demo configuration after failure', () => {
    const previous = process.env.NEXT_PUBLIC_APP_MODE
    try {
      process.env.NEXT_PUBLIC_APP_MODE = 'demo'; failed = true
      render(<RuntimeDiagnostics />)
      expect(screen.getByText('Backend unavailable')).toBeVisible()
      expect(screen.queryByText(/live remains disabled/)).not.toBeInTheDocument()
    } finally { if (previous === undefined) delete process.env.NEXT_PUBLIC_APP_MODE; else process.env.NEXT_PUBLIC_APP_MODE = previous }
  })
  it('does not call retained data HTTP200 or verified backend mode after a failure', () => {
    failed = true; render(<RuntimeDiagnostics />)
    expect(screen.queryAllByText('HTTP 200')).toHaveLength(0)
    expect(screen.queryByText('Backend demo')).not.toBeInTheDocument()
    expect(screen.queryByText('Demo credentials configured')).not.toBeInTheDocument()
    expect(screen.getAllByText('HTTP 503').length).toBeGreaterThan(0)
  })
  it('only recovers successful labels after a new success', () => {
    failed = true; const { rerender } = render(<RuntimeDiagnostics />)
    failed = false; rerender(<RuntimeDiagnostics />)
    expect(screen.getAllByText('HTTP 200')).toHaveLength(6)
    expect(screen.getByText('Backend demo')).toBeInTheDocument()
  })
})
