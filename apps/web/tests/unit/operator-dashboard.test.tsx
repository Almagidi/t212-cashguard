import "@testing-library/jest-dom";

import { beforeEach, describe, expect, it, jest } from "@jest/globals";
import { render, screen, within } from "@testing-library/react";

import type {
  DemoReconciliationSchedulerStatus,
  OperatorStatus,
  OperatorWorkerHealth,
  PaperExecutionHistory,
} from "@/types";

const mockGet = jest.fn<(url: string, config?: unknown) => Promise<{ data: unknown }>>();
const mockPost = jest.fn();
const mockPatch = jest.fn();
const mockPut = jest.fn();
const mockDelete = jest.fn();
let paperHistoryState: {
  data: PaperExecutionHistory | undefined;
  isLoading: boolean;
  isError: boolean;
} = {
  data: undefined,
  isLoading: false,
  isError: false,
};
let demoSchedulerState: {
  data: DemoReconciliationSchedulerStatus | undefined;
  isLoading: boolean;
  error: unknown;
} = {
  data: undefined,
  isLoading: false,
  error: null,
};
let cashGuardState: {
  data:
    | {
        available_to_trade: number;
        reserved: number;
        total_cash: number;
        cash_only_mode: boolean;
        currency: string;
      }
    | undefined;
  isLoading: boolean;
  isError: boolean;
} = {
  data: {
    available_to_trade: 4285.0,
    reserved: 215.0,
    total_cash: 4500.0,
    cash_only_mode: true,
    currency: "USD",
  },
  isLoading: false,
  isError: false,
};

jest.mock("@/hooks/use-api", () => ({
  __esModule: true,
  useBrokerStatus: () => ({
    data: null,
    isLoading: false,
    error: null,
  }),
  useCashGuard: () => cashGuardState,
  useDemoReconciliationStatus: () => ({
    data: null,
    isLoading: false,
    error: null,
  }),
  useDemoReconciliationSchedulerStatus: () => demoSchedulerState,
  useDcaActivity: () => ({
    data: null,
    isLoading: false,
    error: null,
  }),
  usePaperExecutionHistory: () => paperHistoryState,
}));

jest.mock("axios", () => ({
  __esModule: true,
  default: {
    create: jest.fn(() => ({
      get: mockGet,
      post: mockPost,
      patch: mockPatch,
      put: mockPut,
      delete: mockDelete,
      interceptors: {
        request: { use: jest.fn() },
        response: { use: jest.fn() },
      },
    })),
    get: jest.fn(),
  },
}));

const { OperatorDashboard } =
  require("@/components/operator/operator-dashboard") as typeof import("@/components/operator/operator-dashboard");

