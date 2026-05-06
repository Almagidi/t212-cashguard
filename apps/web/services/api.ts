import axios, { AxiosError, type AxiosInstance } from "axios";
import type {
  AccountSummary,
  Alert,
  AppSettings,
  AuditLogList,
  BacktestJob,
  BacktestJobResponse,
  BacktestRunRequest,
  BacktestStrategyInfo,
  BrokerStatus,
  BrokerTestResult,
  CashGuardStatus,
  CreateOrderPayload,
  CreateStrategyPayload,
  CreateStrategyPresetPayload,
  DepsHealth,
  DcaActivityResponse,
  DcaConfig,
  DcaOperatorStatus,
  EmergencyActionResult,
  ExecutionQualityReport,
  HealthStatus,
  Instrument,
  InstrumentList,
  LoginRequest,
  LoginResponse,
  MarketDataHealth,
  MarketRegime,
  LiveReadinessAction,
  LiveReadinessStatus,
  OperatorStatus,
  Order,
  OrderDetail,
  PerformanceReport,
  Position,
  PortfolioStrategyAttribution,
  PortfolioStrategyAttributionSummary,
  PortfolioStrategyMonitoring,
  PortfolioBacktestJob,
  PortfolioBacktestRunRequest,
  PortfolioBacktestStrategyInfo,
  RiskEvent,
  RiskProfile,
  Signal,
  Strategy,
  StrategyIntelligence,
  StrategyPresetInfo,
  StrategyPromotionAction,
  StrategyPromotionStatus,
  StrategyDryRunResult,
  TelegramStatus,
  TelegramTestResult,
  User,
  WatchlistIntelligence,
} from "@/types";

const DEFAULT_API_URL =
  process.env.NODE_ENV === "production" ? "/api" : "http://localhost:8000";
export const API_URL = (
  process.env.NEXT_PUBLIC_API_URL || DEFAULT_API_URL
).replace(/\/$/, "");
export const API_V1_URL = `${API_URL}/v1`;

export function isApiUnreachableError(error: unknown): boolean {
  const axErr = error as AxiosError | undefined;
  return (
    axErr?.code === "ERR_NETWORK" ||
    (!axErr?.response && axErr?.message === "Network Error")
  );
}

export function apiUnreachableMessage(): string {
  return `Backend API is unreachable at ${API_URL}. Start the API server and retry.`;
}

class ApiClient {
  private client: AxiosInstance;

  constructor() {
    this.client = axios.create({
      baseURL: API_V1_URL,
      headers: { "Content-Type": "application/json" },
      timeout: 15000,
    });

    // Attach JWT token to every request
    this.client.interceptors.request.use((config) => {
      const token = this.getToken();
      if (token) config.headers.Authorization = `Bearer ${token}`;
      return config;
    });

    // Handle 401 globally — only redirect to login if the session is truly expired.
    // We verify by calling /auth/me: if that also 401s, the token is gone for real.
    // This prevents a single bad endpoint response from kicking out a valid user.
    let _verifyingLogout = false;
    this.client.interceptors.response.use(
      (r) => r,
      async (error: AxiosError) => {
        const is401 = error.response?.status === 401;
        const isAuthEndpoint = (error.config?.url ?? "").startsWith("/auth/");
        const hasToken = !!this.getToken();

        if (
          is401 &&
          hasToken &&
          !isAuthEndpoint &&
          !_verifyingLogout &&
          typeof window !== "undefined"
        ) {
          _verifyingLogout = true;
          try {
            // Re-check the session; use a fresh axios instance to avoid recursion
            await axios.get(`${API_URL}/v1/auth/me`, {
              headers: { Authorization: `Bearer ${this.getToken()}` },
              timeout: 5000,
            });
            // /auth/me succeeded → the 401 was from a non-critical endpoint, stay logged in
          } catch (meErr: unknown) {
            const meStatus = (meErr as AxiosError)?.response?.status;
            if (meStatus === 401) {
              // Token is genuinely invalid — log out
              this.clearToken();
              window.location.href = "/auth/login";
            }
            // Any other error (network, 5xx) → leave user logged in, don't disrupt UX
          } finally {
            _verifyingLogout = false;
          }
        }

        return Promise.reject(error);
      },
    );
  }

