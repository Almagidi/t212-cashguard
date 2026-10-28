export const orderId = '00000000-0000-4000-8000-000000000002'
export function recorded(operation: string, replayed = false) {
  return {
    operation_id: operation, operation_state: 'recorded', outcome: 'order_recorded',
    original_http_status: 201, recorded_at: '2026-10-28T00:00:00Z', replayed,
    decision_code: null, detail: null,
    id: orderId, status: 'filled', signal_id: null, strategy_id: null,
    strategy_name: null, strategy_type_name: null, client_order_key: 'paper-fixture',
    ticker: 'EXAMPLE', side: 'buy', order_type: 'market', quantity: '1',
    limit_price: null, stop_price: null, time_validity: 'DAY', broker_order_id: null,
    filled_quantity: '1', remaining_quantity: '0', avg_fill_price: '100.1',
    execution_environment: 'paper_mock', expected_fill_price: '100', slippage_pct: '0.1',
    slippage_value: '0.1', fee_amount: '0.02002', submitted_at: null, first_ack_at: null,
    filled_at: null, cancelled_at: null, rejected_at: null, broker_latency_ms: null,
    fill_latency_ms: null, reconciliation_latency_ms: null, execution_quality_score: null,
    execution_quality_grade: null, execution_quality_notes: null, is_dry_run: true,
    cash_used: '100.12002', error_message: null, signal_reason: null, signal_confidence: null,
    signal_risk_rejected: null, signal_risk_rejection_reason: null, retry_count: 0,
    last_reconciled_at: null, created_at: '2026-10-28T00:00:00Z', updated_at: '2026-10-28T00:00:00Z',
  }
}
export function blocked(operation: string) {
  return { operation_id: operation, operation_state: 'recorded', outcome: 'blocked', original_http_status: 422, recorded_at: '2026-10-28T00:00:00Z', replayed: false, decision_code: 'kill_switch_block', detail: 'Kill switch active' }
}
