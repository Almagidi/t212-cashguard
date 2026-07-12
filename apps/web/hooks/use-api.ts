import { getDcaActivity } from "@/services/api";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import toast from "react-hot-toast";
import api from "@/services/api";
import type {
  CreatePaperOrderPayload,
  CreateStrategyPayload,
  CreateStrategyPresetPayload,
  StrategyPresetKey,
  StrategyPromotionAction,
} from "@/types";

function extractErrorMessage(error: any, fallback: string): string {
  const detail = error?.response?.data?.detail;
  if (typeof detail === "string" && detail.trim()) return detail;
  if (
    detail &&
    typeof detail === "object" &&
    typeof detail.message === "string" &&
    detail.message.trim()
  ) {
    return detail.message;
  }
  return fallback;
}

// ── Query keys ───────────────────────────────────────────────────────────────
export const QK = {
  me: ["me"],
  brokerStatus: ["broker", "status"],
  demoReconciliationStatus: ["broker", "trading212", "reconciliation", "status"],
  demoReconciliationSchedulerStatus: [
    "broker",
    "trading212",
    "reconciliation",
    "scheduler",
    "status",
  ],
  account: ["account", "summary"],
  cashGuard: ["account", "cash-guard"],
  instruments: (p?: object) => ["instruments", p],
  strategies: ["strategies"],
  strategyPresets: ["strategies", "presets"],
  strategy: (id: string) => ["strategies", id],
  strategyPromotion: (id: string) => ["strategies", id, "promotion-status"],
  strategyIntelligence: (id: string) => ["strategies", id, "intelligence"],
  portfolioMonitoring: ["strategies", "portfolio-monitoring"],
  portfolioStrategyMonitoring: (id: string) => [
    "strategies",
    id,
    "portfolio-monitoring",
  ],
  portfolioAttribution: ["strategies", "portfolio-attribution"],
  portfolioStrategyAttribution: (id: string) => [
    "strategies",
    id,
    "portfolio-attribution",
  ],
  strategySignals: (id: string) => ["strategies", id, "signals"],
  signals: (p?: object) => ["signals", p],
  orders: (p?: object) => ["orders", p],
  order: (id: string) => ["orders", id],
  paperExecutionHistory: (p?: object) => ["orders", "paper", p],
  paperOrderAudit: (id: string) => ["orders", "paper", id, "audit"],
  positions: ["positions"],
  riskProfile: ["risk", "profile"],
  riskEvents: (p?: object) => ["risk", "events", p],
  alerts: (p?: object) => ["alerts", p],
  settings: ["settings"],
  liveReadiness: ["settings", "live-readiness"],
  telegramStatus: ["telegram", "status"],
  performance: ["reports", "performance"],
  performanceByStrategy: (days: number) => [
    "reports",
    "performance",
    "by-strategy",
    days,
  ],
  executionQuality: (days: number, includeDryRun: boolean) => [
    "reports",
    "execution-quality",
    days,
    includeDryRun,
  ],
  trades: ["reports", "trades"],
  tradesList: (p?: object) => ["trades", "list", p],
  trade: (id: string) => ["trades", id],
  auditLogs: (p?: object) => ["audit", p],
  operatorStatus: ["operator", "status"],
  health: ["health"],
  depsHealth: ["health", "deps"],
  startupHealth: ["health", "startup"],
  marketDataHealth: ["health", "market-data"],
  regime: ["intelligence", "regime"],
  watchlistIntelligence: (limit = 8) => ["intelligence", "watchlist", limit],
};

// ── Auth ────────────────────────────────────────────────────────────────────
// Current user never changes mid-session — treat as permanently fresh
export const useMe = () =>
  useQuery({
    queryKey: QK.me,
    queryFn: api.getMe.bind(api),
    retry: false,
    staleTime: Infinity,
  });

// ── Broker ──────────────────────────────────────────────────────────────────
export const useBrokerStatus = () =>
  useQuery({
    queryKey: QK.brokerStatus,
    queryFn: () => api.getBrokerStatus(),
    refetchInterval: 30_000,
  });

export const useDemoReconciliationStatus = () =>
  useQuery({
    queryKey: QK.demoReconciliationStatus,
    queryFn: () => api.getDemoReconciliationStatus(),
    refetchInterval: 30_000,
  });

export const useDemoReconciliationSchedulerStatus = () =>
  useQuery({
    queryKey: QK.demoReconciliationSchedulerStatus,
    queryFn: () => api.getDemoReconciliationSchedulerStatus(),
    refetchInterval: 30_000,
  });

