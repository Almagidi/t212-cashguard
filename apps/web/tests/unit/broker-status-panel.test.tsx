import { describe, expect, it } from "@jest/globals";
import { render, screen } from "@testing-library/react";

import { BrokerStatusPanel } from "@/components/operator/broker-status-panel";
import type { BrokerStatus } from "@/types";

const mockStatus: BrokerStatus = {
  id: "broker-1",
  broker: "trading212",
  environment: "demo",
  is_active: true,
  credential_state: "configured",
  recovery_hint: null,
  last_test_at: "2026-05-02T00:00:00Z",
  last_test_ok: true,
  last_sync_at: null,
  account_id: "10979023",
  account_currency: "GBP",
  created_at: "2026-05-02T00:00:00Z",
};

describe("BrokerStatusPanel", () => {
  it("renders read-only broker connection metadata", () => {
    render(<BrokerStatusPanel status={mockStatus} />);

    expect(screen.getByText("Broker connection status")).toBeInTheDocument();
    expect(screen.getByText("Read-only")).toBeInTheDocument();
    expect(screen.getByText("No broker actions")).toBeInTheDocument();
    expect(screen.getByText("trading212")).toBeInTheDocument();
    expect(screen.getByText("demo")).toBeInTheDocument();
    expect(screen.getByText("GBP")).toBeInTheDocument();
    expect(screen.getByText("••••9023")).toBeInTheDocument();
  });

  it("does not expose broker mutation or trading controls", () => {
    render(<BrokerStatusPanel status={mockStatus} />);

    expect(
      screen.queryByRole("button", { name: /connect/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /reconnect/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /disconnect/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /buy/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /sell/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /execute/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /live/i }),
    ).not.toBeInTheDocument();
  });

  it("renders conservative unavailable state", () => {
    render(<BrokerStatusPanel error={new Error("failed")} />);

    expect(
      screen.getByText(/Broker status is unavailable/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/unknown/i)).toBeInTheDocument();
  });

  it("does not reveal full account id", () => {
    render(<BrokerStatusPanel status={mockStatus} />);

    expect(screen.queryByText("10979023")).not.toBeInTheDocument();
    expect(screen.getByText("••••9023")).toBeInTheDocument();
  });
});
