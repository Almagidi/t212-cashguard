import '@testing-library/jest-dom'
import { describe, expect, it, jest } from '@jest/globals'
import { render, screen } from '@testing-library/react'

jest.mock('recharts', () => ({
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => children,
  AreaChart: ({ children }: { children: React.ReactNode }) => children,
  YAxis: ({ tickFormatter }: { tickFormatter: (value: number) => string }) => <span data-testid="axis-value">{tickFormatter(5)}</span>,
  Tooltip: ({ content }: { content: React.ReactElement }) => require('react').cloneElement(content, { active: true, payload: [{ value: 12.5 }], label: 'Synthetic date' }),
  Area: () => null, XAxis: () => null, CartesianGrid: () => null, ReferenceLine: () => null,
}))
const { EquityCurve } = require('@/components/charts/equity-curve')

describe('equity curve monetary units', () => {
  it('does not invent a dollar unit for an unlabelled performance series', () => {
    const { container } = render(<EquityCurve data={[{ date: '10-27', pnl: 12.5 }]} />)
    expect(container).not.toHaveTextContent('$')
    expect(screen.getByTestId('axis-value')).toHaveTextContent('5')
    expect(screen.getByText('+12.50 (currency unavailable)')).toBeVisible()
  })
})