export const useTestBroker = () => {
  const qc = useQueryClient();
  return useMutation<Awaited<ReturnType<typeof api.testBroker>>, any, void>({
    mutationFn: () => api.testBroker(),
    onSuccess: (data) => {
      qc.invalidateQueries({ queryKey: QK.brokerStatus });
      if (data.is_ok) {
        toast.success("Connection test passed");
      } else {
        toast.error(data.error || "Connection test failed");
      }
    },
    onError: (e: any) => {
      toast.error(extractErrorMessage(e, "Connection test failed"));
    },
  });
};

export const useDisconnectBroker = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.disconnectBroker(),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.brokerStatus });
      toast.success("Broker disconnected");
    },
  });
};

// ── Account ──────────────────────────────────────────────────────────────────
export const useAccount = () =>
  useQuery({
    queryKey: QK.account,
    queryFn: api.getAccountSummary.bind(api),
    refetchInterval: 15_000,
  });

export const useCashGuard = () =>
  useQuery({
    queryKey: QK.cashGuard,
    queryFn: api.getCashGuardStatus.bind(api),
    refetchInterval: 15_000,
  });

// ── Instruments ─────────────────────────────────────────────────────────────
// Instrument list only changes on an explicit broker sync — cache for 5 min
export const useInstruments = (
  params?: Parameters<typeof api.getInstruments>[0],
) =>
  useQuery({
    queryKey: QK.instruments(params),
    queryFn: () => api.getInstruments(params),
    staleTime: 5 * 60_000,
  });

export const useSyncInstruments = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: api.syncInstruments.bind(api),
    onSuccess: (data: any) => {
      qc.invalidateQueries({ queryKey: ["instruments"] });
      toast.success(`Synced ${data.synced} instruments`);
    },
    onError: () => toast.error("Sync failed"),
  });
};

// ── Strategies ──────────────────────────────────────────────────────────────
// Strategies list changes only when user creates/toggles one — 30 s is plenty
export const useStrategies = () =>
  useQuery({
    queryKey: QK.strategies,
    queryFn: api.getStrategies.bind(api),
    staleTime: 30_000,
  });

// Presets are essentially static config — cache for 10 min
export const useStrategyPresets = () =>
  useQuery({
    queryKey: QK.strategyPresets,
    queryFn: api.getStrategyPresets.bind(api),
    staleTime: 10 * 60_000,
  });

export const useStrategy = (id: string) =>
  useQuery({
    queryKey: QK.strategy(id),
    queryFn: () => api.getStrategy(id),
    enabled: !!id,
    staleTime: 30_000,
  });

export const useStrategyPromotion = (id: string) =>
  useQuery({
    queryKey: QK.strategyPromotion(id),
    queryFn: () => api.getStrategyPromotionStatus(id),
    enabled: !!id,
    refetchInterval: 30_000,
  });

export const useStrategyIntelligence = (id: string, enabled = true) =>
  useQuery({
    queryKey: QK.strategyIntelligence(id),
    queryFn: () => api.getStrategyIntelligence(id),
    enabled: !!id && enabled,
    refetchInterval: 30_000,
  });

export const usePortfolioMonitoring = () =>
  useQuery({
    queryKey: QK.portfolioMonitoring,
    queryFn: api.getPortfolioMonitoring.bind(api),
    refetchInterval: 30_000,
  });

export const usePortfolioStrategyMonitoring = (id: string, enabled = true) =>
  useQuery({
    queryKey: QK.portfolioStrategyMonitoring(id),
    queryFn: () => api.getPortfolioStrategyMonitoring(id),
    enabled: !!id && enabled,
    refetchInterval: 30_000,
  });

export const usePortfolioAttribution = () =>
  useQuery({
    queryKey: QK.portfolioAttribution,
    queryFn: api.getPortfolioAttribution.bind(api),
    refetchInterval: 30_000,
  });

export const usePortfolioStrategyAttribution = (id: string, enabled = true) =>
  useQuery({
    queryKey: QK.portfolioStrategyAttribution(id),
    queryFn: () => api.getPortfolioStrategyAttribution(id),
    enabled: !!id && enabled,
    refetchInterval: 30_000,
  });

export const useCreateStrategy = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (p: CreateStrategyPayload) => api.createStrategy(p),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.strategies });
      toast.success("Strategy created");
    },
    onError: () => toast.error("Failed to create strategy"),
  });
};

