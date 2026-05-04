import { describe, expect, it } from "@jest/globals";
import { render, screen } from "@testing-library/react";

import { DcaActivityPanel } from "@/components/operator/dca-activity-panel";
import type { DcaActivityResponse } from "@/types";

const mockActivity: DcaActivityResponse = {
  subsystem: "kraken_dca",
  mode: "paper_only",
  runnable: false,
  live_enabled: false,
  generated_at: "2026-05-02T00:00:00Z",
  config_count: 2,
  enabled_config_count: 1,
  decision_count_total: 4,
  decision_counts_by_code: {
    BUY_DUE: 1,
    BLOCKED_LOW_CASH: 2,
    SKIP_ALREADY_BOUGHT_THIS_WINDOW: 1,
  },
  buy_due_count: 1,
  blocked_count: 2,
  skipped_count: 1,
  total_paper_allocated_usd: "25.00",
  order_count_sanity: 0,
  configs: [
    {
      id: "cfg-btc",
      ticker: "BTC/USD",
      venue: "kraken",
      enabled: true,
      paper_only: true,
      cadence_days: 7,
      fixed_cash_amount: "25.00",
      max_position_percent: "10",
    },
    {
      id: "cfg-eth",
      ticker: "ETH/USD",
      venue: "kraken",
      enabled: false,
      paper_only: true,
      cadence_days: 7,
      fixed_cash_amount: "25.00",
      max_position_percent: "10",
    },
  ],
  per_ticker_activity: [
    {
      ticker: "BTC/USD",
      venue: "kraken",
      enabled: true,
      latest_decision_code: "BUY_DUE",
      latest_decision_at: "2026-05-02T00:00:00Z",
      latest_reason: "cadence due",
      total_allocated_usd: "25.00",
      executions_count: 1,
      last_buy_at: "2026-05-02",
      decision_counts_by_code: { BUY_DUE: 1 },
    },
  ],
  recent_decisions: [
    {
      audit_id: "audit-1",
      occurred_at: "2026-05-02T00:00:00Z",
      ticker: "BTC/USD",
      venue: "kraken",
      decision_code: "BUY_DUE",
      reason: "cadence due",
    },
  ],
  safety_flags: {
    dca_planner_runnable_is_false: true,
    dca_planner_paper_only_is_true: true,
    main_runner_registered: false,
    order_creation_supported: false,
  },
};

describe("DcaActivityPanel", () => {
  it("renders read-only DCA activity with slash tickers preserved", () => {
    render(<DcaActivityPanel activity={mockActivity} />);

    expect(screen.getByText("DCA paper activity")).toBeInTheDocument();
    expect(screen.getByText("Read-only")).toBeInTheDocument();
    expect(screen.getByText("Paper-only")).toBeInTheDocument();
    expect(screen.getByText("Live disabled")).toBeInTheDocument();
    expect(screen.getAllByText("BTC/USD").length).toBeGreaterThan(0);
    expect(screen.getByText("ETH/USD")).toBeInTheDocument();
    expect(screen.getAllByText("BUY_DUE").length).toBeGreaterThan(0);
    expect(screen.getAllByText("cadence due").length).toBeGreaterThan(0);
  });

  it("does not render forbidden trading or mutation controls", () => {
    render(<DcaActivityPanel activity={mockActivity} />);

    expect(
      screen.queryByRole("button", { name: /enable/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /disable/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /buy/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /sell/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /trade/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /execute/i }),
    ).not.toBeInTheDocument();
  });

  it("renders conservative error wording", () => {
    render(<DcaActivityPanel error={new Error("failed")} />);

    expect(
      screen.getByText(/DCA activity report is unavailable/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/unknown/i)).toBeInTheDocument();
  });

  it("renders empty state", () => {
    render(<DcaActivityPanel />);

    expect(
      screen.getByText(/No DCA activity report is available/i),
    ).toBeInTheDocument();
  });
});
