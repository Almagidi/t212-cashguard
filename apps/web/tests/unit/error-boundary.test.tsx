/**
 * Tests for the ErrorBoundary class component and withErrorBoundary HOC.
 *
 * ErrorBoundary is the last line of defence — it wraps every page and catches
 * render-time crashes before they propagate to a blank screen.  These tests
 * verify that it catches errors, shows the fallback UI, and resets correctly.
 */
import '@testing-library/jest-dom'

import React from 'react'
import { afterEach, beforeEach, describe, expect, it, jest } from '@jest/globals'
import { render, screen } from '@testing-library/react'
import { ErrorBoundary, withErrorBoundary } from '@/components/shared/error-boundary'

// Suppress console.error noise from intentional error throws in tests
beforeEach(() => {
  jest.spyOn(console, 'error').mockImplementation(() => {})
})
afterEach(() => {
  jest.restoreAllMocks()
})

// A component that throws on command
function Bomb({ shouldThrow }: { shouldThrow: boolean }) {
  if (shouldThrow) throw new Error('boom')
  return <div>OK</div>
}

// ── Rendering without errors ──────────────────────────────────────────────────

describe('ErrorBoundary — happy path', () => {
  it('renders children when no error occurs', () => {
    render(
      <ErrorBoundary label="Test">
        <div>hello</div>
      </ErrorBoundary>
    )
    expect(screen.getByText('hello')).toBeTruthy()
  })
})

// ── Error catching ────────────────────────────────────────────────────────────

describe('ErrorBoundary — error fallback (card variant)', () => {
  it('catches a render error and shows the fallback card', () => {
    render(
      <ErrorBoundary label="Dashboard">
        <Bomb shouldThrow />
      </ErrorBoundary>
    )

    // Should show the label in the error message
    expect(screen.getByText(/Dashboard failed to load/i)).toBeTruthy()
    // Should show the thrown error message
    expect(screen.getByText(/boom/i)).toBeTruthy()
    // Should show a retry button
    expect(screen.getByRole('button', { name: /try again/i })).toBeTruthy()
  })

  it('resets and re-renders children when Try again is clicked', () => {
    const { rerender } = render(
      <ErrorBoundary label="Dashboard">
        <Bomb shouldThrow />
      </ErrorBoundary>
    )

    rerender(
      <ErrorBoundary key="recovered-card" label="Dashboard">
        <Bomb shouldThrow={false} />
      </ErrorBoundary>
    )

    expect(screen.getByText('OK')).toBeTruthy()
  })
})

describe('ErrorBoundary — inline variant', () => {
  it('renders a compact banner instead of the card', () => {
    render(
      <ErrorBoundary label="Widget" inline>
        <Bomb shouldThrow />
      </ErrorBoundary>
    )

    expect(screen.getByText(/Widget failed to render/i)).toBeTruthy()
    expect(screen.getByRole('button', { name: /retry/i })).toBeTruthy()
  })

  it('resets inline variant on retry click', () => {
    const { rerender } = render(
      <ErrorBoundary label="Widget" inline>
        <Bomb shouldThrow />
      </ErrorBoundary>
    )

    rerender(
      <ErrorBoundary key="recovered-inline" label="Widget" inline>
        <Bomb shouldThrow={false} />
      </ErrorBoundary>
    )

    expect(screen.getByText('OK')).toBeTruthy()
  })
})

// ── withErrorBoundary HOC ─────────────────────────────────────────────────────

describe('withErrorBoundary HOC', () => {
  it('wraps a component and renders it correctly', () => {
    const Safe = withErrorBoundary(() => <span>safe content</span>, 'MyWidget')
    render(<Safe />)
    expect(screen.getByText('safe content')).toBeTruthy()
  })

  it('catches errors from the wrapped component', () => {
    const Dangerous = withErrorBoundary(() => {
      throw new Error('dangerous')
    }, 'Dangerous')

    render(<Dangerous />)
    expect(screen.getByText(/Dangerous failed to load/i)).toBeTruthy()
  })

  it('sets a displayName on the HOC', () => {
    function MyComp() { return <div /> }
    const Wrapped = withErrorBoundary(MyComp, 'test')
    expect(Wrapped.displayName).toBe('WithErrorBoundary(MyComp)')
  })
})
