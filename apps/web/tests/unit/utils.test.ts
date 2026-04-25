import { describe, expect, it } from '@jest/globals'
import {
  executionQualityBadge,
  executionQualityClass,
  formatCurrency,
  formatPnL,
  orderStatusBg,
  pnlClass,
  timeAgo,
  truncate,
} from '@/lib/utils'

describe('formatCurrency', () => {
  it('formats positive number', () => expect(formatCurrency(1234.56)).toBe('$1,234.56'))
  it('formats negative number', () => expect(formatCurrency(-99.5)).toBe('-$99.50'))
  it('returns dash for null', () => expect(formatCurrency(null)).toBe('—'))
  it('returns dash for NaN', () => expect(formatCurrency('abc')).toBe('—'))
  it('formats zero', () => expect(formatCurrency(0)).toBe('$0.00'))
})

describe('formatPnL', () => {
  it('shows + prefix for positive', () => expect(formatPnL(100)).toBe('+$100.00'))
  it('shows - for negative', () => expect(formatPnL(-50)).toContain('-$50.00'))
  it('returns dash for null', () => expect(formatPnL(null)).toBe('—'))
})

describe('pnlClass', () => {
  it('returns positive class for positive values', () => expect(pnlClass(10)).toBe('pnl-positive'))
  it('returns negative class for negative values', () => expect(pnlClass(-10)).toBe('pnl-negative'))
  it('returns neutral class for zero', () => expect(pnlClass(0)).toBe('pnl-neutral'))
  it('returns neutral class for null', () => expect(pnlClass(null)).toBe('pnl-neutral'))
})

describe('orderStatusBg', () => {
  it('filled orders get green styling', () => expect(orderStatusBg('filled')).toContain('emerald'))
  it('error orders get red styling', () => expect(orderStatusBg('error')).toContain('red'))
  it('submitted orders get blue styling', () => expect(orderStatusBg('submitted')).toContain('blue'))
  it('unknown status returns muted', () => expect(orderStatusBg('unknown')).toContain('muted'))
})

describe('executionQuality helpers', () => {
  it('maps good grades to green styling', () => {
    expect(executionQualityClass('good')).toContain('emerald')
    expect(executionQualityBadge('excellent')).toContain('emerald')
  })

  it('maps degraded grades to red styling', () => {
    expect(executionQualityClass('poor')).toContain('red')
    expect(executionQualityBadge('degraded')).toContain('red')
  })
})

describe('truncate', () => {
  it('truncates long strings', () => expect(truncate('hello world this is long', 10)).toBe('hello worl…'))
  it('leaves short strings unchanged', () => expect(truncate('short', 10)).toBe('short'))
  it('handles exact length', () => expect(truncate('exactly16chars!!', 16)).toBe('exactly16chars!!'))
})

describe('timeAgo', () => {
  it('returns dash for null', () => expect(timeAgo(null)).toBe('—'))
  it('shows seconds for recent', () => {
    const recent = new Date(Date.now() - 5000).toISOString()
    expect(timeAgo(recent)).toContain('s ago')
  })
  it('shows minutes for minute-old', () => {
    const old = new Date(Date.now() - 120_000).toISOString()
    expect(timeAgo(old)).toContain('m ago')
  })
})
