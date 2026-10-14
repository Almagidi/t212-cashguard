import "@testing-library/jest-dom";

import { beforeEach, describe, expect, it, jest } from "@jest/globals";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";

const cancelAllPending = jest.fn<() => Promise<Record<string, unknown>>>();
const cancelOrder = jest.fn<(orderId: string) => Promise<unknown>>();
const emergencyCancelAll = jest.fn<
  () => Promise<{
    success: boolean;
    action: string;
    message: string;
    timestamp: string;
  }>
>();
const toastSuccess = jest.fn<(message: string) => void>();
const toastError = jest.fn<(message: string) => void>();

jest.mock("@/services/api", () => ({
  __esModule: true,
  default: {
    cancelOrder,
    cancelAllPending,
    emergencyCancelAll,
  },
}));

jest.mock("react-hot-toast", () => ({
  __esModule: true,
  default: {
    success: toastSuccess,
    error: toastError,
  },
}));

const { useCancelAllPending, useCancelOrder, useEmergencyCancelAll } =
  require("@/hooks/use-api") as typeof import("@/hooks/use-api");

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

describe("cancellation mutation evidence", () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  it("shows reconciliation as an error when bulk cancellation partially fails", async () => {
    cancelAllPending.mockResolvedValue({ cancelled: 1, failed: 1 });
    const { result } = renderHook(() => useCancelAllPending(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync();
    });

    expect(toastError).toHaveBeenCalledWith(
      "Cancelled 1 orders; 1 cancellation requires reconciliation",
    );
    expect(toastSuccess).not.toHaveBeenCalled();
  });

  it("shows reconciliation when a single cancellation returns committed failure", async () => {
    cancelOrder.mockRejectedValue({
      response: { data: { requires_reconciliation: true } },
    });
    const { result } = renderHook(() => useCancelOrder(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync("order-1").catch(() => undefined);
    });

    expect(toastError).toHaveBeenCalledWith(
      "Cancellation failed; order requires reconciliation",
    );
    expect(toastSuccess).not.toHaveBeenCalled();
  });

  it("uses an error toast when emergency cancellation reports failure", async () => {
    emergencyCancelAll.mockResolvedValue({
      success: false,
      action: "cancel_all",
      message:
        "Cancelled 1 pending orders; 1 cancellation attempts require reconciliation.",
      timestamp: "2026-08-20T00:00:00Z",
    });
    const { result } = renderHook(() => useEmergencyCancelAll(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync();
    });

    expect(toastError).toHaveBeenCalledWith(
      "Cancelled 1 pending orders; 1 cancellation attempts require reconciliation.",
    );
    expect(toastSuccess).not.toHaveBeenCalled();
  });

  it("does not call an accepted but unconfirmed cancel request a cancellation", async () => {
    cancelOrder.mockResolvedValue({
      cancelled: false,
      awaiting_confirmation: true,
      order_id: "order-1",
      status: "accepted",
      reason: "no_broker_record",
      requires_reconciliation: true,
    });
    const { result } = renderHook(() => useCancelOrder(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync("order-1");
    });

    expect(toastError).toHaveBeenCalledWith(
      "Cancellation requested; the broker has not confirmed it yet. The order stays open until it does.",
    );
    expect(toastSuccess).not.toHaveBeenCalled();
  });

  it("says when the order filled before the cancellation could take effect", async () => {
    cancelOrder.mockRejectedValue({
      response: {
        status: 409,
        data: { cancelled: false, status: "filled", filled_quantity: "10" },
      },
    });
    const { result } = renderHook(() => useCancelOrder(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync("order-1").catch(() => undefined);
    });

    expect(toastError).toHaveBeenCalledWith(
      "Order not cancelled: it had already ended (filled, 10 filled)",
    );
    expect(toastSuccess).not.toHaveBeenCalled();
  });

  it("names the fill that came before a cancellation", async () => {
    cancelOrder.mockResolvedValue({
      cancelled: true,
      order_id: "order-1",
      filled_quantity: "4",
    });
    const { result } = renderHook(() => useCancelOrder(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync("order-1");
    });

    expect(toastSuccess).toHaveBeenCalledWith("Order cancelled after 4 filled");
    expect(toastError).not.toHaveBeenCalled();
  });

  it("does not report bulk cancellation as complete while some await confirmation", async () => {
    cancelAllPending.mockResolvedValue({
      cancelled: 2,
      failed: 0,
      awaiting_confirmation: 1,
    });
    const { result } = renderHook(() => useCancelAllPending(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync();
    });

    expect(toastError).toHaveBeenCalledWith(
      "Cancelled 2 orders; 1 awaiting broker confirmation (still open)",
    );
    expect(toastSuccess).not.toHaveBeenCalled();
  });

  it("names fills the bulk cancellation did not prevent", async () => {
    cancelAllPending.mockResolvedValue({
      cancelled: 1,
      failed: 0,
      already_finished: 1,
      cancelled_after_partial_fill: [
        { order_id: "a", ticker: "AAA", filled_quantity: "4", quantity: "10" },
      ],
      already_finished_orders: [
        { order_id: "c", ticker: "CCC", status: "filled", filled_quantity: "10" },
      ],
    });
    const { result } = renderHook(() => useCancelAllPending(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync();
    });

    expect(toastError).toHaveBeenCalledWith(
      "Cancelled 1 orders; 1 after a partial fill (AAA 4 of 10); 1 had already ended (CCC filled)",
    );
    expect(toastSuccess).not.toHaveBeenCalled();
  });

  it("keeps a plain success when an order that ended had nothing filled", async () => {
    cancelAllPending.mockResolvedValue({
      cancelled: 2,
      failed: 0,
      already_finished: 1,
      already_finished_orders: [
        { order_id: "c", ticker: "CCC", status: "rejected", filled_quantity: "0" },
      ],
    });
    const { result } = renderHook(() => useCancelAllPending(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync();
    });

    expect(toastSuccess).toHaveBeenCalledWith(
      "Cancelled 2 orders; 1 had already ended (CCC rejected)",
    );
    expect(toastError).not.toHaveBeenCalled();
  });
});
