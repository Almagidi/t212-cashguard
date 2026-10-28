import { beforeEach, test, expect, jest } from '@jest/globals'
import { createPaperIntent, readPaperIntent, recordedPaperResult, recordPaperResult, scrubPaperIntentPayloads } from '@/lib/paper-operation-intent'
import { recorded, blocked } from './fixtures/paper-operation'

const owner = '00000000-0000-4000-8000-000000000001'
const other = '00000000-0000-4000-8000-000000000002'
const id = '00000000-0000-4000-8000-000000000003'
const payload = { ticker: 'EXAMPLE', side: 'buy' as const, quantity: '1', estimated_price: '100', source: 'manual_demo_ui', venue: 'paper' as const, paper_only: true as const, order_type: 'market' as const }

beforeEach(() => sessionStorage.clear())

test('one stored identity survives repeated handlers and reload reads', () => {
  const generate = jest.fn(() => id)
  const first = createPaperIntent(owner, payload, sessionStorage, generate)
  expect(createPaperIntent(owner, { ...payload, quantity: '2' }, sessionStorage, generate)).toEqual(first)
  expect(readPaperIntent(owner, sessionStorage)).toEqual(first)
  expect(generate).toHaveBeenCalledTimes(1)
  expect(readPaperIntent(other, sessionStorage)).toBeNull()
})

test('terminal tombstone removes financial input without silently creating a new intent', () => {
  createPaperIntent(owner, payload, sessionStorage, () => id)
  recordPaperResult(owner, id, sessionStorage)
  expect(readPaperIntent(owner, sessionStorage)).toEqual({ version: 1, ownerId: owner, operationId: id, state: 'recorded' })
  expect(JSON.stringify(sessionStorage)).not.toContain('EXAMPLE')
  expect(() => createPaperIntent(owner, payload, sessionStorage, () => id)).toThrow()
})

test('logout removes payload, retains unresolved reference and cannot submit it as a new operation', () => {
  createPaperIntent(owner, payload, sessionStorage, () => id)
  scrubPaperIntentPayloads(sessionStorage)
  expect(readPaperIntent(owner, sessionStorage)).toEqual({ version: 1, ownerId: owner, operationId: id, state: 'unresolved' })
  expect(JSON.stringify(sessionStorage)).not.toContain('EXAMPLE')
  expect(() => createPaperIntent(owner, payload, sessionStorage, () => id)).toThrow()
})

test('storage failure and unrecognized request fields fail before submission', () => {
  const denied = { getItem: () => null, setItem: () => { throw new Error('storage denied') } } as unknown as Storage
  expect(() => createPaperIntent(owner, payload, denied, () => id)).toThrow('storage denied')
  expect(() => createPaperIntent(owner, { ...payload, authorization: 'synthetic-sensitive-field' } as typeof payload, sessionStorage, () => id)).toThrow()
})

test.each(['', ' ', '1e999', '-1', '0', '0.000000001', '1000000000000', 'NaN'])('invalid quantity %s never allocates an identity', quantity => {
  const generate = jest.fn(() => id)
  expect(() => createPaperIntent(owner, { ...payload, quantity }, sessionStorage, generate)).toThrow()
  expect(generate).not.toHaveBeenCalled()
  expect(readPaperIntent(owner, sessionStorage)).toBeNull()
})

test.each([' ', 'X'.repeat(51)])('invalid ticker cannot strand a new intent', ticker => {
  expect(() => createPaperIntent(owner, { ...payload, ticker }, sessionStorage, () => id)).toThrow()
  expect(readPaperIntent(owner, sessionStorage)).toBeNull()
})

test('only a complete discriminated recorded result establishes a terminal receipt', () => {
  expect(recordedPaperResult(recorded(id), id)).not.toBeNull()
  expect(recordedPaperResult({ ...recorded(id), remaining_quantity: '0E-8' }, id)).not.toBeNull()
  expect(recordedPaperResult(blocked(id), id)).not.toBeNull()
  expect(recordedPaperResult({ ...blocked(id), original_http_status: 422.5 }, id)).toBeNull()
  expect(recordedPaperResult({ ...blocked(id), status: 'filled' }, id)).toBeNull()
  for (const field of ['updated_at', 'quantity', 'decision_code', 'remaining_quantity', 'is_dry_run']) {
    const incomplete: Record<string, unknown> = recorded(id)
    delete incomplete[field]
    expect(recordedPaperResult(incomplete, id)).toBeNull()
  }
  expect(recordedPaperResult({ ...recorded(id), status: 'future-unverified-state' }, id)).toBeNull()
  expect(recordedPaperResult(recorded(id), other)).toBeNull()
})