function operatorStatus(
  overrides: Partial<OperatorStatus> = {},
): OperatorStatus {
  const base: OperatorStatus = {
    subsystem: "operator",
    mode: "read_only_status",
    generated_at: "2026-05-01T09:30:00Z",
    overall_status: "degraded",
    why_blocked: [
      {
        code: "kill_switch_active",
        severity: "blocked",
        message: "A venue kill switch is active. Trading is blocked until it is cleared.",
      },
      {
        code: "venue_degraded",
        severity: "degraded",
        message: "At least one venue is reporting degraded mode.",
      },
    ],
    protective_stops: {
      status: "ok",
      global_kill_switch_active: false,
      global_auto_trading_enabled: false,
      last_kill_switch_event: null,
      recent_events: [],
      safety_notes: [
        "Read-only surface. No reset, clear, enable, or disable controls exist here.",
      ],
    },
    live_trading_possible: false,
    live_trading_enabled_anywhere: false,
    venues: [
      {
        venue: "t212",
        present: true,
        kill_switch_active: true,
        auto_trading_enabled: false,
        degraded_mode_active: false,
        note: "Trading212 venue kill switch is active.",
        updated_at: "2026-05-01T09:00:00Z",
      },
      {
        venue: "kraken",
        present: true,
        kill_switch_active: false,
        auto_trading_enabled: false,
        degraded_mode_active: true,
        note: "Kraken degraded mode active.",
        updated_at: "2026-05-01T09:05:00Z",
      },
    ],
    trading212: {
      strategies_count: 5,
      live_approved_strategies_count: 1,
      active_orders_count: 0,
      recent_orders_count: 2,
      latest_order_status: "filled",
      live_readiness_status: null,
      safety_notes: ["Trading212 summary uses persisted local state only."],
    },
    kraken: {
      strategies_count: 4,
      paper_only_strategies_count: 4,
      live_enabled: false,
      recent_orders_count: 0,
      active_orders_count: 0,
      venue_config: null,
      safety_notes: ["Kraken live execution remains disabled/unproven."],
    },
    dca: {
      config_count: 2,
      enabled_config_count: 1,
      decision_count_total: 9,
      buy_due_count: 2,
      blocked_count: 5,
      skipped_count: 2,
      total_paper_allocated_usd: "125.50",
      scheduler_registered: true,
      scheduler_cadence: "daily at 09:00",
      worker_health: "missing",
      runnable: false,
      live_enabled: false,
      paper_only: true,
      tickers: ["BTC/USD", "ETH/USD"],
    },
    paper_execution: {
      paper_only: true,
      enabled_in_mode: "mock",
      total_paper_orders: 3,
      latest_paper_order_timestamp: "2026-05-01T09:25:00Z",
      last_paper_execution_status: "filled",
      open_paper_positions_count: 1,
      safety_notes: [
        "Paper execution is local/mock only.",
        "No broker order sent.",
        "Global kill switch blocks paper simulation in this endpoint.",
      ],
    },
    schedulers: {
      dca_paper_evaluate_registered: true,
      dca_paper_evaluate_cadence: "daily at 09:00",
      heartbeat_registered: true,
      heartbeat_cadence: "every 60 seconds",
      worker_health: "missing",
      heartbeat_component: "celery-worker",
      heartbeat_last_seen_at: null,
      heartbeat_stale_after_seconds: 180,
      strategy_signals_registered: true,
      strategy_signals_cadence: "300.0",
      strategy_signals_task_name: "app.workers.tasks.run_strategy_signals",
      strategy_signals_observation_status: "unknown",
      strategy_signals_last_seen_at: null,
      strategy_signals_observation_detail: "Task heartbeat has not been recorded yet.",
    },
    recent_activity: [
      {
        id: "activity-1",
        occurred_at: "2026-05-01T09:20:00Z",
        action: "dca_paper_decision",
        entity_type: "dca_config",
        entity_id: "config-1",
        actor: "system",
        payload_summary: {
          ticker: "BTC/USD",
          decision_code: "BUY_DUE",
          safe_summary: "Paper decision recorded",
          api_key: "should-not-render",
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
      unrealized_pnl_failure_policy: "block_trading",
      credentials_configured: true,
      credential_source: "mock",
    },
  };

  return { ...base, ...overrides };
}

function withStrategySignalsScheduler(
  overrides: {
    strategy_signals_registered?: boolean;
    strategy_signals_cadence?: string | null;
    strategy_signals_task_name?: string;
    strategy_signals_observation_status?: "ok" | "stale" | "unknown";
    strategy_signals_last_seen_at?: string | null;
    strategy_signals_observation_detail?: string;
  } = {},
): OperatorStatus {
  const status = operatorStatus();
  return {
    ...status,
    schedulers: {
      ...status.schedulers,
      strategy_signals_cadence: "300.0",
      strategy_signals_observation_status: "ok",
      strategy_signals_last_seen_at: "2026-05-01T09:28:00Z",
      strategy_signals_observation_detail: "Task heartbeat observed recently.",
      ...overrides,
    },
  };
}

function withWorkerHealth(workerHealth: OperatorWorkerHealth): OperatorStatus {
  const status = operatorStatus();
  return {
    ...status,
    dca: { ...status.dca, worker_health: workerHealth },
    schedulers: { ...status.schedulers, worker_health: workerHealth },
    safety_flags: {
      ...status.safety_flags,
      worker_health_known: workerHealth === "healthy",
    },
  };
}

function paperHistory(
  overrides: Partial<PaperExecutionHistory> = {},
): PaperExecutionHistory {
  return {
    total: 1,
    limit: 25,
    items: [
      {
        id: "11111111-1111-4111-8111-111111111111",
        order_id: "22222222-2222-4222-8222-222222222222",
        created_at: "2026-05-01T09:26:00Z",
        updated_at: "2026-05-01T09:26:02Z",
        ticker: "PAPERXYZ",
        side: "buy",
        quantity: "2.00000000",
        notional: "51.00000000",
        venue: "paper",
        source: "test_signal",
        strategy: "paper-test",
        status: "filled",
        risk_result: "allowed",
        fill_price: "25.50000000",
        filled_quantity: "2.00000000",
        paper_only: true,
        live_order_sent: false,
        no_broker_order_sent: true,
        rejection_reason: null,
        audit_count: 3,
        latest_audit_at: "2026-05-01T09:26:03Z",
      },
    ],
    ...overrides,
  };
}

function setPaperHistory(state: Partial<typeof paperHistoryState>) {
  paperHistoryState = {
    data: paperHistory(),
    isLoading: false,
    isError: false,
    ...state,
  };
}

function schedulerStatus(
  overrides: Partial<DemoReconciliationSchedulerStatus> = {},
): DemoReconciliationSchedulerStatus {
  return {
    enabled: false,
    running: false,
    app_mode: "demo",
    broker_environment: "demo",
    live_trading_enabled: false,
    worker_enabled: true,
    interval_seconds: 120,
    backoff_seconds: 300,
    initial_delay_seconds: 10,
    run_on_startup: false,
    last_run_started_at: null,
    last_run_finished_at: null,
    last_run_duration_ms: null,
    last_run_outcome: null,
    last_run_summary: null,
    next_run_at: null,
    next_run_not_before: null,
    consecutive_failures: 0,
    consecutive_rate_limits: 0,
    total_runs: 0,
    total_successful_runs: 0,
    total_failed_runs: 0,
    total_rate_limited_runs: 0,
    last_error_message: null,
    safety_state: "safe",
    warnings: [],
    no_broker_order_sent: true,
    read_only_broker_calls: true,
    ...overrides,
  };
}

function setSchedulerStatus(state: Partial<typeof demoSchedulerState>) {
  demoSchedulerState = {
    data: schedulerStatus(),
    isLoading: false,
    error: null,
    ...state,
  };
}

function setCashGuard(state: Partial<typeof cashGuardState>) {
  cashGuardState = {
    data: {
      available_to_trade: 4285.0,
      reserved: 215.0,
      total_cash: 4500.0,
      cash_only_mode: true,
      currency: "USD",
    },
    isLoading: false,
    isError: false,
    ...state,
  };
}

describe("OperatorDashboard", () => {
  beforeEach(() => {
    setPaperHistory({});
    setSchedulerStatus({});
    setCashGuard({});
  });

  it("renders loading state", () => {
    render(<OperatorDashboard isLoading />);

    expect(screen.getByLabelText("Loading operator status")).toBeTruthy();
    expect(screen.getByText("Loading operator status...")).toBeTruthy();
  });

  it("renders fetched operator status with paper-only and live-disabled badges", () => {
    render(<OperatorDashboard status={operatorStatus()} />);

    expect(
      screen.getByRole("heading", { name: "Read-only Operator Dashboard" }),
    ).toBeTruthy();
    expect(screen.getAllByText("Paper-only").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Live disabled").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Read-only").length).toBeGreaterThan(0);
  });

  it.each<OperatorWorkerHealth>(["missing", "stale", "healthy", "unknown"])(
    "displays worker_health %s truthfully",
    (workerHealth) => {
      render(<OperatorDashboard status={withWorkerHealth(workerHealth)} />);

      expect(screen.getByText(`Worker heartbeat ${workerHealth}`)).toBeTruthy();
    },
  );

  it("renders execution boundary safety invariants", () => {
    paperHistoryState = {
      data: paperHistory(),
      isLoading: false,
      isError: false,
    };

    render(<OperatorDashboard status={operatorStatus()} />);

    const boundary = screen.getByTestId("operator-execution-boundary");

    expect(boundary).toBeInTheDocument();
    expect(screen.getByTestId("operator-read-only-badge")).toHaveTextContent("Read-only endpoint");
    expect(screen.getByTestId("operator-no-broker-order-badge")).toHaveTextContent("No broker order sent");
    expect(screen.getByTestId("operator-live-disabled-badge")).toHaveTextContent("Live locked");

    expect(within(boundary).getByText("Creates orders")).toBeInTheDocument();
    expect(within(boundary).getByText("Calls brokers")).toBeInTheDocument();
    expect(within(boundary).getByText("Triggers schedulers")).toBeInTheDocument();
    expect(within(boundary).getByText("Runs strategies")).toBeInTheDocument();
    expect(boundary).toHaveTextContent("Local/mock only");
    expect(boundary).toHaveTextContent("Cash-only mode");
    expect(boundary).toHaveTextContent("Live trading possible");
    expect(boundary).toHaveTextContent("Live enabled anywhere");
  });

  it("renders every safety flag including live trading lock state", () => {
    render(<OperatorDashboard status={operatorStatus()} />);

    const card = screen.getByTestId("operator-safety-flags");
    const expectedFlagLabels = [
      "Endpoint read-only",
      "Creates orders",
      "Calls brokers",
      "Triggers schedulers",
      "Runs strategies",
      "DCA runnable",
      "DCA live enabled",
      "Kraken live enabled",
      "Live trading enabled (env setting)",
      "Live trading unlocked (app)",
      "Expected venue configs missing",
      "Any venue kill switch active",
      "Any venue degraded",
      "Worker health known",
      "Cash-only mode",
    ];

    for (const label of expectedFlagLabels) {
      expect(within(card).getByText(label)).toBeInTheDocument();
    }
  });

  it("renders the safety posture card with failure policy and credential source", () => {
    render(<OperatorDashboard status={operatorStatus()} />);

    const card = screen.getByTestId("operator-safety-posture");
    expect(within(card).getByText("Safety Posture")).toBeInTheDocument();
    // Plain-language explanation of the fail-closed P&L policy.
    expect(
      within(card).getByText(
        "Trading is blocked until live P&L can be read again (fail-closed).",
      ),
    ).toBeInTheDocument();
    // Credential metadata is shown in operator-friendly wording.
    const credsRow = within(card)
      .getByText("Broker credentials configured")
      .closest("div");
    expect(credsRow).toHaveTextContent("Yes");
    expect(
      within(card).getByText("Mock (offline simulation — no real broker)"),
    ).toBeInTheDocument();
  });

  it("explains a fail-open P&L policy distinctly from fail-closed", () => {
    const status = operatorStatus();
    render(
      <OperatorDashboard
        status={{
          ...status,
          safety_flags: {
            ...status.safety_flags,
            unrealized_pnl_failure_policy: "assume_zero",
            credentials_configured: false,
            credential_source: "none",
          },
        }}
      />,
    );

    const card = screen.getByTestId("operator-safety-posture");
    expect(
      within(card).getByText(
        "Live P&L is treated as zero and trading continues (fail-open).",
      ),
    ).toBeInTheDocument();
    const credsRow = within(card)
      .getByText("Broker credentials configured")
      .closest("div");
    expect(credsRow).toHaveTextContent("No");
    expect(within(card).getByText("None configured")).toBeInTheDocument();
  });

  it("shows credential metadata only as a mapped safe label, never a raw value", () => {
    const status = operatorStatus();
    render(
      <OperatorDashboard
        status={{
          ...status,
          safety_flags: {
            ...status.safety_flags,
            credential_source: "stored_connection",
          },
        }}
      />,
    );

    const card = screen.getByTestId("operator-safety-posture");
    // The coarse enum is mapped to a human-readable label, and the raw token
    // is never passed through. The card only ever shows safe metadata — there
    // is no field on the status that could carry a key, secret, or blob.
    expect(
      within(card).getByText("Stored broker connection"),
    ).toBeInTheDocument();
    expect(within(card).queryByText("stored_connection")).toBeNull();
  });

  it("shows live trading as locked when env setting and app unlock are false", () => {
    render(<OperatorDashboard status={operatorStatus()} />);

    const card = screen.getByTestId("operator-safety-flags");
    const liveSettingRow = within(card)
      .getByText("Live trading enabled (env setting)")
      .closest("div");
    const liveUnlockRow = within(card)
      .getByText("Live trading unlocked (app)")
      .closest("div");

    expect(liveSettingRow).toHaveTextContent("False");
    expect(liveUnlockRow).toHaveTextContent("False");
  });

  it("describes DCA scheduler registration as registered", () => {
    render(<OperatorDashboard status={operatorStatus()} />);

    expect(screen.getAllByText("Registered").length).toBeGreaterThan(0);
  });

  it("renders registered and observed strategy-signals scheduler status read-only", () => {
    render(<OperatorDashboard status={withStrategySignalsScheduler()} />);

    const card = screen.getByTestId("strategy-signals-scheduler-status");
    expect(within(card).getByText("Strategy Signals Scheduler")).toBeInTheDocument();
    expect(within(card).getByText("Registered")).toBeInTheDocument();
    expect(within(card).getByText("Observation OK")).toBeInTheDocument();
    expect(within(card).getByText("300.0")).toBeInTheDocument();
    expect(
      within(card).getByText("app.workers.tasks.run_strategy_signals"),
    ).toBeInTheDocument();
    expect(
      within(card).getByText("Task heartbeat observed recently."),
    ).toBeInTheDocument();
    expect(
      within(card).getByText("This status is read-only. It does not start, stop, or run strategies."),
    ).toBeInTheDocument();
    expect(within(card).queryAllByRole("button")).toHaveLength(0);
    expect(within(card).queryAllByRole("link")).toHaveLength(0);
    expect(within(card).queryByRole("form")).not.toBeInTheDocument();
  });

  it("warns when the strategy-signals scheduler is configured but stale", () => {
    render(
      <OperatorDashboard
        status={withStrategySignalsScheduler({
          strategy_signals_observation_status: "stale",
          strategy_signals_last_seen_at: null,
          strategy_signals_observation_detail:
            "Celery beat entry exists, but no fresh worker heartbeat has been recorded.",
        })}
      />,
    );

    const card = screen.getByTestId("strategy-signals-scheduler-status");
    expect(within(card).getByText("Observation stale")).toBeInTheDocument();
    expect(within(card).getByText("Not observed yet")).toBeInTheDocument();
    expect(
      within(card).getByText(
        "Configured in Celery beat, but no real beat+worker run has been observed yet.",
      ),
    ).toBeInTheDocument();
  });

  it("renders unconfigured or unknown strategy-signals scheduler status safely", () => {
    render(
      <OperatorDashboard
        status={withStrategySignalsScheduler({
          strategy_signals_registered: false,
          strategy_signals_cadence: null,
          strategy_signals_observation_status: "unknown",
          strategy_signals_last_seen_at: null,
          strategy_signals_observation_detail: "Task heartbeat has not been recorded yet.",
        })}
      />,
    );

    const card = screen.getByTestId("strategy-signals-scheduler-status");
    expect(within(card).getByText("Not registered")).toBeInTheDocument();
    expect(within(card).getByText("Observation unknown")).toBeInTheDocument();
    expect(within(card).getByText("Unknown")).toBeInTheDocument();
    expect(within(card).getByText("Not observed yet")).toBeInTheDocument();
    expect(
      within(card).getByText("Task heartbeat has not been recorded yet."),
    ).toBeInTheDocument();
  });

  it("preserves slash tickers like BTC/USD", () => {
    render(<OperatorDashboard status={operatorStatus()} />);

    expect(screen.getAllByText("BTC/USD").length).toBeGreaterThan(0);
    expect(screen.getByText("ETH/USD")).toBeTruthy();
  });

  it("makes kill switch and degraded venue states visible", () => {
    render(<OperatorDashboard status={operatorStatus()} />);

    expect(screen.getAllByText("Kill switch active").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Degraded").length).toBeGreaterThan(0);
    expect(
      screen.getByText("Trading212 venue kill switch is active."),
    ).toBeTruthy();
  });

  it("renders recent activity safe summaries without sensitive payload keys", () => {
    render(<OperatorDashboard status={operatorStatus()} />);

    expect(screen.getAllByText("dca_paper_decision").length).toBeGreaterThan(0);
    expect(screen.getAllByText("safe_summary:").length).toBeGreaterThan(0);
    expect(screen.queryByText(/should-not-render/i)).toBeNull();
    expect(screen.queryByText(/api_key/i)).toBeNull();
  });

  it("does not expose manual trading or automation action controls", () => {
    render(<OperatorDashboard status={operatorStatus()} />);

    const forbiddenActionLabels = [
      /start automation/i,
      /stop automation/i,
      /enable automation/i,
      /disable automation/i,
      /run strategy now/i,
      /run strategy/i,
      /start live trading/i,
      /stop live trading/i,
      /unlock live/i,
      /place order/i,
      /submit order/i,
      /execute/i,
      /trade/i,
      /buy/i,
      /sell/i,
    ];

    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.queryByRole("link", { name: /run strategy|unlock live/i })).toBeNull();
    for (const label of forbiddenActionLabels) {
      expect(screen.queryByRole("button", { name: label })).toBeNull();
      expect(screen.queryByRole("link", { name: label })).toBeNull();
    }
  });

  it("renders Paper Execution History rows with safety wording", () => {
    render(<OperatorDashboard status={operatorStatus()} />);

    expect(
      screen.getByRole("heading", { name: "Paper Execution History" }),
    ).toBeTruthy();
    expect(screen.getByText("PAPERXYZ")).toBeTruthy();
    expect(screen.getByText("test_signal")).toBeTruthy();
    expect(screen.getByText("paper-test")).toBeTruthy();
    expect(screen.getAllByText("filled").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Paper only").length).toBeGreaterThan(0);
    expect(screen.getAllByText("No broker order sent").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Mock/local execution").length).toBeGreaterThan(0);
  });

  it("renders Paper Execution History empty state", () => {
    setPaperHistory({ data: paperHistory({ total: 0, items: [] }) });

    render(<OperatorDashboard status={operatorStatus()} />);

    expect(
      screen.getByText(
        "Paper execution history will appear here after mock paper orders are created. No broker order is sent.",
      ),
    ).toBeTruthy();
  });

  it("renders Paper Execution History error state without hiding operator status", () => {
    setPaperHistory({ data: undefined, isError: true });

    render(<OperatorDashboard status={operatorStatus()} />);

    expect(screen.getByRole("heading", { name: "Paper Execution History" }))
      .toBeTruthy();
    expect(screen.getByText("Paper execution history unavailable")).toBeTruthy();
    expect(screen.getByRole("heading", { name: "DCA Summary" })).toBeTruthy();
  });

  it("shows conservative failure wording", () => {
    render(<OperatorDashboard isError />);

    expect(
      screen.getAllByText(/Operator status unavailable/i).length,
    ).toBeGreaterThan(0);
    expect(
      screen.getAllByText(/Treat automation state as unknown/i).length,
    ).toBeGreaterThan(0);
  });

  it("shows empty states for no recent activity and no DCA configs", () => {
    const status = operatorStatus({
      dca: {
        ...operatorStatus().dca,
        config_count: 0,
        enabled_config_count: 0,
        tickers: [],
      },
      recent_activity: [],
    });

    render(<OperatorDashboard status={status} />);

    expect(screen.getByText("No DCA configs")).toBeTruthy();
    expect(screen.getByText("No recent activity")).toBeTruthy();
  });

  it("renders demo reconciliation scheduler disabled state", () => {
    setSchedulerStatus({ data: schedulerStatus({ enabled: false }) });

    render(<OperatorDashboard status={operatorStatus()} />);

    const card = screen.getByTestId("demo-reconciliation-status");
    expect(within(card).getByText("Scheduler disabled")).toBeInTheDocument();
    expect(within(card).getByText("Scheduler disabled by config.")).toBeInTheDocument();
    expect(within(card).getByText("Interval")).toBeInTheDocument();
    expect(within(card).getByText("120s")).toBeInTheDocument();
  });

  it("renders demo reconciliation scheduler enabled and running state", () => {
    setSchedulerStatus({
      data: schedulerStatus({
        enabled: true,
        running: true,
        last_run_outcome: "completed",
        last_run_finished_at: "2026-05-01T09:35:00Z",
        next_run_at: "2026-05-01T09:37:00Z",
        last_run_summary: {
          candidates_found: 3,
          attempted: 2,
          succeeded: 2,
          failed: 0,
          rate_limited: 0,
        },
      }),
    });

    render(<OperatorDashboard status={operatorStatus()} />);

    const card = screen.getByTestId("demo-reconciliation-status");
    expect(within(card).getByText("Scheduler enabled")).toBeInTheDocument();
    expect(within(card).getByText("Running Yes")).toBeInTheDocument();
    expect(within(card).getByText("completed")).toBeInTheDocument();
    expect(within(card).getByText("3")).toBeInTheDocument();
    expect(within(card).getAllByText("2").length).toBeGreaterThan(0);
  });

  it("renders demo reconciliation scheduler rate-limited backoff state", () => {
    setSchedulerStatus({
      data: schedulerStatus({
        enabled: true,
        running: false,
        last_run_outcome: "rate_limited",
        next_run_not_before: "2026-05-01T09:40:00Z",
        consecutive_rate_limits: 2,
        total_rate_limited_runs: 4,
        last_run_summary: {
          candidates_found: 1,
          attempted: 1,
          succeeded: 0,
          failed: 0,
          rate_limited: 1,
        },
      }),
    });

    render(<OperatorDashboard status={operatorStatus()} />);

    const card = screen.getByTestId("demo-reconciliation-status");
    expect(within(card).getByText("Backing off")).toBeInTheDocument();
    expect(within(card).getByText("Rate limited/backing off.")).toBeInTheDocument();
    expect(within(card).getByText("2")).toBeInTheDocument();
    expect(within(card).getByText("4")).toBeInTheDocument();
  });

  it("shows missing and failed reconciliation counts from the latest run", () => {
    setSchedulerStatus({
      data: schedulerStatus({
        enabled: true,
        running: true,
        last_run_outcome: "completed_with_failures",
        last_run_finished_at: new Date(Date.now() - 30_000).toISOString(),
        last_run_summary: {
          candidates_found: 5,
          attempted: 5,
          succeeded: 2,
          missing: 2,
          failed: 1,
          rate_limited: 0,
        },
      }),
    });

    render(<OperatorDashboard status={operatorStatus()} />);

    const card = screen.getByTestId("demo-reconciliation-status");
    expect(within(card).getByText("Missing")).toBeInTheDocument();
    expect(within(card).getByText("Failed")).toBeInTheDocument();
    expect(
      within(card).getByTestId("reconciliation-missing-count"),
    ).toHaveTextContent("2");
    expect(
      within(card).getByTestId("reconciliation-failed-count"),
    ).toHaveTextContent("1");
    expect(
      within(card).getByText(/not found in broker history/i),
    ).toBeInTheDocument();
  });

  it("flags a stale reconciliation when the last run is much older than the interval", () => {
    setSchedulerStatus({
      data: schedulerStatus({
        enabled: true,
        running: true,
        interval_seconds: 120,
        last_run_outcome: "completed",
        last_run_finished_at: "2026-01-01T00:00:00Z",
      }),
    });

    render(<OperatorDashboard status={operatorStatus()} />);

    const card = screen.getByTestId("demo-reconciliation-status");
    expect(
      within(card).getByTestId("reconciliation-stale-badge"),
    ).toHaveTextContent("Stale");
    expect(
      within(card).getByText(/older than the expected cadence/i),
    ).toBeInTheDocument();
  });

  it("does not flag stale for a recent reconciliation run", () => {
    setSchedulerStatus({
      data: schedulerStatus({
        enabled: true,
        running: true,
        interval_seconds: 120,
        last_run_outcome: "completed",
        last_run_finished_at: new Date(Date.now() - 30_000).toISOString(),
      }),
    });

    render(<OperatorDashboard status={operatorStatus()} />);

    const card = screen.getByTestId("demo-reconciliation-status");
    expect(
      within(card).queryByTestId("reconciliation-stale-badge"),
    ).not.toBeInTheDocument();
  });

  it("shows the scheduler's last error message when present", () => {
    setSchedulerStatus({
      data: schedulerStatus({
        enabled: true,
        last_error_message: "Worker run timed out after 60s.",
      }),
    });

    render(<OperatorDashboard status={operatorStatus()} />);

    const card = screen.getByTestId("demo-reconciliation-status");
    expect(
      within(card).getByText(/Last error: Worker run timed out after 60s\./),
    ).toBeInTheDocument();
  });

  it("declares the reconciliation card read-only with no controls", () => {
    render(<OperatorDashboard status={operatorStatus()} />);

    const card = screen.getByTestId("demo-reconciliation-status");
    expect(
      within(card).getByText(/no reconciliation controls/i),
    ).toBeInTheDocument();
    expect(within(card).queryAllByRole("button")).toHaveLength(0);
  });

  it("renders why_blocked reasons for a blocked/degraded status", () => {
    render(<OperatorDashboard status={operatorStatus()} />);

    const reasons = screen.getByTestId("why-blocked-reasons");
    expect(
      within(reasons).getByText(
        "A venue kill switch is active. Trading is blocked until it is cleared.",
      ),
    ).toBeInTheDocument();
    expect(
      within(reasons).getByText("At least one venue is reporting degraded mode."),
    ).toBeInTheDocument();
    expect(within(reasons).getByText("blocked")).toBeInTheDocument();
    expect(within(reasons).getByText("degraded")).toBeInTheDocument();
  });

  it("does not show stale blocker reasons when overall_status is ok", () => {
    const okStatus: OperatorStatus = {
      ...operatorStatus(),
      overall_status: "ok",
      why_blocked: [],
    };

    render(<OperatorDashboard status={okStatus} />);

    expect(screen.getByTestId("why-blocked-empty")).toHaveTextContent(
      "No active blockers.",
    );
    expect(screen.queryByTestId("why-blocked-reasons")).not.toBeInTheDocument();
    expect(
      screen.queryByText(
        "A venue kill switch is active. Trading is blocked until it is cleared.",
      ),
    ).not.toBeInTheDocument();
  });

  it("renders individual live_readiness_status checks with label, status, and detail", () => {
    const statusWithReadiness: OperatorStatus = {
      ...operatorStatus(),
      trading212: {
        ...operatorStatus().trading212,
        live_readiness_status: {
          mode: "demo",
          live_execution_enabled: false,
          live_trading_unlocked: false,
          eligible_for_unlock: false,
          ready_for_live: false,
          blockers: ["Server in live mode is not satisfied."],
          checks: [
            {
              key: "app_mode_live",
              label: "Server in live mode",
              status: "fail",
              detail: "`APP_MODE` must be `live` before the app can place real orders.",
              verified_at: null,
            },
            {
              key: "kill_switch_clear",
              label: "Kill switch currently clear",
              status: "pass",
              detail: "Kill switch is not active.",
              verified_at: null,
            },
          ],
        },
      },
    };

    render(<OperatorDashboard status={statusWithReadiness} />);

    const checklist = screen.getByTestId("live-readiness-checks");
    expect(within(checklist).getByText("Server in live mode")).toBeInTheDocument();
    expect(
      within(checklist).getByText(
        "`APP_MODE` must be `live` before the app can place real orders.",
      ),
    ).toBeInTheDocument();
    expect(within(checklist).getByText("Kill switch currently clear")).toBeInTheDocument();
    expect(within(checklist).getByText("Kill switch is not active.")).toBeInTheDocument();
    expect(within(checklist).getByText("fail")).toBeInTheDocument();
    expect(within(checklist).getByText("pass")).toBeInTheDocument();
  });
});

describe("CashGuardCard", () => {
  beforeEach(() => {
    setPaperHistory({});
    setSchedulerStatus({});
    setCashGuard({});
  });

  it("renders loading skeleton while cash data is fetching", () => {
    setCashGuard({ data: undefined, isLoading: true, isError: false });

    render(<OperatorDashboard status={operatorStatus()} />);

    const card = screen.getByTestId("operator-cashguard-card");
    expect(card).toBeInTheDocument();
    expect(within(card).getByLabelText("Loading CashGuard status")).toBeInTheDocument();
  });

  it("renders unavailable state when cash snapshot errors", () => {
    setCashGuard({ data: undefined, isLoading: false, isError: true });

    render(<OperatorDashboard status={operatorStatus()} />);

    const card = screen.getByTestId("operator-cashguard-card");
    expect(within(card).getByText("Cash snapshot unavailable")).toBeInTheDocument();
    expect(within(card).getByText("No order controls")).toBeInTheDocument();
  });

  it("renders available, reserved, and total cash with currency when data is present", () => {
    setCashGuard({
      data: {
        available_to_trade: 4285.0,
        reserved: 215.0,
        total_cash: 4500.0,
        cash_only_mode: true,
        currency: "USD",
      },
      isLoading: false,
      isError: false,
    });

    render(<OperatorDashboard status={operatorStatus()} />);

    const card = screen.getByTestId("operator-cashguard-card");
    expect(within(card).getByText("Available to trade")).toBeInTheDocument();
    expect(within(card).getByText("Reserved")).toBeInTheDocument();
    expect(within(card).getByText("Total cash")).toBeInTheDocument();
    expect(within(card).getByText("Currency")).toBeInTheDocument();
    expect(within(card).getByText("USD")).toBeInTheDocument();
  });

  it("shows blocked badge when operator overall_status is blocked", () => {
    const status = operatorStatus({
      overall_status: "blocked",
      safety_flags: {
        ...operatorStatus().safety_flags,
        any_venue_kill_switch_active: true,
      },
    });

    render(<OperatorDashboard status={status} />);

    const card = screen.getByTestId("operator-cashguard-card");
    expect(within(card).getByText("Blocked")).toBeInTheDocument();
  });

  it("shows degraded badge when operator overall_status is degraded and no kill switch", () => {
    const status = operatorStatus({
      overall_status: "degraded",
      safety_flags: {
        ...operatorStatus().safety_flags,
        any_venue_kill_switch_active: false,
      },
    });

    render(<OperatorDashboard status={status} />);

    const card = screen.getByTestId("operator-cashguard-card");
    expect(within(card).getByText("Degraded")).toBeInTheDocument();
  });

  it("shows ok badge when overall_status is ok and cash data is present", () => {
    const status = operatorStatus({
      overall_status: "ok",
      why_blocked: [],
      safety_flags: {
        ...operatorStatus().safety_flags,
        any_venue_kill_switch_active: false,
        any_venue_degraded: false,
      },
    });

    render(<OperatorDashboard status={status} />);

    const card = screen.getByTestId("operator-cashguard-card");
    expect(within(card).getByText("OK")).toBeInTheDocument();
  });

  it("does not render buy, sell, order, execute, or deposit controls inside the card", () => {
    render(<OperatorDashboard status={operatorStatus()} />);

    const card = screen.getByTestId("operator-cashguard-card");
    expect(within(card).queryByRole("button", { name: /buy/i })).toBeNull();
    expect(within(card).queryByRole("button", { name: /sell/i })).toBeNull();
    expect(within(card).queryByRole("button", { name: /order/i })).toBeNull();
    expect(within(card).queryByRole("button", { name: /execute/i })).toBeNull();
    expect(within(card).queryByRole("button", { name: /deposit/i })).toBeNull();
  });

  it("shows the read-only and no-order-controls badges", () => {
    render(<OperatorDashboard status={operatorStatus()} />);

    const card = screen.getByTestId("operator-cashguard-card");
    expect(within(card).getByText("Read-only")).toBeInTheDocument();
    expect(within(card).getByText("No order controls")).toBeInTheDocument();
  });
});

describe("ProtectiveStopsCard", () => {
  it("renders ok state with read-only wording and no recorded events", () => {
    render(<OperatorDashboard status={operatorStatus()} />);

    const card = screen.getByTestId("operator-protective-stops");
    expect(within(card).getByTestId("protective-stops-status")).toHaveTextContent("OK");
    expect(within(card).getByText("Read-only")).toBeInTheDocument();
    expect(within(card).getByText("No controls")).toBeInTheDocument();
    expect(within(card).getByTestId("protective-stops-events-empty")).toBeInTheDocument();
    expect(within(card).getByText("None recorded")).toBeInTheDocument();
  });

  it("renders triggered state with last kill-switch event and actor", () => {
    const status = operatorStatus({
      protective_stops: {
        status: "triggered",
        global_kill_switch_active: true,
        global_auto_trading_enabled: false,
        last_kill_switch_event: {
          event_type: "kill_switch_on",
          occurred_at: "2026-05-01T09:15:00Z",
          message: "Kill switch activated by circuit_breaker:trading212",
          ticker: null,
          actor: "circuit_breaker:trading212",
        },
        recent_events: [
          {
            event_type: "kill_switch_on",
            occurred_at: "2026-05-01T09:15:00Z",
            message: "Kill switch activated by circuit_breaker:trading212",
            ticker: null,
            actor: "circuit_breaker:trading212",
          },
          {
            event_type: "cash_guard_block",
            occurred_at: "2026-05-01T09:10:00Z",
            message: "Cash guard: estimated cost exceeds available cash",
            ticker: "AAPL",
            actor: null,
          },
        ],
        safety_notes: ["Read-only surface."],
      },
    });

    render(<OperatorDashboard status={status} />);

    const card = screen.getByTestId("operator-protective-stops");
    expect(within(card).getByTestId("protective-stops-status")).toHaveTextContent(
      "Triggered",
    );
    expect(within(card).getByText("Triggered by")).toBeInTheDocument();
    expect(
      within(card).getAllByText("circuit_breaker:trading212").length,
    ).toBeGreaterThan(0);
    const events = within(card).getByTestId("protective-stops-events");
    expect(within(events).getByText(/cash_guard_block/)).toBeInTheDocument();
    expect(within(events).getByText(/AAPL/)).toBeInTheDocument();
  });

  it("renders unknown state when persisted app settings are unavailable", () => {
    const status = operatorStatus({
      protective_stops: {
        status: "unknown",
        global_kill_switch_active: null,
        global_auto_trading_enabled: null,
        last_kill_switch_event: null,
        recent_events: [],
        safety_notes: ["Read-only surface."],
      },
    });

    render(<OperatorDashboard status={status} />);

    const card = screen.getByTestId("operator-protective-stops");
    expect(within(card).getByTestId("protective-stops-status")).toHaveTextContent(
      "Unknown",
    );
  });

  it("does not render reset, clear, enable, or disable controls", () => {
    render(<OperatorDashboard status={operatorStatus()} />);

    const card = screen.getByTestId("operator-protective-stops");
    expect(within(card).queryAllByRole("button")).toHaveLength(0);
    expect(within(card).queryByRole("button", { name: /reset/i })).toBeNull();
    expect(within(card).queryByRole("button", { name: /clear/i })).toBeNull();
    expect(within(card).queryByRole("button", { name: /enable/i })).toBeNull();
    expect(within(card).queryByRole("button", { name: /disable/i })).toBeNull();
  });
});

describe("operator API client", () => {
  it("uses GET only for operator status", async () => {
    const status = operatorStatus();
    mockGet.mockResolvedValue({ data: status });
    const api = (await import("@/services/api")).default;

    await expect(api.getOperatorStatus()).resolves.toEqual(status);

    expect(mockGet).toHaveBeenCalledWith("/operator/status");
    expect(mockPost).not.toHaveBeenCalled();
    expect(mockPatch).not.toHaveBeenCalled();
    expect(mockPut).not.toHaveBeenCalled();
    expect(mockDelete).not.toHaveBeenCalled();
  });

  it("uses GET only for paper execution history and audit", async () => {
    const history = paperHistory();
    mockGet
      .mockResolvedValueOnce({ data: history })
      .mockResolvedValueOnce({ data: { order_id: "order-1", items: [] } });
    const api = (await import("@/services/api")).default;

    await expect(api.getPaperExecutionHistory()).resolves.toEqual(history);
    await expect(api.getPaperOrderAudit("order-1")).resolves.toEqual({
      order_id: "order-1",
      items: [],
    });

    expect(mockGet).toHaveBeenCalledWith("/orders/paper", {
      params: undefined,
    });
    expect(mockGet).toHaveBeenCalledWith("/orders/paper/order-1/audit");
    expect(mockPost).not.toHaveBeenCalled();
    expect(mockPatch).not.toHaveBeenCalled();
    expect(mockPut).not.toHaveBeenCalled();
    expect(mockDelete).not.toHaveBeenCalled();
  });
});