  // ── Token management ────────────────────────────────────────────────────────
  getToken(): string | null {
    if (typeof window === "undefined") return null;
    return localStorage.getItem("cg_token");
  }
  setToken(token: string): void {
    if (typeof window !== "undefined") localStorage.setItem("cg_token", token);
  }
  clearToken(): void {
    if (typeof window !== "undefined") localStorage.removeItem("cg_token");
  }
  isAuthenticated(): boolean {
    return !!this.getToken();
  }

  // ── Auth ────────────────────────────────────────────────────────────────────
  async login(data: LoginRequest): Promise<LoginResponse> {
    const res = await this.client.post<LoginResponse>("/auth/login", data);
    this.setToken(res.data.access_token);
    return res.data;
  }
  async logout(): Promise<void> {
    await this.client.post("/auth/logout").catch(() => {});
    this.clearToken();
  }
  async getMe(): Promise<User> {
    return (await this.client.get<User>("/auth/me")).data;
  }

  // ── Broker ──────────────────────────────────────────────────────────────────
  async connectBroker(payload: {
    broker?: "trading212" | "kraken";
    api_key: string;
    api_secret: string;
    environment: "demo" | "live";
  }): Promise<BrokerStatus> {
    const broker = payload.broker || "trading212";
    const path =
      broker === "kraken"
        ? "/broker/kraken/connect"
        : "/broker/trading212/connect";
    const { broker: _, ...rest } = payload;
    return (await this.client.post<BrokerStatus>(path, rest)).data;
  }
  async testBroker(
    broker: "trading212" | "kraken" = "trading212",
  ): Promise<BrokerTestResult> {
    const path =
      broker === "kraken" ? "/broker/kraken/test" : "/broker/trading212/test";
    return (await this.client.post<BrokerTestResult>(path)).data;
  }
  async disconnectBroker(
    broker: "trading212" | "kraken" = "trading212",
  ): Promise<void> {
    const path =
      broker === "kraken"
        ? "/broker/kraken/disconnect"
        : "/broker/trading212/disconnect";
    await this.client.delete(path);
  }
  async getBrokerStatus(
    broker: "trading212" | "kraken" = "trading212",
  ): Promise<BrokerStatus | null> {
    const path =
      broker === "kraken"
        ? "/broker/kraken/status"
        : "/broker/trading212/status";
    return (await this.client.get<BrokerStatus | null>(path)).data;
  }

  // ── Account ─────────────────────────────────────────────────────────────────
  async getAccountSummary(): Promise<AccountSummary> {
    return (await this.client.get<AccountSummary>("/account/summary")).data;
  }
  async getCashGuardStatus(): Promise<CashGuardStatus> {
    return (await this.client.get<CashGuardStatus>("/account/cash-guard")).data;
  }

  // ── Instruments ─────────────────────────────────────────────────────────────
  async getInstruments(params?: {
    search?: string;
    type?: string;
    page?: number;
    page_size?: number;
  }): Promise<InstrumentList> {
    return (await this.client.get<InstrumentList>("/instruments", { params }))
      .data;
  }
  async syncInstruments(): Promise<{ synced: number; timestamp: string }> {
    return (await this.client.post("/instruments/sync")).data;
  }
  async getInstrument(ticker: string): Promise<Instrument> {
    return (await this.client.get<Instrument>(`/instruments/${ticker}`)).data;
  }

