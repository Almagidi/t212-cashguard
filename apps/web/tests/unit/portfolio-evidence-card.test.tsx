import "@testing-library/jest-dom";

import { describe, expect, it } from "@jest/globals";
import { render, screen } from "@testing-library/react";

import { PortfolioEvidenceCard } from "@/components/backtest/portfolio-evidence-card";
import type { PortfolioBacktestJob } from "@/types";

describe("PortfolioEvidenceCard", () => {
  it("surfaces insufficient evidence and retained session coverage", () => {
    const job: PortfolioBacktestJob = {
      status: "complete",
      verdict: "insufficient_evidence",
      evidence_reasons: ["retained intersection coverage 90.00% is below 95%"],
      bars_used: 18,
      result: null,
      coverage: {
        calendar: "XNYS",
        exchange_timezone: "America/New_York",
        requested_from: "2025-01-06",
        requested_to: "2025-02-03",
        minimum_coverage_pct: 95,
        retained_coverage_pct: 90,
        complete: false,
        expected_session_ids: Array.from(
          { length: 20 },
          (_, index) => `XNYS:${index}`,
        ),
        retained_session_ids: Array.from(
          { length: 18 },
          (_, index) => `XNYS:${index}`,
        ),
        dropped_session_ids: ["XNYS:2025-01-21", "XNYS:2025-01-22"],
        symbols: [],
      },
    };

    render(<PortfolioEvidenceCard job={job} />);

    expect(
      screen.getByText("Insufficient Portfolio Evidence"),
    ).toBeInTheDocument();
    expect(screen.getByText("90.00% retained")).toBeInTheDocument();
    expect(
      screen.getByText(/retained intersection coverage 90.00%/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/2 expected sessions were dropped/i),
    ).toBeInTheDocument();
  });
});
