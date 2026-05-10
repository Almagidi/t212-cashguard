"use client";

import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, HelpCircle, WifiOff } from "lucide-react";
import type { AxiosError } from "axios";
import { API_URL } from "@/services/api";
import api from "@/services/api";
import { Badge, Card, CardContent, CardHeader, CardTitle } from "@/components/ui";
import { cn } from "@/lib/utils";

type RuntimeProfile =
  | "Normal launcher"
  | "Manual QA"
  | "Integration"
  | "Mock E2E"
  | "Custom runtime";

type ProbeState = {
  label: string;
  status: number | "loading" | "network" | "idle" | "unknown";
  ok: boolean;
  detail: string;
};

function httpStatus(error: unknown): number | "network" | "unknown" {
  const axiosError = error as AxiosError | undefined;
  if (axiosError?.response?.status) return axiosError.response.status;
  if (axiosError?.code === "ERR_NETWORK" || axiosError?.message === "Network Error") {
    return "network";
  }
  return "unknown";
}

function probeState(
  label: string,
  query: {
    isPending: boolean;
    isError: boolean;
    error: unknown;
    data: unknown;
    fetchStatus?: string;
  },
): ProbeState {
  if (query.isPending && query.fetchStatus !== "idle") {
    return { label, status: "loading", ok: false, detail: "Checking..." };
  }
  if (query.data) return { label, status: 200, ok: true, detail: "HTTP 200" };
  if (query.isError) {
    const status = httpStatus(query.error);
    const detail =
      status === "network"
        ? "Network error"
        : status === "unknown"
          ? "Unknown error"
          : `HTTP ${status}`;
    return { label, status, ok: false, detail };
  }
  return { label, status: "idle", ok: false, detail: "Not checked" };
}

function profileFromPorts(webPort: string, apiPort: string): RuntimeProfile {
  if (webPort === "3000" && apiPort === "8000") return "Normal launcher";
  if (webPort === "3002" && apiPort === "8002") return "Manual QA";
  if (webPort === "3001" && apiPort === "8001") return "Integration";
  if (webPort === "3100") return "Mock E2E";
  return "Custom runtime";
}

function statusTone(status: ProbeState["status"]) {
  if (status === 200) return "success";
  if (status === "loading" || status === "idle") return "outline";
  if (status === 401 || status === 404) return "warning";
  return "destructive";
}

function statusIcon(state: ProbeState) {
  if (state.ok) return <CheckCircle2 className="h-3.5 w-3.5 text-emerald-400" />;
  if (state.status === "network") return <WifiOff className="h-3.5 w-3.5 text-red-400" />;
  if (state.status === "loading" || state.status === "idle") {
    return <HelpCircle className="h-3.5 w-3.5 text-muted-foreground" />;
  }
  return <AlertTriangle className="h-3.5 w-3.5 text-amber-400" />;
}

function endpointExplanation(state: ProbeState): string {
  if (state.status === 200) return `${state.label} is reachable.`;
  if (state.status === 401) return `${state.label} returned 401. You are not authenticated; log in again.`;
  if (state.status === 404) return `${state.label} returned 404. Routes are not registered in this backend build.`;
  if (state.status === "network") return `${state.label} is unreachable from the browser.`;
  if (typeof state.status === "number" && state.status >= 500) {
    return `${state.label} returned ${state.status}. The route is registered but failing; check API logs.`;
  }
  return `${state.label} status is still being checked.`;
}

