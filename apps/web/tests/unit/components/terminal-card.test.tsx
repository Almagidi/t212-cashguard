import '@testing-library/jest-dom'
import { describe, expect, it } from '@jest/globals'
import { render, screen } from '@testing-library/react'
import { TerminalCard } from '@/components/ui/terminal-card'

describe('TerminalCard', () => {
  it('renders label and value', () => {
    render(<TerminalCard label="P&L" value="£1,234.56" />)
    expect(screen.getByText('P&L')).toBeTruthy()
    expect(screen.getByText('£1,234.56')).toBeTruthy()
  })

  it('renders sub text when provided', () => {
    render(<TerminalCard label="Cash" value="£5,000" sub="Available balance" />)
    expect(screen.getByText('Available balance')).toBeTruthy()
  })

  it('applies cyan variant class by default', () => {
    const { container } = render(<TerminalCard label="X" value="Y" />)
    expect(container.firstChild).toHaveClass('terminal-card-cyan')
  })

  it('applies teal variant class when variant=teal', () => {
    const { container } = render(<TerminalCard label="X" value="Y" variant="teal" />)
    expect(container.firstChild).toHaveClass('terminal-card-teal')
  })

  it('applies red variant class when variant=red', () => {
    const { container } = render(<TerminalCard label="X" value="Y" variant="red" />)
    expect(container.firstChild).toHaveClass('terminal-card-red')
  })

  it('shows live pulse dot when live=true', () => {
    const { container } = render(<TerminalCard label="X" value="Y" live />)
    expect(container.querySelector('[aria-label="live"]')).toBeTruthy()
  })

  it('does not render pulse dot when live is not set', () => {
    const { container } = render(<TerminalCard label="X" value="Y" />)
    expect(container.querySelector('[aria-label="live"]')).toBeNull()
  })
})
