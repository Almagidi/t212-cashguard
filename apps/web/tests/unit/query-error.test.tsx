import '@testing-library/jest-dom'

import { describe, expect, it, jest } from '@jest/globals'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { QueryError } from '@/components/shared/query-error'

describe('QueryError', () => {
  it('renders the default card state with a string API detail', () => {
    render(
      <QueryError
        error={{ response: { data: { detail: 'Feed health degraded for AAPL.' } } }}
        label="orders"
      />,
    )

    expect(screen.getByText('Failed to load orders')).toBeTruthy()
    expect(screen.getByText('Feed health degraded for AAPL.')).toBeTruthy()
  })

  it('renders object detail.message when available', () => {
    render(
      <QueryError
        error={{ response: { data: { detail: { message: 'Broker reconciliation failed.' } } } }}
        label="positions"
      />,
    )

    expect(screen.getByText('Broker reconciliation failed.')).toBeTruthy()
  })

  it('falls back to the error.message when response detail is absent', () => {
    render(
      <QueryError
        error={{ message: 'Network timeout' }}
        label="risk profile"
      />,
    )

    expect(screen.getByText('Network timeout')).toBeTruthy()
  })

  it('supports inline mode and retry action', async () => {
    const user = userEvent.setup()
    const onRetry = jest.fn()

    render(
      <QueryError
        error={{ response: { data: { detail: 'Unable to refresh watchlist.' } } }}
        label="watchlist"
        inline
        onRetry={onRetry}
      />,
    )

    expect(screen.getByText('Unable to refresh watchlist.')).toBeTruthy()
    await user.click(screen.getByTitle('Retry'))
    expect(onRetry).toHaveBeenCalledTimes(1)
  })
})