export const useCreateStrategyPreset = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      key,
      payload,
    }: {
      key: StrategyPresetKey;
      payload?: CreateStrategyPresetPayload;
    }) => api.createStrategyFromPreset(key, payload),
    onSuccess: (strategy) => {
      qc.invalidateQueries({ queryKey: QK.strategies });
      qc.invalidateQueries({ queryKey: QK.strategyPresets });
      toast.success(`${strategy.name} created with demo risk template`);
    },
    onError: (e: any) =>
      toast.error(extractErrorMessage(e, "Failed to create preset strategy")),
  });
};

export const useToggleStrategy = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, enable }: { id: string; enable: boolean }) =>
      enable ? api.enableStrategy(id) : api.disableStrategy(id),
    onSuccess: (_d, v) => {
      qc.invalidateQueries({ queryKey: QK.strategies });
      toast.success(v.enable ? "Strategy enabled" : "Strategy disabled");
    },
  });
};

export const useUpdateStrategyPromotion = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      id,
      action,
      notes,
    }: {
      id: string;
      action: StrategyPromotionAction;
      notes?: string;
    }) => api.updateStrategyPromotion(id, action, notes),
    onSuccess: (data, variables) => {
      qc.setQueryData(QK.strategyPromotion(variables.id), data);
      qc.invalidateQueries({ queryKey: QK.strategy(variables.id) });
      qc.invalidateQueries({ queryKey: QK.strategies });
      const label =
        variables.action === "promote_to_demo"
          ? "Strategy promoted to demo"
          : variables.action === "promote_to_live"
            ? "Strategy approved for live"
            : variables.action === "demote_to_dry_run"
              ? "Strategy moved back to dry-run"
              : variables.action === "revoke_live_promotion"
                ? "Live approval revoked"
                : "Promotion checkpoint recorded";
      toast.success(label);
    },
    onError: (e: any) =>
      toast.error(extractErrorMessage(e, "Strategy promotion update failed")),
  });
};

// ── Signals ─────────────────────────────────────────────────────────────────
export const useSignals = (params?: Parameters<typeof api.getSignals>[0]) =>
  useQuery({
    queryKey: QK.signals(params),
    queryFn: () => api.getSignals(params),
    refetchInterval: 10_000,
  });

// ── Orders ──────────────────────────────────────────────────────────────────
export const useOrders = (params?: Parameters<typeof api.getOrders>[0]) =>
  useQuery({
    queryKey: QK.orders(params),
    queryFn: () => api.getOrders(params),
    refetchInterval: 10_000,
  });

export const useOrder = (id: string | null, options?: { enabled?: boolean }) =>
  useQuery({
    queryKey: ["order", id],
    queryFn: () => api.getOrder(id!),
    enabled: Boolean(id) && (options?.enabled ?? true),
    refetchInterval: 10_000,
  });

export const usePaperExecutionHistory = (
  params?: Parameters<typeof api.getPaperExecutionHistory>[0],
) =>
  useQuery({
    queryKey: QK.paperExecutionHistory(params),
    queryFn: () => api.getPaperExecutionHistory(params),
    refetchInterval: 30_000,
  });

export const usePaperOrderAudit = (
  id: string | null,
  options?: { enabled?: boolean },
) =>
  useQuery({
    queryKey: QK.paperOrderAudit(id ?? "none"),
    queryFn: () => api.getPaperOrderAudit(id!),
    enabled: Boolean(id) && (options?.enabled ?? true),
    refetchInterval: 30_000,
  });

export const usePlacePaperOrder = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (p: CreatePaperOrderPayload) => api.placePaperOrder(p),
    onSuccess: async () => {
      await Promise.all([
        qc.invalidateQueries({ queryKey: ["orders"] }),
        qc.invalidateQueries({ queryKey: ["positions"] }),
        qc.invalidateQueries({ queryKey: ["orders", "paper"] }),
      ]);
      toast.success("Paper order recorded");
    },
    onError: (e: any) =>
      toast.error(extractErrorMessage(e, "Paper order blocked")),
  });
};

export const useCancelOrder = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.cancelOrder(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["orders"] });
      toast.success("Order cancelled");
    },
    onError: () => toast.error("Failed to cancel order"),
  });
};

export const useCancelAllPending = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: api.cancelAllPending.bind(api),
    onSuccess: (d: any) => {
      qc.invalidateQueries({ queryKey: ["orders"] });
      toast.success(`Cancelled ${d.cancelled} orders`);
    },
  });
};