  // ── Strategies ──────────────────────────────────────────────────────────────
  async getStrategies(): Promise<Strategy[]> {
    return (await this.client.get<Strategy[]>("/strategies")).data;
  }
  async getStrategyPresets(): Promise<StrategyPresetInfo[]> {
    return (await this.client.get<StrategyPresetInfo[]>("/strategies/presets"))
      .data;
  }
  async createStrategy(payload: CreateStrategyPayload): Promise<Strategy> {
    return (await this.client.post<Strategy>("/strategies", payload)).data;
  }
  async createStrategyFromPreset(
    key: string,
    payload: CreateStrategyPresetPayload = {},
  ): Promise<Strategy> {
    return (
      await this.client.post<Strategy>(`/strategies/presets/${key}`, payload)
    ).data;
  }
  async getStrategy(id: string): Promise<Strategy> {
    return (await this.client.get<Strategy>(`/strategies/${id}`)).data;
  }
  async getStrategyPromotionStatus(
    id: string,
  ): Promise<StrategyPromotionStatus> {
    return (
      await this.client.get<StrategyPromotionStatus>(
        `/strategies/${id}/promotion-status`,
      )
    ).data;
  }
  async updateStrategyPromotion(
    id: string,
    action: StrategyPromotionAction,
    notes?: string,
  ): Promise<StrategyPromotionStatus> {
    return (
      await this.client.post<StrategyPromotionStatus>(
        `/strategies/${id}/promotion`,
        { action, notes },
      )
    ).data;
  }
  async getPortfolioMonitoring(): Promise<PortfolioStrategyMonitoring[]> {
    return (
      await this.client.get<PortfolioStrategyMonitoring[]>(
        "/strategies/portfolio-monitoring",
      )
    ).data;
  }
  async getPortfolioStrategyMonitoring(
    id: string,
  ): Promise<PortfolioStrategyMonitoring> {
    return (
      await this.client.get<PortfolioStrategyMonitoring>(
        `/strategies/${id}/portfolio-monitoring`,
      )
    ).data;
  }
  async getPortfolioAttribution(): Promise<
    PortfolioStrategyAttributionSummary[]
  > {
    return (
      await this.client.get<PortfolioStrategyAttributionSummary[]>(
        "/strategies/portfolio-attribution",
      )
    ).data;
  }
  async getPortfolioStrategyAttribution(
    id: string,
  ): Promise<PortfolioStrategyAttribution> {
    return (
      await this.client.get<PortfolioStrategyAttribution>(
        `/strategies/${id}/portfolio-attribution`,
      )
    ).data;
  }
  async getStrategyIntelligence(id: string): Promise<StrategyIntelligence> {
    return (
      await this.client.get<StrategyIntelligence>(
        `/strategies/${id}/intelligence`,
      )
    ).data;
  }
  async updateStrategy(
    id: string,
    payload: Partial<CreateStrategyPayload>,
  ): Promise<Strategy> {
    return (await this.client.patch<Strategy>(`/strategies/${id}`, payload))
      .data;
  }
  async enableStrategy(id: string): Promise<void> {
    await this.client.post(`/strategies/${id}/enable`);
  }
  async disableStrategy(id: string): Promise<void> {
    await this.client.post(`/strategies/${id}/disable`);
  }
  async dryRunStrategy(id: string): Promise<StrategyDryRunResult> {
    return (
      await this.client.post<StrategyDryRunResult>(`/strategies/${id}/run-dry`)
    ).data;
  }
  async getStrategySignals(id: string): Promise<Signal[]> {
    return (await this.client.get<Signal[]>(`/strategies/${id}/signals`)).data;
  }

  // ── Backtest ───────────────────────────────────────────────────────────────
  async listBacktestStrategies(): Promise<BacktestStrategyInfo[]> {
    return (
      await this.client.get<BacktestStrategyInfo[]>("/backtest/strategies")
    ).data;
  }
  async listPortfolioBacktestStrategies(): Promise<
    PortfolioBacktestStrategyInfo[]
  > {
    return (
      await this.client.get<PortfolioBacktestStrategyInfo[]>(
        "/backtest/portfolio/strategies",
      )
    ).data;
  }
  async runBacktest(payload: BacktestRunRequest): Promise<BacktestJobResponse> {
    return (
      await this.client.post<BacktestJobResponse>("/backtest/run", payload)
    ).data;
  }
  async getBacktestResult(jobId: string): Promise<BacktestJob> {
    return (await this.client.get<BacktestJob>(`/backtest/result/${jobId}`))
      .data;
  }
  async runPortfolioBacktest(
    payload: PortfolioBacktestRunRequest,
  ): Promise<BacktestJobResponse> {
    return (
      await this.client.post<BacktestJobResponse>(
        "/backtest/portfolio/run",
        payload,
      )
    ).data;
  }
  async getPortfolioBacktestResult(
    jobId: string,
  ): Promise<PortfolioBacktestJob> {
    return (
      await this.client.get<PortfolioBacktestJob>(
        `/backtest/portfolio/result/${jobId}`,
      )
    ).data;
  }

  // ── Signals ─────────────────────────────────────────────────────────────────
  async getSignals(params?: {
    status?: string;
    ticker?: string;
    limit?: number;
  }): Promise<Signal[]> {
    return (await this.client.get<Signal[]>("/signals", { params })).data;
  }

