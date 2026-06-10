import { test, expect } from '@playwright/test'

const testUser = {
  id: '00000000-0000-0000-0000-000000000001',
  email: 'admin@localhost',
  is_active: true,
  is_admin: true,
  created_at: '2026-01-01T00:00:00Z',
}

const operatorStatus = {
  subsystem: 'operator',
  mode: 'read_only_status',
  generated_at: '2026-05-05T09:30:00Z',
  overall_status: 'degraded',
  live_trading_possible: false,
  live_trading_enabled_anywhere: false,
  venues: [
    {
      venue: 't212',
      present: true,
      kill_switch_active: true,
      auto_trading_enabled: false,
      degraded_mode_active: false,
      note: 'Trading212 venue kill switch is active.',
      updated_at: '2026-05-05T09:00:00Z',
    },
    {
      venue: 'kraken',
      present: true,
      kill_switch_active: false,
      auto_trading_enabled: false,
      degraded_mode_active: true,
      note: 'Kraken degraded mode active.',
      updated_at: '2026-05-05T09:05:00Z',
    },
  ],
  trading212: {
    strategies_count: 5,
    live_approved_strategies_count: 1,
    active_orders_count: 0,
    recent_orders_count: 2,
    latest_order_status: 'filled',
    live_readiness_status: null,
    safety_notes: ['Trading212 summary uses persisted local state only.'],
  },
  kraken: {
    strategies_count: 4,
    paper_only_strategies_count: 4,
    live_enabled: false,
    recent_orders_count: 0,
    active_orders_count: 0,
    venue_config: null,
    safety_notes: ['Kraken live execution remains disabled/unproven.'],
  },
  dca: {
    config_count: 2,
    enabled_config_count: 1,
    decision_count_total: 9,
    buy_due_count: 2,
    blocked_count: 5,
    skipped_count: 2,
    total_paper_allocated_usd: '125.50',
    scheduler_registered: true,
    scheduler_cadence: 'daily at 09:00',
    worker_health: 'missing',
    runnable: false,
    live_enabled: false,
    paper_only: true,
    tickers: ['BTC/USD', 'ETH/USD'],
  },
  paper_execution: {
    paper_only: true,
    enabled_in_mode: 'mock',
    total_paper_orders: 3,
    latest_paper_order_timestamp: '2026-05-05T09:25:00Z',
    last_paper_execution_status: 'filled',
    open_paper_positions_count: 1,
    safety_notes: [
      'Paper execution is local/mock only.',
      'No broker order sent.',
      'Global kill switch blocks paper simulation in this endpoint.',
    ],
  },
  schedulers: {
    dca_paper_evaluate_registered: true,
    dca_paper_evaluate_cadence: 'daily at 09:00',
    heartbeat_registered: true,
    heartbeat_cadence: 'every 60 seconds',
    worker_health: 'missing',
    heartbeat_component: 'celery-worker',
    heartbeat_last_seen_at: null,
    heartbeat_stale_after_seconds: 180,
  },
  recent_activity: [
    {
      id: 'activity-1',
      occurred_at: '2026-05-05T09:20:00Z',
      action: 'dca_paper_decision',
      entity_type: 'dca_config',
      entity_id: 'config-1',
      actor: 'system',
      payload_summary: {
        ticker: 'BTC/USD',
        decision_code: 'BUY_DUE',
        safe_summary: 'Paper decision recorded',
        api_key: 'should-not-render',
      },
    },
  ],
  safety_flags: {
    endpoint_read_only: true,
    creates_orders: false,
    calls_brokers: false,
    triggers_schedulers: false,
    runs_strategies: false,
    dca_runnable: false,
    dca_live_enabled: false,
    kraken_live_enabled: false,
    cash_only_mode: true,
    live_trading_enabled_setting: false,
    app_live_trading_unlocked: false,
    any_venue_kill_switch_active: true,
    any_venue_degraded: true,
    missing_expected_venue_configs: false,
    worker_health_known: false,
  },
}

const settings = {
  id: 1,
  theme: 'dark',
  timezone: 'Europe/London',
  market_data_provider: 'mock',
  auto_trading_enabled: false,
  kill_switch_active: true,
  live_trading_unlocked: false,
  daily_stats_reset_time: '00:00',
  updated_at: '2026-05-05T09:00:00Z',
}

const dcaStatus = {
  subsystem: 'kraken_dca',
  mode: 'paper_only',
  runnable: false,
  live_enabled: false,
  scheduler_registered: true,
  scheduler_cadence: 'daily at 09:00 UTC',
  config_count: 2,
  enabled_config_count: 1,
  configs: [],
  recent_audit_entries: [],
  safety_flags: {
    dca_planner_runnable_is_false: true,
    dca_planner_paper_only_is_true: true,
    main_runner_registered: false,
    order_creation_supported: false,
  },
}

