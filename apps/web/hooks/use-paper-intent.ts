'use client'
import { useMemo, useRef, useState, useSyncExternalStore } from 'react'
import type { CreatePaperOrderPayload, Order } from '@/types'
import { api } from '@/services/api'
import { createPaperIntent, newPaperIntent, paperIntentSnapshot, parsePaperIntentSnapshot, readPaperIntent, recordedPaperResult, recordPaperResult, subscribePaperIntents } from '@/lib/paper-operation-intent'
import type { PaperIntent } from '@/lib/paper-operation-intent'

export interface PaperIntentMessage {
  tone: 'success' | 'blocked' | 'unknown'; title: string; detail: string; orderId?: string
}
const unknown: PaperIntentMessage = { tone: 'unknown', title: 'Paper result unconfirmed', detail: 'Check the existing receipt or retry the same saved operation. Do not create another order to resolve an uncertain result.' }
const unavailable: PaperIntentMessage = { tone: 'unknown', title: 'Saved paper operation unavailable', detail: 'No request sent. Storage or malformed saved input needs manual ledger and technical reconciliation.' }

export function usePaperIntent(owner: string | undefined, send: (payload: CreatePaperOrderPayload) => Promise<unknown>) {
  const raw = useSyncExternalStore(subscribePaperIntents, () => paperIntentSnapshot(owner), () => null)
  const stored = useMemo(() => {
    if (!owner || raw === null) return { ready: false, intent: null, invalid: false }
    try { return { ready: true, intent: parsePaperIntentSnapshot(owner, raw), invalid: false } }
    catch { return { ready: false, intent: null, invalid: true } }
  }, [owner, raw])
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<{ owner: string; value: PaperIntentMessage } | null>(null)
  const submitting = useRef(false)
  const intent = stored.intent
  const result = message && message.owner === owner ? message.value : stored.invalid ? unavailable : intent
    ? intent.state === 'recorded'
      ? { tone: 'unknown' as const, title: 'Paper operation already recorded', detail: 'Check its receipt for the current recorded status. Choose New Paper Order only for a genuinely new intent.' }
      : unknown
    : null

  const accept = (value: unknown, saved: PaperIntent) => {
    const known = recordedPaperResult(value, saved.operationId)
    if (!known) throw new Error('No confirmed recorded receipt')
    recordPaperResult(saved.ownerId, saved.operationId, sessionStorage)
    const order = value as Order
    setMessage({ owner: saved.ownerId, value: known.outcome === 'blocked'
      ? { tone: 'blocked', title: 'Paper order blocked by safety controls', detail: `${known.detail} No broker order was sent.` }
      : { tone: 'success', title: known.replayed ? 'Paper order already recorded' : `Paper order ${order.status.replaceAll('_', ' ')}`, detail: `${known.replayed ? 'No new paper execution. ' : ''}Recorded status: ${order.status}. No broker order sent.`, orderId: order.id } })
  }

  const run = async (payload?: CreatePaperOrderPayload) => {
    if (!owner || !stored.ready || submitting.current) return
    submitting.current = true
    setBusy(true)
    let saved: PaperIntent | null = null
    let dispatched = false
    try {
      saved = payload ? createPaperIntent(owner, payload, sessionStorage) : readPaperIntent(owner, sessionStorage)
      if (!saved) throw new Error('No operation to reconcile')
      if (payload) {
        if (saved.state !== 'pending' || !saved.payload) throw new Error('Unresolved operation requires receipt review')
        dispatched = true
        accept(await send({ ...saved.payload, operation_id: saved.operationId, expected_owner_id: saved.ownerId }), saved)
      } else {
        const receipt = await api.getPaperOperation(saved.operationId, saved.ownerId)
        accept({ ...(receipt.order ?? {}), ...receipt }, saved)
      }
    } catch (error) {
      const response = (error as { response?: { data?: unknown; status?: number } })?.response
      try {
        if (!saved) throw error
        accept(response?.data, saved)
      } catch {
        setMessage({ owner, value: !saved && !dispatched
          ? { tone: 'unknown', title: 'Paper input or storage needs attention', detail: 'No request was sent. Check the ticker, positive Decimal quantity and available browser storage.' }
          : response?.status === 422
            ? { tone: 'unknown', title: 'Paper request needs manual reconciliation', detail: 'Validation failed without a confirmed receipt. Keep this operation ID; manual ledger and technical review are required. Do not create another intent.' }
            : unknown })
      }
    } finally { submitting.current = false; setBusy(false) }
  }

  const startNew = () => {
    if (!owner || submitting.current || !stored.ready) return
    try { newPaperIntent(owner, sessionStorage); setMessage(null) }
    catch { setMessage({ owner, value: unknown }) }
  }
  return { intent, result, busy, ready: stored.ready, submit: (payload: CreatePaperOrderPayload) => run(payload), check: () => run(), startNew }
}
