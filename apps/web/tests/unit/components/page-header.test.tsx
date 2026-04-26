import '@testing-library/jest-dom/jest-globals'
import { describe, expect, it } from '@jest/globals'
import { render, screen } from '@testing-library/react'
import { PageHeader } from '@/components/layout/page-header'
import { Database } from 'lucide-react'

describe('PageHeader', () => {
  it('renders label', () => {
    render(<PageHeader icon={<Database />} label="Instruments" />)
    expect(screen.getByText('Instruments')).toBeTruthy()
  })

  it('renders sub text when provided', () => {
    render(<PageHeader icon={<Database />} label="Instruments" sub="1,234 instruments" />)
    expect(screen.getByText('1,234 instruments')).toBeTruthy()
  })

  it('does not render sub when omitted', () => {
    const { container } = render(<PageHeader icon={<Database />} label="Instruments" />)
    expect(container.querySelector('p')).toBeNull()
  })

  it('renders actions slot', () => {
    render(<PageHeader icon={<Database />} label="X" actions={<button>Sync</button>} />)
    expect(screen.getByRole('button', { name: 'Sync' })).toBeTruthy()
  })
})
