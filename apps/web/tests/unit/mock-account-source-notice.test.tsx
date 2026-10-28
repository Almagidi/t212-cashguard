import '@testing-library/jest-dom'
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from '@jest/globals'
import { AccountSourceNotice } from '@/components/dashboard/account-source-notice'

describe('mock account provenance is not an accounting assertion', () => {
  it('separates the adapter example before any stream frame', () => {
    render(<AccountSourceNotice accountMode="mock" hasSnapshot={false} />)
    expect(screen.getByText('Mock adapter example')).toBeVisible()
    expect(screen.getByTestId('account-source-notice')).toHaveTextContent('not a paper ledger balance')
  })
  it('does not invent a source for an old or unidentified frame', () => {
    render(<AccountSourceNotice accountMode="mock" hasSnapshot />)
    expect(screen.getByText('Stream source unverified')).toBeVisible()
    expect(screen.queryByText('Mock animation example')).not.toBeInTheDocument()
  })
  it('retains the animation warning independently of connectivity and conflicting REST mode', () => {
    const view = render(<AccountSourceNotice accountMode="mock" hasSnapshot streamSource="mock_market_animation" />)
    view.rerender(<AccountSourceNotice accountMode="demo" hasSnapshot streamSource="mock_market_animation" />)
    expect(screen.getByText('Mock animation example')).toBeVisible()
    expect(screen.getByTestId('account-source-notice')).toHaveTextContent('not reconciled')
  })
  it('does not relabel an observed demo account as a mock example', () => {
    render(<AccountSourceNotice accountMode="demo" hasSnapshot />)
    expect(screen.queryByTestId('account-source-notice')).not.toBeInTheDocument()
  })
  it('keeps an unknown initial account unverified', () => {
    render(<AccountSourceNotice hasSnapshot={false} />)
    expect(screen.getByText('Account source unverified')).toBeVisible()
  })
})