function visibilityMessage({
  frontendMode,
  backendMode,
  profile,
  dcaStates,
}: {
  frontendMode: string;
  backendMode: string | null;
  profile: RuntimeProfile;
  dcaStates: ProbeState[];
}): string {
  const firstFailure = dcaStates.find((state) => !state.ok && state.status !== "loading");
  if (backendMode && frontendMode !== backendMode) {
    return `Frontend mode is ${frontendMode} but backend mode is ${backendMode}. Check launcher/env wiring before trusting Kraken/DCA visibility.`;
  }
  if (dcaStates.every((state) => state.ok)) {
    return "Kraken/DCA readiness data is available.";
  }
  if (firstFailure?.status === 401) return "You are not authenticated. Log in again.";
  if (firstFailure?.status === 404) return "Routes are not registered in this backend build.";
  if (typeof firstFailure?.status === "number" && firstFailure.status >= 500) {
    return "Backend route is registered but failing. Check API logs.";
  }
  if (profile === "Manual QA" && frontendMode === "mock") {
    return "Manual QA mock mode should show read-only Kraken/DCA data and requires no broker credentials.";
  }
  if (profile === "Normal launcher" && frontendMode === "demo") {
    return "Normal launcher is in demo mode; Kraken/DCA cards may not be seeded unless configured. Use make operator-manual for mock Kraken/DCA QA.";
  }
  return "Kraken/DCA visibility depends on the current mode, auth token, and registered backend routes.";
}

function nextAction({
  frontendMode,
  backendMode,
  profile,
  states,
}: {
  frontendMode: string;
  backendMode: string | null;
  profile: RuntimeProfile;
  states: ProbeState[];
}): string {
  if (states.some((state) => state.status === "network")) {
    return `Start or check the API at ${API_URL}.`;
  }
  if (states.some((state) => state.status === 401)) {
    return "Log in again, then reload this page.";
  }
  if (states.some((state) => state.status === 404)) {
    return "Use a backend build with operator and Kraken DCA routes registered.";
  }
  if (states.some((state) => typeof state.status === "number" && state.status >= 500)) {
    return "Check API logs for the failing read-only endpoint.";
  }
  if (profile === "Normal launcher" && (backendMode ?? frontendMode) === "demo") {
    return "Use make operator-manual for mock Kraken/DCA QA.";
  }
  if (profile === "Manual QA") {
    return "Manual QA is the expected read-only path for Kraken/DCA visibility checks.";
  }
  return "Use the port/profile rows below to confirm this is the runtime you intended.";
}

function DiagnosticRow({ state }: { state: ProbeState }) {
  return (
    <div className="flex items-center justify-between gap-3 rounded-lg border border-border/60 bg-background/35 px-3 py-2">
      <div className="flex min-w-0 items-center gap-2">
        {statusIcon(state)}
        <span className="truncate text-xs font-medium text-foreground">{state.label}</span>
      </div>
      <Badge variant={statusTone(state.status)}>{state.detail}</Badge>
    </div>
  );
}

