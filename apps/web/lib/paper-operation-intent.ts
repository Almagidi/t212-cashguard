import type { CreatePaperOrderPayload } from '@/types'

const PREFIX = 'cg-paper-intent-v1:'
const CHANGED = 'cashguard-paper-intent-changed'
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
const FIELDS = new Set(['ticker', 'side', 'quantity', 'estimated_price', 'source', 'venue', 'paper_only', 'order_type'])
export type PaperIntent = {
  version: 1; ownerId: string; operationId: string; state: 'pending' | 'recorded' | 'unresolved'
  payload?: CreatePaperOrderPayload
}

function changed(): void {
  if (typeof window !== 'undefined') window.dispatchEvent(new Event(CHANGED))
}

export function subscribePaperIntents(notify: () => void): () => void {
  window.addEventListener(CHANGED, notify)
  window.addEventListener('storage', notify)
  return () => { window.removeEventListener(CHANGED, notify); window.removeEventListener('storage', notify) }
}

export function paperIntentSnapshot(owner: string | undefined): string | null {
  if (!owner || typeof window === 'undefined') return null
  try { return sessionStorage.getItem(PREFIX + owner) ?? '' } catch { return '!storage-unavailable' }
}

export function parsePaperIntentSnapshot(owner: string, raw: string): PaperIntent | null {
  return raw === '' ? null : validate(JSON.parse(raw), owner)
}

function validate(value: unknown, owner: string): PaperIntent {
  const row = value as PaperIntent | null
  if (!row || row.version !== 1 || row.ownerId !== owner || !UUID.test(row.operationId)
    || !['pending', 'recorded', 'unresolved'].includes(row.state)) throw new Error('Stored paper intent needs manual reconciliation')
  if (row.state === 'pending') validatePayload(row.payload)
  return row
}

function positivePaperDecimal(value: string): boolean {
  // Supported UI fixed-point input, losslessly bounded like Decimal(20, 8).
  const match = /^\+?(\d+)(?:\.(\d+))?$/.exec(value)
  if (!match) return false
  const whole = match[1].replace(/^0+/, '')
  const fraction = (match[2] ?? '').replace(/0+$/, '')
  return whole.length <= 12 && fraction.length <= 8 && /[1-9]/.test(whole + fraction)
}

function validatePayload(value: unknown): asserts value is CreatePaperOrderPayload {
  const row = value as CreatePaperOrderPayload | null
  if (!row || Object.keys(row).some(key => !FIELDS.has(key)) || typeof row.ticker !== 'string'
    || !['buy', 'sell'].includes(row.side) || typeof row.quantity !== 'string'
    || typeof row.estimated_price !== 'string' || row.source !== 'manual_demo_ui'
    || row.ticker.trim().length < 1 || Array.from(row.ticker).length > 50
    || !positivePaperDecimal(row.quantity) || !positivePaperDecimal(row.estimated_price)
    || !['paper', 'mock'].includes(row.venue ?? '') || row.paper_only !== true || row.order_type !== 'market') {
    throw new Error('Invalid paper input; no request was sent')
  }
}

export function readPaperIntent(owner: string, storage: Storage): PaperIntent | null {
  if (!UUID.test(owner)) throw new Error('Known authenticated owner required')
  const raw = storage.getItem(PREFIX + owner)
  return raw === null ? null : validate(JSON.parse(raw), owner)
}

export function createPaperIntent(owner: string, payload: CreatePaperOrderPayload, storage: Storage, generate = () => crypto.randomUUID()): PaperIntent {
  const prior = readPaperIntent(owner, storage)
  if (prior?.state === 'pending') return prior
  if (prior) throw new Error('Review the existing paper operation before a new order')
  validatePayload(payload)
  const operationId = generate()
  if (!UUID.test(operationId)) throw new Error('Secure operation identity unavailable')
  const record: PaperIntent = { version: 1, ownerId: owner, operationId, state: 'pending', payload: { ...payload } }
  storage.setItem(PREFIX + owner, JSON.stringify(record))
  changed()
  return record
}

export function recordPaperResult(owner: string, operation: string, storage: Storage): PaperIntent {
  const prior = readPaperIntent(owner, storage)
  if (!prior || prior.operationId !== operation) throw new Error('Paper operation identity changed; reconcile before continuing')
  const record: PaperIntent = { version: 1, ownerId: owner, operationId: operation, state: 'recorded' }
  storage.setItem(PREFIX + owner, JSON.stringify(record))
  changed()
  return record
}

export function newPaperIntent(owner: string, storage: Storage): void {
  if (readPaperIntent(owner, storage)?.state !== 'recorded') throw new Error('Unresolved paper result cannot be cleared')
  storage.removeItem(PREFIX + owner)
  changed()
}

export function scrubPaperIntentPayloads(storage: Storage): void {
  for (let index = 0; index < storage.length; index++) {
    const key = storage.key(index)
    if (!key?.startsWith(PREFIX)) continue
    try {
      const prior = readPaperIntent(key.slice(PREFIX.length), storage)
      if (prior) storage.setItem(key, JSON.stringify({ version: 1, ownerId: prior.ownerId, operationId: prior.operationId, state: prior.state === 'recorded' ? 'recorded' : 'unresolved' }))
    } catch {
      // Retain a fail-closed marker, not an unvalidated/possibly sensitive payload.
      storage.setItem(key, JSON.stringify({ version: 1, state: 'invalid' }))
    }
  }
  changed()
}

export { recordedPaperResult } from './paper-operation-result'
