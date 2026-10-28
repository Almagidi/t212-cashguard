import { describe, expect, it } from '@jest/globals'
import { hasCurrentQueryData } from '@/lib/query-observation'

describe('query observation authority', () => {
  const success = { data: { enabled: false }, dataUpdatedAt: 1000, fetchStatus: 'idle' }
  it('keeps a successful idle observation, including ordinary React Query staleness', () => {
    expect(hasCurrentQueryData({ ...success, ...{ isStale: true } })).toBe(true)
  })
  it.each([{ data: undefined }, { isError: true }, { failureCount: 1 }, { failureReason: new Error('unavailable') }, { isFetching: true }, { isPending: true }, { isPlaceholderData: true }, { fetchStatus: 'paused' }, { dataUpdatedAt: 0 }])('rejects an unavailable or provisional observation %#', patch => {
    expect(hasCurrentQueryData({ ...success, ...patch })).toBe(false)
  })
})
