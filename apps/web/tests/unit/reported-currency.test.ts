import { describe, expect, it } from '@jest/globals'
import { formatReportedAmount, formatReportedPnL } from '@/lib/utils'

describe('reported monetary units', () => {
  it.each(['GBP', 'USD', 'EUR'])('prints the authoritative %s code without converting the value', code => {
    expect(formatReportedAmount(1234.5, code).replace(/\s/g, ' ')).toBe(`${code} 1,234.50`)
    expect(formatReportedPnL(-12.25, code)).toMatch(new RegExp(`^-${code}\\s+12\\.25$`))
  })
  it.each([null, undefined, '', 'not-a-code'])('does not assume USD when metadata is %s', code => {
    expect(formatReportedAmount(1234.5, code)).toBe('1,234.50 (currency unavailable)')
  })
  it('keeps missing values unavailable', () => { expect(formatReportedAmount(null, 'GBP')).toBe('—') })
})