const dcaActivity = {
  subsystem: 'kraken_dca',
  mode: 'paper_only',
  runnable: false,
  live_enabled: false,
  generated_at: '2026-05-05T09:30:00Z',
  config_count: 2,
  enabled_config_count: 1,
  decision_count_total: 9,
  decision_counts_by_code: { BUY_DUE: 2 },
  buy_due_count: 2,
  blocked_count: 5,
  skipped_count: 2,
  total_paper_allocated_usd: '125.50',
  order_count_sanity: 0,
  configs: [
    {
      id: 'config-1',
      ticker: 'BTC/USD',
      venue: 'kraken',
      enabled: true,
      paper_only: true,
      cadence_days: 7,
      fixed_cash_amount: '50.00',
      max_position_percent: '10.00',
    },
  ],
  per_ticker_activity: [],
  recent_decisions: [],
  safety_flags: {
    dca_planner_runnable_is_false: true,
    dca_planner_paper_only_is_true: true,
    main_runner_registered: false,
    order_creation_supported: false,
    execution_called_by_report: false,
    provider_called_by_report: false,
    scheduler_triggered_by_report: false,
  },
}

const dcaConfigs = [
  {
    id: 'config-1',
    ticker: 'BTC/USD',
    venue: 'kraken',
    cadence_days: 7,
    fixed_cash_amount: '50.00',
    dip_buy_enabled: false,
    dip_buy_multiplier: '1.00',
    min_cash_reserve: '100.00',
    max_position_percent: '10.00',
    paper_only: true,
    enabled: true,
    created_at: '2026-05-05T09:00:00Z',
    updated_at: '2026-05-05T09:00:00Z',
  },
]

const paperExecutionHistory = {
  total: 1,
  limit: 25,
  items: [
    {
      id: '11111111-1111-4111-8111-111111111111',
      order_id: '22222222-2222-4222-8222-222222222222',
      created_at: '2026-05-05T09:26:00Z',
      updated_at: '2026-05-05T09:26:02Z',
      ticker: 'PAPERXYZ',
      side: 'buy',
      quantity: '2.00000000',
      notional: '51.00000000',
      venue: 'paper',
      source: 'test_signal',
      strategy: 'paper-test',
      status: 'filled',
      risk_result: 'allowed',
      fill_price: '25.50000000',
      filled_quantity: '2.00000000',
      paper_only: true,
      live_order_sent: false,
      no_broker_order_sent: true,
      rejection_reason: null,
      audit_count: 3,
      latest_audit_at: '2026-05-05T09:26:03Z',
    },
  ],
}

