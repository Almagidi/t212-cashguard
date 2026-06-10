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

jest.mock("@/hooks/use-api", () => ({
  __esModule: true,
  useBrokerStatus: () => ({
    data: null,
    isLoading: false,
    error: null,
  }),
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
    },
  };

  return { ...base, ...overrides };
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

describe("OperatorDashboard", () => {
  beforeEach(() => {
    setPaperHistory({});
    setSchedulerStatus({});
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

  it("does not expose enable, disable, execute, trade, buy, or sell buttons", () => {
    render(<OperatorDashboard status={operatorStatus()} />);

    expect(screen.queryByRole("button", { name: /enable/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /disable/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /execute/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /trade/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /buy/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /sell/i })).toBeNull();
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
