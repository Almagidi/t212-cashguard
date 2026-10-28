import { z } from 'zod'
import type { PaperOperationMetadata } from '@/types'

// Pydantic preserves Decimal exponent notation (for example 0E-8); never coerce to Number.
const amount = z.string().regex(/^-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/)
const time = z.iso.datetime({ offset: true })
const id = z.uuid()
const metadata = {
  operation_id: id, operation_state: z.literal('recorded'), recorded_at: time,
  replayed: z.boolean(),
}
// Complete current backend OrderOut contract, with paper-only provenance.
const order = z.object({
  id, signal_id: id.nullable(), strategy_id: id.nullable(), strategy_name: z.string().nullable(),
  strategy_type_name: z.string().nullable(), client_order_key: z.string().min(1),
  ticker: z.string().min(1).max(50), side: z.enum(['buy', 'sell']), order_type: z.literal('market'),
  quantity: amount, limit_price: amount.nullable(), stop_price: amount.nullable(),
  time_validity: z.string().min(1), status: z.enum(['pending_intent', 'submitted', 'submission_unknown', 'accepted', 'partially_filled', 'filled', 'cancelled', 'rejected', 'error']),
  broker_order_id: z.null(), filled_quantity: amount.nullable(), remaining_quantity: amount,
  avg_fill_price: amount.nullable(), execution_environment: z.literal('paper_mock'),
  expected_fill_price: amount.nullable(), slippage_pct: amount.nullable(), slippage_value: amount.nullable(),
  fee_amount: amount.nullable(), submitted_at: time.nullable(), first_ack_at: time.nullable(),
  filled_at: time.nullable(), cancelled_at: time.nullable(), rejected_at: time.nullable(),
  broker_latency_ms: z.number().int().nullable(), fill_latency_ms: z.number().int().nullable(),
  reconciliation_latency_ms: z.number().int().nullable(), execution_quality_score: amount.nullable(),
  execution_quality_grade: z.string().nullable(), execution_quality_notes: z.record(z.string(), z.unknown()).nullable(),
  is_dry_run: z.literal(true), cash_used: amount.nullable(), error_message: z.string().nullable(),
  signal_reason: z.string().nullable(), signal_confidence: amount.nullable(),
  signal_risk_rejected: z.boolean().nullable(), signal_risk_rejection_reason: z.string().nullable(),
  retry_count: z.number().int(), last_reconciled_at: time.nullable(), created_at: time, updated_at: time,
})
const result = z.discriminatedUnion('outcome', [
  order.extend({ ...metadata, outcome: z.literal('order_recorded'), original_http_status: z.literal(201), decision_code: z.null(), detail: z.null() }),
  z.object({ ...metadata, outcome: z.literal('blocked'), original_http_status: z.number().int().min(400).max(499), decision_code: z.string().min(1), detail: z.string().min(1), id: z.undefined().optional(), status: z.undefined().optional(), order: z.null().optional() }),
])

export function recordedPaperResult(value: unknown, operation: string): PaperOperationMetadata | null {
  const parsed = result.safeParse(value)
  return parsed.success && parsed.data.operation_id === operation ? parsed.data : null
}