test.describe('Operator dashboard readiness', () => {
  test.skip(
    (process.env.NEXT_PUBLIC_APP_MODE ?? 'mock') !== 'mock',
    'Operator dashboard readiness E2E is mock-mode only.',
  )

  test('loads read-only operator and DCA readiness without triggering mutations', async ({ page }) => {
    const mutations: string[] = []

    await page.route('**/*', async (route) => {
      const request = route.request()
      const url = request.url()
      const method = request.method()

      if (!url.includes('/v1/') && !url.includes('/api/v1/')) {
        await route.continue()
        return
      }

      if (method !== 'GET') {
        mutations.push(`${method} ${url}`)
        await route.fulfill({ status: 405, body: 'Mutations disabled in mock readiness E2E' })
        return
      }

      const path = new URL(url).pathname.replace(/^\/api/, '')
      const bodyForPath: Record<string, unknown> = {
        '/v1/auth/me': testUser,
        '/v1/operator/status': operatorStatus,
        '/v1/broker/trading212/status': null,
        '/v1/settings': settings,
        '/v1/orders': [],
        '/v1/positions': [],
        '/v1/alerts': [],
        '/v1/account/summary': {
          total_value: 10000,
          cash: 10000,
          free_funds: 10000,
          invested: 0,
          result: 0,
          currency: 'GBP',
          synced_at: '2026-05-05T09:00:00Z',
          mode: 'mock',
        },
        '/v1/health/live': {
          status: 'ok',
          timestamp: '2026-05-05T09:00:00Z',
          version: 'test',
          mode: 'mock',
        },
        '/v1/health/deps': {
          database: 'ok',
          redis: 'ok',
          broker: 'mock',
          market_data: 'mock',
        },
        '/v1/kraken/dca/status': dcaStatus,
        '/v1/kraken/dca/activity': dcaActivity,
        '/v1/kraken/dca/configs': dcaConfigs,
        '/v1/orders/paper': paperExecutionHistory,
      }

      if (path in bodyForPath) {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify(bodyForPath[path]),
        })
        return
      }

      await route.fulfill({
        status: 404,
        contentType: 'application/json',
        body: JSON.stringify({ detail: `Unexpected mock API request: ${path}` }),
      })
    })

    await page.goto('/auth/login')
    await page.evaluate(() => localStorage.setItem('cg_token', 'mock-readiness-token'))
    await page.goto('/app/operator')

    await expect(page.locator('.badge-mock').first()).toBeVisible({ timeout: 10_000 })
    await expect(page.getByRole('heading', { name: 'Runtime Diagnostics' })).toBeVisible()
    await expect(page.getByText('Frontend mock')).toBeVisible()
    await expect(page.getByText('Backend mock')).toBeVisible()
    await expect(page.getByText('API URL')).toBeVisible()
    await expect(page.getByText('Operator /v1/operator/status')).toBeVisible()
    await expect(page.getByText('DCA /v1/kraken/dca/status')).toBeVisible()
    await expect(page.getByText('Kraken/DCA readiness data is available.')).toBeVisible()
    await expect(page.getByRole('heading', { name: 'Read-only Operator Dashboard' })).toBeVisible()
    await expect(page.getByRole('heading', { name: 'Trading212 Summary' })).toBeVisible()
    await expect(page.getByRole('heading', { name: 'Kraken Summary' })).toBeVisible()
    await expect(page.getByRole('heading', { name: 'DCA Summary' })).toBeVisible()
    await expect(page.getByRole('heading', { name: 'Paper Execution', exact: true })).toBeVisible()
    await expect(page.getByRole('heading', { name: 'Paper Execution History' })).toBeVisible()
    await expect(page.getByText('PAPERXYZ')).toBeVisible()
    await expect(page.getByText('test_signal')).toBeVisible()
    await expect(page.getByText('paper-test')).toBeVisible()
    await expect(page.getByText('Paper only').first()).toBeVisible()
    await expect(page.getByText('No broker order sent').first()).toBeVisible()
    await expect(page.getByText('Mock execution').first()).toBeVisible()
    await expect(page.getByText('Paper-only').first()).toBeVisible()
    await expect(page.getByText('Live disabled').first()).toBeVisible()

    // Execution boundary — the operator page is visibility-only and must not expose execution paths.
    await expect(page.getByTestId('operator-execution-boundary')).toBeVisible()
    await expect(page.getByTestId('operator-read-only-badge')).toContainText('Read-only endpoint')
    await expect(page.getByTestId('operator-no-broker-order-badge')).toContainText('No broker order sent')
    await expect(page.getByTestId('operator-live-disabled-badge')).toContainText(/Live locked|Live state needs review/)
    await expect(page.getByTestId('operator-execution-boundary')).toContainText('Creates orders')
    await expect(page.getByTestId('operator-execution-boundary')).toContainText('Calls brokers')
    await expect(page.getByTestId('operator-execution-boundary')).toContainText('Triggers schedulers')
    await expect(page.getByTestId('operator-execution-boundary')).toContainText('Runs strategies')

    // Safety flags — every flag from the API must render, including the
    // live-trading lock state (env setting + app unlock) and missing
    // venue config visibility.
    const safetyFlags = page.getByTestId('operator-safety-flags')
    await expect(safetyFlags).toBeVisible()
    const expectedSafetyFlagLabels = [
      'Endpoint read-only',
      'Creates orders',
      'Calls brokers',
      'Triggers schedulers',
      'Runs strategies',
      'DCA runnable',
      'DCA live enabled',
      'Kraken live enabled',
      'Live trading enabled (env setting)',
      'Live trading unlocked (app)',
      'Expected venue configs missing',
      'Any venue kill switch active',
      'Any venue degraded',
      'Worker health known',
      'Cash-only mode',
    ]
    for (const label of expectedSafetyFlagLabels) {
      await expect(safetyFlags).toContainText(label)
    }

    await expect(page.getByText('Worker heartbeat missing')).toBeVisible()
    await expect(page.getByText('Endpoint read-only')).toBeVisible()
    await expect(page.getByTestId('operator-execution-boundary')).toContainText('Creates orders')
    await expect(page.getByTestId('operator-execution-boundary')).toContainText('Calls brokers')
    await expect(page.getByText('BTC/USD').first()).toBeVisible()
    await expect(page.getByText('should-not-render')).toHaveCount(0)
    await expect(page.getByRole('button', { name: /enable|disable|execute|trade|buy|sell/i })).toHaveCount(0)
    expect(mutations).toEqual([])
  })
})
