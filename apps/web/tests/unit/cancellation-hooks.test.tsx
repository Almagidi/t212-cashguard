import "@testing-library/jest-dom";

import { beforeEach, describe, expect, it, jest } from "@jest/globals";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";

const cancelAllPending =
  jest.fn<() => Promise<{ cancelled: number; failed: number }>>();
const cancelOrder = jest.fn<(orderId: string) => Promise<void>>();
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
});
