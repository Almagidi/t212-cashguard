import "@testing-library/jest-dom";

import { describe, expect, it, jest } from "@jest/globals";
import { render, screen } from "@testing-library/react";

import type { OperatorStatus, OperatorWorkerHealth } from "@/types";

const mockGet = jest.fn<() => Promise<{ data: OperatorStatus }>>();
const mockPost = jest.fn();
const mockPatch = jest.fn();
const mockPut = jest.fn();
const mockDelete = jest.fn();

jest.mock("@/hooks/use-api", () => ({
  __esModule: true,
  useBrokerStatus: () => ({
    data: null,
    isLoading: false,
    error: null,
  }),
  useDcaActivity: () => ({
    data: null,
    isLoading: false,
    error: null,
  }),
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

describe("OperatorDashboard", () => {
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

  it("describes scheduler registration as registered, not running", () => {
    render(<OperatorDashboard status={operatorStatus()} />);

    expect(screen.getAllByText("Registered").length).toBeGreaterThan(0);
    expect(screen.queryByText(/running/i)).toBeNull();
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
});