export function RuntimeDiagnostics({ compact = false }: { compact?: boolean }) {
  const frontendMode = process.env.NEXT_PUBLIC_APP_MODE || "mock";
  const hasToken = api.isAuthenticated();
  const health = useQuery({
    queryKey: ["runtime-diagnostics", "health"],
    queryFn: api.getBackendHealth.bind(api),
    retry: false,
    refetchInterval: 30_000,
  });
  const auth = useQuery({
    queryKey: ["runtime-diagnostics", "auth"],
    queryFn: api.getMe.bind(api),
    retry: false,
    enabled: hasToken,
    refetchInterval: 30_000,
  });
  const operator = useQuery({
    queryKey: ["runtime-diagnostics", "operator"],
    queryFn: api.getOperatorStatus.bind(api),
    retry: false,
    refetchInterval: 30_000,
  });
  const dcaStatus = useQuery({
    queryKey: ["runtime-diagnostics", "dca-status"],
    queryFn: api.getDcaStatus.bind(api),
    retry: false,
    refetchInterval: 30_000,
  });
  const dcaActivity = useQuery({
    queryKey: ["runtime-diagnostics", "dca-activity"],
    queryFn: api.getDcaActivity.bind(api),
    retry: false,
    refetchInterval: 30_000,
  });
  const dcaConfigs = useQuery({
    queryKey: ["runtime-diagnostics", "dca-configs"],
    queryFn: api.getDcaConfigs.bind(api),
    retry: false,
    refetchInterval: 30_000,
  });

  const { webPort, apiPort, profile } = useMemo(() => {
    if (typeof window === "undefined") {
      return { webPort: "", apiPort: "", profile: "Custom runtime" as RuntimeProfile };
    }
    const currentWebPort =
      window.location.port || (window.location.protocol === "https:" ? "443" : "80");
    let currentApiPort = "";
    try {
      const apiUrl = new URL(API_URL, window.location.origin);
      currentApiPort = apiUrl.port || (apiUrl.protocol === "https:" ? "443" : "80");
    } catch {
      currentApiPort = "";
    }
    return {
      webPort: currentWebPort,
      apiPort: currentApiPort,
      profile: profileFromPorts(currentWebPort, currentApiPort),
    };
  }, []);

  const backendMode = health.data?.mode ?? null;
  const states = [
    probeState("Backend health /v1/health/live", health),
    hasToken
      ? probeState("Auth /v1/auth/me", auth)
      : { label: "Auth /v1/auth/me", status: 401, ok: false, detail: "No token" },
    probeState("Operator /v1/operator/status", operator),
    probeState("DCA /v1/kraken/dca/status", dcaStatus),
    probeState("DCA /v1/kraken/dca/activity", dcaActivity),
    probeState("DCA /v1/kraken/dca/configs", dcaConfigs),
  ] satisfies ProbeState[];
  const dcaStates = states.slice(3);
  const message = visibilityMessage({ frontendMode, backendMode, profile, dcaStates });
  const action = nextAction({ frontendMode, backendMode, profile, states });

  return (
    <Card
      className={cn(
        "border-blue-500/20 bg-blue-500/5",
        compact && "rounded-lg",
      )}
      aria-label="Runtime diagnostics"
      data-testid="mock-runtime-status"
    >
      <CardHeader className={compact ? "px-4 pt-4 pb-2" : undefined}>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <CardTitle>Runtime Diagnostics</CardTitle>
          <div className="flex flex-wrap gap-1.5">
            <Badge variant="info">{profile}</Badge>
            <Badge variant={frontendMode === backendMode ? "success" : "warning"}>
              Frontend {frontendMode}
            </Badge>
            <Badge variant={backendMode ? "info" : "outline"}>
              Backend {backendMode ?? "checking"}
            </Badge>
          </div>
        </div>
      </CardHeader>
      <CardContent className={cn("space-y-4", compact && "px-4 pb-4")}>
        <div className="grid gap-2 text-xs md:grid-cols-3">
          <div>
            <p className="text-muted-foreground">API URL</p>
            <p className="mt-1 break-all font-mono text-foreground">{API_URL}</p>
          </div>
          <div>
            <p className="text-muted-foreground">Web port</p>
            <p className="mt-1 font-mono text-foreground">:{webPort || "unknown"}</p>
          </div>
          <div>
            <p className="text-muted-foreground">API port</p>
            <p className="mt-1 font-mono text-foreground">:{apiPort || "unknown"}</p>
          </div>
        </div>

        {!compact && (
          <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
            {states.map((state) => (
              <DiagnosticRow key={state.label} state={state} />
            ))}
          </div>
        )}

        <div className="space-y-2 rounded-lg border border-border/60 bg-background/35 p-3">
          <p className="text-sm font-semibold text-foreground">{message}</p>
          <p className="text-xs leading-relaxed text-muted-foreground">{action}</p>
          {!compact && (
            <ul className="space-y-1 text-xs text-muted-foreground">
              {states
                .filter((state) => !state.ok && state.status !== "loading")
                .slice(0, 3)
                .map((state) => (
                  <li key={state.label}>{endpointExplanation(state)}</li>
                ))}
            </ul>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