// ── Positions ────────────────────────────────────────────────────────────────
export const usePositions = () =>
  useQuery({
    queryKey: QK.positions,
    queryFn: api.getPositions.bind(api),
    refetchInterval: 15_000,
  });

// ── Risk ─────────────────────────────────────────────────────────────────────
// Risk profile only changes when explicitly saved — cache for 2 min
export const useRiskProfile = () =>
  useQuery({
    queryKey: QK.riskProfile,
    queryFn: api.getRiskProfile.bind(api),
    staleTime: 2 * 60_000,
  });

export const useUpdateRiskProfile = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (p: any) => api.updateRiskProfile(p),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.riskProfile });
      toast.success("Risk profile updated");
    },
    onError: () => toast.error("Update failed"),
  });
};

export const useRiskEvents = (params?: {
  limit?: number;
  event_type?: string;
}) =>
  useQuery({
    queryKey: QK.riskEvents(params),
    queryFn: () => api.getRiskEvents(params),
    refetchInterval: 30_000,
  });

export const useKillSwitch = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ active }: { active: boolean }) =>
      active ? api.enableKillSwitch() : api.disableKillSwitch(),
    onSuccess: async (_d, v) => {
      await qc.invalidateQueries({ queryKey: QK.settings });
      toast.success(
        v.active ? "⛔ Kill switch activated" : "✅ Kill switch deactivated",
      );
    },
  });
};

// ── Alerts ───────────────────────────────────────────────────────────────────
export const useAlerts = (params?: { is_read?: boolean; limit?: number }) =>
  useQuery({
    queryKey: QK.alerts(params),
    queryFn: () => api.getAlerts(params),
    refetchInterval: 30_000,
  });

// ── Settings ─────────────────────────────────────────────────────────────────
export const useSettings = () =>
  useQuery({
    queryKey: QK.settings,
    queryFn: api.getSettings.bind(api),
    refetchInterval: 60_000,
  });

export const useUpdateSettings = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (p: any) => api.updateSettings(p),
    onSuccess: (data) => {
      qc.setQueryData(QK.settings, data);
      toast.success("Settings saved");
    },
  });
};

export const useLiveReadiness = () =>
  useQuery({
    queryKey: QK.liveReadiness,
    queryFn: api.getLiveReadiness.bind(api),
    refetchInterval: 60_000,
  });

export const useUpdateLiveReadiness = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      action,
      notes,
    }: {
      action: Parameters<typeof api.updateLiveReadiness>[0];
      notes?: string;
    }) => api.updateLiveReadiness(action, notes),
    onSuccess: (data, variables) => {
      qc.setQueryData(QK.liveReadiness, data);
      qc.invalidateQueries({ queryKey: QK.settings });
      const label =
        variables.action === "unlock_live"
          ? "Live trading unlocked"
          : variables.action === "lock_live"
            ? "Live trading locked"
            : "Readiness checkpoint recorded";
      toast.success(label);
    },
    onError: (e: any) =>
      toast.error(extractErrorMessage(e, "Live readiness update failed")),
  });
};

export const useTelegramStatus = () =>
  useQuery({
    queryKey: QK.telegramStatus,
    queryFn: api.getTelegramStatus.bind(api),
    refetchInterval: 60_000,
  });

export const useTelegramTestAlert = () => {
  return useMutation({
    mutationFn: api.sendTelegramTestAlert.bind(api),
    onSuccess: (data: { message: string }) => toast.success(data.message),
    onError: (e: any) =>
      toast.error(extractErrorMessage(e, "Telegram test failed")),
  });
};

// ── Emergency ─────────────────────────────────────────────────────────────────
export const useEmergencyKillSwitch = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: api.emergencyKillSwitch.bind(api),
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: QK.settings });
      toast.error("⛔ KILL SWITCH ACTIVATED");
    },
  });
};
export const useEmergencyDisableKillSwitch = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: api.disableKillSwitch.bind(api),
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: QK.settings });
      toast.success(
        "Kill switch disabled. Auto-trading remains OFF until manually re-enabled.",
      );
    },
    onError: (e: any) =>
      toast.error(extractErrorMessage(e, "Failed to disable kill switch")),
  });
};
export const useEmergencyAutoTradingOff = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: api.emergencyAutoTradingOff.bind(api),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["settings"] });
      toast.success("Auto-trading disabled");
    },
  });
};
export const useEmergencyAutoTradingOn = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: api.emergencyAutoTradingOn.bind(api),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["settings"] });
    },
    onError: (e: any) => toast.error(extractErrorMessage(e, "Cannot enable")),
  });
};
export const useEmergencyCancelAll = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: api.emergencyCancelAll.bind(api),
    onSuccess: (d: any) => {
      qc.invalidateQueries({ queryKey: ["orders"] });
      toast.success(d.message);
    },
  });
};
export const useEmergencyFlattenAll = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: api.emergencyFlattenAll.bind(api),
    onSuccess: (d: any) => {
      qc.invalidateQueries({ queryKey: ["positions", "orders"] });
      toast.success(d.message);
    },
  });
};