  // ── Orders ──────────────────────────────────────────────────────────────────
  async getOrders(params?: {
    status?: string;
    ticker?: string;
    limit?: number;
  }): Promise<Order[]> {
    return (await this.client.get<Order[]>("/orders", { params })).data;
  }
  async placeOrder(payload: CreateOrderPayload): Promise<Order> {
    return (await this.client.post<Order>("/orders", payload)).data;
  }
  async getOrder(id: string): Promise<OrderDetail> {
    return (await this.client.get<OrderDetail>(`/orders/${id}`)).data;
  }
  async cancelOrder(id: string): Promise<void> {
    await this.client.post(`/orders/${id}/cancel`);
  }
  async cancelAllPending(): Promise<{ cancelled: number }> {
    return (await this.client.post("/orders/cancel-all-pending")).data;
  }

  // ── Positions ───────────────────────────────────────────────────────────────
  async getPositions(): Promise<Position[]> {
    return (await this.client.get<Position[]>("/positions")).data;
  }
  async refreshPositions(): Promise<void> {
    await this.client.post("/positions/refresh");
  }

  // ── Risk ────────────────────────────────────────────────────────────────────
  async getRiskProfile(): Promise<RiskProfile | null> {
    return (await this.client.get<RiskProfile | null>("/risk/profile")).data;
  }
  async updateRiskProfile(payload: Partial<RiskProfile>): Promise<RiskProfile> {
    return (await this.client.patch<RiskProfile>("/risk/profile", payload))
      .data;
  }
  async getRiskEvents(params?: {
    limit?: number;
    event_type?: string;
  }): Promise<RiskEvent[]> {
    return (await this.client.get<RiskEvent[]>("/risk/events", { params }))
      .data;
  }
  async enableKillSwitch(): Promise<void> {
    await this.client.post("/risk/kill-switch/enable");
  }
  async disableKillSwitch(): Promise<void> {
    await this.client.post("/risk/kill-switch/disable");
  }
  async dailyReset(): Promise<void> {
    await this.client.post("/risk/daily-reset");
  }

  // ── Alerts ──────────────────────────────────────────────────────────────────
  async getAlerts(params?: {
    is_read?: boolean;
    limit?: number;
  }): Promise<Alert[]> {
    return (await this.client.get<Alert[]>("/alerts", { params })).data;
  }
  async sendTestAlert(): Promise<{ sent: boolean; alert_id: string }> {
    return (await this.client.post("/alerts/test")).data;
  }
  async markAlertRead(id: string): Promise<void> {
    await this.client.patch(`/alerts/${id}/read`);
  }

  // ── Settings ────────────────────────────────────────────────────────────────
  async getSettings(): Promise<AppSettings> {
    return (await this.client.get<AppSettings>("/settings")).data;
  }
  async updateSettings(payload: Partial<AppSettings>): Promise<AppSettings> {
    return (await this.client.patch<AppSettings>("/settings", payload)).data;
  }
  async getLiveReadiness(): Promise<LiveReadinessStatus> {
    return (
      await this.client.get<LiveReadinessStatus>("/settings/live-readiness")
    ).data;
  }
  async updateLiveReadiness(
    action: LiveReadinessAction,
    notes?: string,
  ): Promise<LiveReadinessStatus> {
    return (
      await this.client.post<LiveReadinessStatus>("/settings/live-readiness", {
        action,
        notes,
      })
    ).data;
  }
  async getTelegramStatus(): Promise<TelegramStatus> {
    return (await this.client.get<TelegramStatus>("/telegram/status")).data;
  }
  async sendTelegramTestAlert(): Promise<TelegramTestResult> {
    return (await this.client.post<TelegramTestResult>("/telegram/test-alert"))
      .data;
  }

  // ── Emergency ───────────────────────────────────────────────────────────────
  async emergencyKillSwitch(): Promise<EmergencyActionResult> {
    return (
      await this.client.post<EmergencyActionResult>("/emergency/kill-switch")
    ).data;
  }
  async emergencyAutoTradingOff(): Promise<EmergencyActionResult> {
    return (
      await this.client.post<EmergencyActionResult>(
        "/emergency/auto-trading/off",
      )
    ).data;
  }
  async emergencyAutoTradingOn(): Promise<EmergencyActionResult> {
    return (
      await this.client.post<EmergencyActionResult>(
        "/emergency/auto-trading/on",
      )
    ).data;
  }
  async emergencyCancelAll(): Promise<EmergencyActionResult> {
    return (
      await this.client.post<EmergencyActionResult>("/emergency/cancel-all")
    ).data;
  }
  async emergencyFlattenAll(): Promise<EmergencyActionResult> {
    return (
      await this.client.post<EmergencyActionResult>("/emergency/flatten-all")
    ).data;
  }

  // ── Reports ─────────────────────────────────────────────────────────────────
  async getPerformanceReport(days = 30): Promise<PerformanceReport> {
    return (
      await this.client.get<PerformanceReport>("/reports/performance", {
        params: { days },
      })
    ).data;
  }
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  async getPerformanceByStrategy(days = 30): Promise<any[]> {
    return (
      await this.client.get("/reports/performance/by-strategy", {
        params: { days },
      })
    ).data;
  }
  async getExecutionQualityReport(
    days = 30,
    includeDryRun = false,
  ): Promise<ExecutionQualityReport> {
    return (
      await this.client.get<ExecutionQualityReport>(
        "/reports/execution-quality",
        {
          params: { days, include_dry_run: includeDryRun },
        },
      )
    ).data;
  }
  async getTradesReport(limit = 100): Promise<Order[]> {
    return (
      await this.client.get<Order[]>("/reports/trades", { params: { limit } })
    ).data;
  }
  // Trade Journal
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  async listTrades(params?: {
    page?: number;
    page_size?: number;
    ticker?: string;
    has_notes?: boolean;
  }): Promise<any> {
    return (await this.client.get("/trades", { params })).data;
  }
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  async getTrade(id: string): Promise<any> {
    return (await this.client.get(`/trades/${id}`)).data;
  }
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  async updateTradeJournal(
    id: string,
    payload: {
      notes?: string;
      tags?: string[];
      emotion?: string;
      rating?: number;
    },
  ): Promise<any> {
    return (await this.client.patch(`/trades/${id}/journal`, payload)).data;
  }

  // ── Audit ────────────────────────────────────────────────────────────────────
  async getAuditLogs(params?: {
    action?: string;
    entity_type?: string;
    page?: number;
    page_size?: number;
  }): Promise<AuditLogList> {
    return (await this.client.get<AuditLogList>("/audit", { params })).data;
  }

  // ── Operator ────────────────────────────────────────────────────────────────
  async getOperatorStatus(): Promise<OperatorStatus> {
    return (await this.client.get<OperatorStatus>("/operator/status")).data;
  }

  // ── Intelligence ─────────────────────────────────────────────────────────────
  async getMarketRegime(): Promise<MarketRegime> {
    return (await this.client.get<MarketRegime>("/intelligence/regime")).data;
  }
  async getWatchlistIntelligence(limit = 8): Promise<WatchlistIntelligence> {
    return (
      await this.client.get<WatchlistIntelligence>("/intelligence/watchlist", {
        params: { limit },
      })
    ).data;
  }

  // ── Health ───────────────────────────────────────────────────────────────────
  async getBackendHealth(): Promise<HealthStatus> {
    return (await this.client.get<HealthStatus>("/health/live")).data;
  }
  async getHealth(): Promise<HealthStatus> {
    return this.getBackendHealth();
  }
  async getDepsHealth(): Promise<DepsHealth> {
    return (await this.client.get<DepsHealth>("/health/deps")).data;
  }
  async getMarketDataHealth(): Promise<MarketDataHealth> {
    return (await this.client.get<MarketDataHealth>("/health/market-data"))
      .data;
  }

  async getDcaActivity(): Promise<DcaActivityResponse> {
    const response = await this.client.get<DcaActivityResponse>(
      "/kraken/dca/activity",
    );
    return response.data;
  }
  async getDcaStatus(): Promise<DcaOperatorStatus> {
    return (await this.client.get<DcaOperatorStatus>("/kraken/dca/status"))
      .data;
  }
  async getDcaConfigs(): Promise<DcaConfig[]> {
    return (await this.client.get<DcaConfig[]>("/kraken/dca/configs")).data;
  }
}

export const api = new ApiClient();

export const getBackendHealth = () => api.getBackendHealth();
export const getDcaActivity = () => api.getDcaActivity();
export const getDcaStatus = () => api.getDcaStatus();
export const getDcaConfigs = () => api.getDcaConfigs();
export default api;