// ── Reports ──────────────────────────────────────────────────────────────────
export const usePerformanceReport = (days = 30) =>
  useQuery({
    queryKey: [...QK.performance, days],
    queryFn: () => api.getPerformanceReport(days),
  });

export const usePerformanceByStrategy = (days = 30) =>
  useQuery({
    queryKey: QK.performanceByStrategy(days),
    queryFn: () => api.getPerformanceByStrategy(days),
  });

export const useExecutionQualityReport = (days = 30, includeDryRun = false) =>
  useQuery({
    queryKey: QK.executionQuality(days, includeDryRun),
    queryFn: () => api.getExecutionQualityReport(days, includeDryRun),
    refetchInterval: 30_000,
  });

export const useTradesReport = (limit = 100) =>
  useQuery({ queryKey: QK.trades, queryFn: () => api.getTradesReport(limit) });

export const useTradesList = (params?: {
  page?: number;
  page_size?: number;
  ticker?: string;
  has_notes?: boolean;
}) =>
  useQuery({
    queryKey: QK.tradesList(params),
    queryFn: () => api.listTrades(params),
  });

export const useTrade = (id: string) =>
  useQuery({
    queryKey: QK.trade(id),
    queryFn: () => api.getTrade(id),
    enabled: !!id,
  });

export const useUpdateTradeJournal = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      id,
      payload,
    }: {
      id: string;
      payload: {
        notes?: string;
        tags?: string[];
        emotion?: string;
        rating?: number;
      };
    }) => api.updateTradeJournal(id, payload),
    onSuccess: (_, { id }) => {
      qc.invalidateQueries({ queryKey: QK.trade(id) });
      qc.invalidateQueries({ queryKey: ["trades", "list"] });
      toast.success("Journal updated");
    },
    onError: () => toast.error("Failed to update journal"),
  });
};

// ── Audit ────────────────────────────────────────────────────────────────────
export const useAuditLogs = (params?: Parameters<typeof api.getAuditLogs>[0]) =>
  useQuery({
    queryKey: QK.auditLogs(params),
    queryFn: () => api.getAuditLogs(params),
    refetchInterval: 30_000,
  });

// ── Operator ────────────────────────────────────────────────────────────────
export const useOperatorStatus = () =>
  useQuery({
    queryKey: QK.operatorStatus,
    queryFn: api.getOperatorStatus.bind(api),
    refetchInterval: 30_000,
  });

// ── Health ───────────────────────────────────────────────────────────────────
export const useHealth = () =>
  useQuery({
    queryKey: QK.health,
    queryFn: api.getHealth.bind(api),
    refetchInterval: 30_000,
  });

export const useDepsHealth = () =>
  useQuery({
    queryKey: QK.depsHealth,
    queryFn: api.getDepsHealth.bind(api),
    refetchInterval: 30_000,
  });

export const useStartupHealth = () =>
  useQuery({
    queryKey: QK.startupHealth,
    queryFn: api.getStartupHealth.bind(api),
    refetchInterval: 30_000,
  });

export const useMarketDataHealth = () =>
  useQuery({
    queryKey: QK.marketDataHealth,
    queryFn: api.getMarketDataHealth.bind(api),
    refetchInterval: 30_000,
  });

export const useMarketRegime = () =>
  useQuery({
    queryKey: QK.regime,
    queryFn: api.getMarketRegime.bind(api),
    refetchInterval: 30_000,
  });

export const useWatchlistIntelligence = (limit = 8) =>
  useQuery({
    queryKey: QK.watchlistIntelligence(limit),
    queryFn: () => api.getWatchlistIntelligence(limit),
    refetchInterval: 30_000,
  });

// T-OPS-004: read-only Kraken DCA activity report.
export function useDcaActivity() {
  return useQuery({
    queryKey: ["kraken", "dca", "activity"],
    queryFn: getDcaActivity,
    refetchInterval: 30_000,
  });
}
